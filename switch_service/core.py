# SPDX-License-Identifier: Apache-2.0
"""Orchestrator: triangle transitions, admission and reconciliation."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from switch_service.admission import Holder, admit
from switch_service.catalog import Catalog, Profile, ServiceSpec
from switch_service.errors import SwitchError
from switch_service.runtime import ContainerSpec, FakeRuntime, PodmanRuntime
from switch_service.sample import CachingSampler, sampler_for
from switch_service.store import Store, TransitionRow
from switch_service.util import duration_ms, other_vertices, redact, utcnow

log = logging.getLogger("switch_service")

OVER_BUDGET_RATIO = 1.15
_VERTICES = {"off", "cpu", "gpu"}


class SwitchCore:
    """Embeddable control plane.

    ``set_state`` moves one service along the off/cpu/gpu triangle. The same
    state is a no-op when the service is already healthy there, and a retry
    when the last attempt failed. Resource checks happen before any container
    is changed; the long start runs afterwards.
    """

    def __init__(
        self,
        catalog: Catalog,
        store: Store,
        runtime: FakeRuntime | PodmanRuntime,
        sampler: CachingSampler,
        *,
        synchronous: bool = False,
        reconcile: bool = False,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.runtime = runtime
        self.sampler = sampler
        self.synchronous = synchronous
        self.reconcile = reconcile
        self.reconcile_interval = catalog.host.reconcile_interval_s
        self._cv = threading.Condition()
        self._queue: list[str] = []
        self._stop = False
        self._thread: threading.Thread | None = None
        self._counts: dict[tuple[str, str, str], int] = {}
        self._network_lock = threading.Lock()
        self._network_ready = catalog.host.network is None
        for service in catalog.services:
            store.ensure_service(service.id)

    @classmethod
    def open(
        cls,
        config_path: str,
        *,
        db_path: str | None = None,
        runtime: str | None = None,
        synchronous: bool = False,
        reconcile: bool | None = None,
        secrets_file: str | None = None,
    ) -> SwitchCore:
        catalog = Catalog.load(config_path, secrets_file=secrets_file)
        db_path = db_path or os.environ.get("SWITCH_DB", "var/switch.sqlite")
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        store = Store(db_path)
        runtime_name = runtime or os.environ.get("SWITCH_RUNTIME", "fake")
        engine: FakeRuntime | PodmanRuntime
        if runtime_name == "fake":
            engine = FakeRuntime()
        elif runtime_name == "podman":
            engine = PodmanRuntime()
        else:
            raise SwitchError(400, "BAD_OPTIONS", f"unknown runtime {runtime_name}")
        if reconcile is None:
            reconcile = not synchronous
        return cls(
            catalog,
            store,
            engine,
            sampler_for(catalog.host),
            synchronous=synchronous,
            reconcile=reconcile,
        )

    def close(self) -> None:
        self.stop_worker()
        self.store.close()

    def start_worker(self) -> None:
        if self.synchronous:
            return
        with self._cv:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._loop, name="switch-worker", daemon=True)
            self._thread.start()

    def stop_worker(self, timeout: float = 5) -> None:
        thread = self._thread
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if thread is not None:
            thread.join(timeout=timeout)

    def recover(self) -> None:
        """Resume transitions that were still running when the process stopped."""
        with self._cv:
            ids = [item.id for item in self.store.running_transitions()]
        if self.synchronous:
            for transition_id in ids:
                self._execute(transition_id)
            return
        with self._cv:
            for transition_id in ids:
                if transition_id not in self._queue:
                    self._queue.append(transition_id)
            self._cv.notify()

    def set_state(
        self,
        service_id: str,
        target: str,
        *,
        evict: list[str] | None = None,
        force: bool = False,
        options: dict[str, Any] | None = None,
        actor: str = "local",
        recreate: bool = False,
    ) -> dict[str, Any]:
        service, profile, vertex, evict_ids, options = self._validate(
            service_id, target, evict, force, options
        )
        noop = None if recreate else self._try_noop(service, profile, vertex, options)
        if noop is not None:
            return noop
        if not self.runtime.ready():
            raise SwitchError(503, "RUNTIME_UNAVAILABLE", "container runtime is not reachable")
        sample = self.sampler.sample()
        transition_id = self._prepare(
            service, profile, vertex, evict_ids, force, options, actor, recreate, sample
        )
        if self.synchronous:
            error = self._execute(transition_id)
            view = self._transition_view(transition_id)
            if error is not None:
                raise error
            return {"noop": False, "transition": view}
        with self._cv:
            self._queue.append(transition_id)
            self._cv.notify()
        return {"noop": False, "transition": self._transition_view(transition_id)}

    def restart(self, service_id: str, *, actor: str = "local") -> dict[str, Any]:
        self._known(service_id)
        with self._cv:
            row = self.store.get_service(service_id)
            if row.desired_state not in {"cpu", "gpu"}:
                raise SwitchError(409, "BAD_STATE", "only a cpu or gpu service can be restarted")
            target = row.desired_state
            options = dict(row.options)
        return self.set_state(service_id, target, options=options, actor=actor, recreate=True)

    def recalibrate(self, service_id: str, *, actor: str = "local") -> dict[str, Any]:
        service = self._known(service_id)
        with self._cv:
            row = self.store.get_service(service_id)
            if row.state != "ready" or row.mode not in service.profiles:
                raise SwitchError(409, "BAD_STATE", "recalibrate requires a ready service")
            profile = service.profiles[row.mode]
        stats = self.runtime.stats(service.container_name)
        ram = stats.ram_mib if stats is not None else None
        fields: dict[str, Any] = {"measured_ram_mib": ram, "measured_at": utcnow()}
        if profile.calibrate_vram:
            sample = self.sampler.sample()
            if sample.vram_used_mib is not None:
                fields["measured_vram_mib"] = self._vram_of(service_id, sample.vram_used_mib)
        with self._cv:
            self.store.update_service(service_id, **fields)
            self.store.add_audit(actor, "recalibrate", service_id, "measured")
        return self.service_view(service_id)

    def logs(self, service_id: str, tail: int = 200) -> str:
        service = self._known(service_id)
        tail = max(1, min(int(tail), 2000))
        text = self.runtime.logs(service.container_name, tail)
        return redact(text, list(self.catalog.secret_values))

    def transition_history(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._cv:
            rows = self.store.list_transitions(limit)
        return [self._row_view(row) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        sample = self.sampler.sample()
        services = [self.service_view(service.id) for service in self.catalog.services]
        claimed_ram = sum(item["claim"]["ram_mib"] for item in services)
        claimed_vram = sum(item["claim"]["vram_mib"] for item in services)
        ram_budget = max(0, sample.ram_total_mib - self.catalog.host.ram_reserve_mib)
        vram_block: dict[str, Any] | None
        if sample.vram_total_mib is None:
            vram_block = None
        else:
            vram_budget = max(0, sample.vram_total_mib - self.catalog.host.vram_reserve_mib)
            vram_block = {
                "total_mib": sample.vram_total_mib,
                "used_mib": sample.vram_used_mib,
                "reserve_mib": self.catalog.host.vram_reserve_mib,
                "budget_mib": vram_budget,
                "claimed_mib": claimed_vram,
                "free_mib": vram_budget - claimed_vram,
            }
        active = [item["transition"] for item in services if item["transition"]]
        return {
            "host": {
                "ram": {
                    "total_mib": sample.ram_total_mib,
                    "available_mib": sample.ram_available_mib,
                    "reserve_mib": self.catalog.host.ram_reserve_mib,
                    "budget_mib": ram_budget,
                    "claimed_mib": claimed_ram,
                    "free_mib": ram_budget - claimed_ram,
                },
                "vram": vram_block,
                "cpu": {"threads": sample.cpu_threads, "load1": sample.load1},
                "device": {
                    "index": sample.device_index,
                    "name": sample.device_name,
                    "compute_cap": sample.compute_cap,
                },
                "sampled_at": sample.sampled_at,
                "sample_source": sample.source,
            },
            "services": services,
            "active_transitions": active,
        }

    def service_view(self, service_id: str) -> dict[str, Any]:
        service = self._known(service_id)
        with self._cv:
            row = self.store.get_service(service_id)
            active = self.store.active_transition(service_id)
        info = None
        try:
            info = self.runtime.inspect(service.container_name)
        except SwitchError:
            info = None
        mode = row.mode if row.mode in _VERTICES else None
        if active is not None:
            allowed: list[str] = []
        elif row.state == "ready" and mode:
            vertex = mode
            allowed = other_vertices(vertex)
        else:
            vertex = row.desired_state if row.desired_state in _VERTICES else "off"
            allowed = other_vertices(vertex)
        claim_ram, claim_vram = row.reserve_ram_mib, row.reserve_vram_mib
        profile = service.profiles.get(mode) if mode else None
        over = False
        if profile is not None and row.state == "ready":
            limit_ram = profile.claim_ram_mib
            limit_vram = profile.claim_vram_mib
            if row.options.get("references"):
                limit_vram += profile.vram_per_reference_mib * int(row.options["references"])
            ram_over = (
                row.measured_ram_mib is not None
                and limit_ram
                and row.measured_ram_mib > limit_ram * OVER_BUDGET_RATIO
            )
            if ram_over:
                over = True
            if (
                row.measured_vram_mib is not None
                and limit_vram
                and row.measured_vram_mib > limit_vram * OVER_BUDGET_RATIO
            ):
                over = True
        expected_hash = None
        if row.state == "ready" and mode and mode in service.profiles:
            expected_hash = self._container_spec(service, service.profiles[mode], recreate=False).config_hash
        drift = bool(
            expected_hash
            and (
                (row.config_hash and row.config_hash != expected_hash)
                or (info and info.config_hash and info.config_hash != expected_hash)
            )
        )
        healthy = False
        if info and info.running and mode and mode in service.profiles:
            try:
                healthy = self.runtime.probe(
                    self._container_spec(service, service.profiles[mode], recreate=False)
                )
            except SwitchError:
                healthy = False
        if info is None:
            container_health = "absent"
        elif info.running and healthy:
            container_health = "healthy"
        elif info.running:
            container_health = "unhealthy"
        else:
            container_health = info.status or "exited"
        notes = list(service.notes)
        if over:
            notes.append("measured use is more than 15% above the claim")
        measured = None
        if row.measured_at and (row.measured_ram_mib is not None or row.measured_vram_mib is not None):
            measured = {
                "ram_mib": row.measured_ram_mib,
                "vram_mib": row.measured_vram_mib,
                "at": row.measured_at,
            }
        retry = row.desired_state if row.state != "ready" and row.desired_state in {"cpu", "gpu"} else None
        return {
            "id": service.id,
            "title": service.title,
            "kind": service.kind,
            "state": row.state,
            "mode": mode,
            "desired_state": row.desired_state,
            "pending_state": active.to_state if active else None,
            "busy": active is not None,
            "supported_states": service.supported_states,
            "vertices": ["off", "cpu", "gpu"],
            "transitions_allowed": allowed,
            "retry_state": retry,
            "unavailable_states": service.unavailable(),
            "preemptible": service.preemptible,
            "options": row.options,
            "claim": {"ram_mib": claim_ram, "vram_mib": claim_vram},
            "profiles": {
                name: {
                    "available": item.available,
                    "reason": item.reason,
                    "claim": {"ram_mib": item.claim_ram_mib, "vram_mib": item.claim_vram_mib},
                    **(
                        {"vram_per_reference_mib": item.vram_per_reference_mib}
                        if item.vram_per_reference_mib
                        else {}
                    ),
                }
                for name, item in service.profiles.items()
            },
            "measured": measured,
            "over_budget": over,
            "container": {
                "name": service.container_name,
                "id": None if info is None else info.id,
                "image": None if info is None else info.image,
                "health": container_health,
                "running": bool(info and info.running),
            },
            "health": {
                "ok": healthy,
                "checked_at": utcnow(),
                "detail": container_health,
            },
            "consumers": list(service.consumers),
            "notes": notes,
            "drift": drift,
            "fail_sticky": row.fail_sticky,
            "last_error": row.last_error,
            "transition": None if active is None else self._row_view(active),
        }

    def render_metrics(self) -> str:
        snap = self.snapshot()
        lines = [
            "# HELP switch_service_info Control plane sample source.",
            "# TYPE switch_service_info gauge",
            f'switch_service_info{{source="{snap["host"]["sample_source"]}"}} 1',
            "# TYPE switch_service_state gauge",
            "# TYPE switch_measured_ram_mib gauge",
            "# TYPE switch_measured_vram_mib gauge",
            "# TYPE switch_transitions_total counter",
        ]
        for service in snap["services"]:
            mode = service["mode"] or ""
            lines.append(
                f'switch_service_state{{id="{service["id"]}",state="{service["state"]}",mode="{mode}"}} 1'
            )
            measured = service["measured"]
            if measured and measured["ram_mib"] is not None:
                lines.append(
                    f'switch_measured_ram_mib{{id="{service["id"]}"}} {measured["ram_mib"]}'
                )
            if measured and measured["vram_mib"] is not None:
                lines.append(
                    f'switch_measured_vram_mib{{id="{service["id"]}"}} {measured["vram_mib"]}'
                )
        with self._cv:
            items = list(self._counts.items())
        for (service_id, target, result), count in items:
            lines.append(
                f'switch_transitions_total{{id="{service_id}",to="{target}",result="{result}"}} {count}'
            )
        return "\n".join(lines) + "\n"

    def reconcile_once(self) -> None:
        if not self.runtime.ready():
            return
        for service in self.catalog.services:
            try:
                self._reconcile_one(service.id)
            except SwitchError as exc:
                if exc.code not in {
                    "BUSY",
                    "INSUFFICIENT_VRAM",
                    "INSUFFICIENT_RAM",
                    "CANNOT_EVICT",
                    "RUNTIME_UNAVAILABLE",
                }:
                    log.warning("reconcile %s: %s", service.id, exc.message)
            except Exception:
                log.exception("reconcile %s", service.id)

    def _loop(self) -> None:
        while True:
            with self._cv:
                if self._stop and not self._queue:
                    return
                if not self._queue and not self._stop:
                    timeout = self.reconcile_interval if self.reconcile else None
                    self._cv.wait(timeout=timeout)
                if self._stop and not self._queue:
                    return
                transition_id = self._queue.pop(0) if self._queue else None
                run_reconcile = transition_id is None and self.reconcile and not self._stop
            if transition_id is not None:
                error = self._execute(transition_id)
                if error is not None:
                    log.info("transition %s finished with %s", transition_id, error.code)
            elif run_reconcile:
                self.reconcile_once()

    def _reconcile_one(self, service_id: str) -> None:
        service = self.catalog.get(service_id)
        with self._cv:
            row = self.store.get_service(service_id)
            if self.store.active_transition(service_id) or row.fail_sticky:
                return
            if row.next_retry_at and row.next_retry_at > utcnow():
                return
            desired = row.desired_state
            options = dict(row.options)
            state = row.state
            mode = row.mode
        if desired == "off":
            info = self.runtime.inspect(service.container_name)
            if info and info.running:
                self.set_state(service_id, "off", actor="reconcile")
            return
        profile = service.profiles.get(desired)
        if profile is None or not profile.available:
            return
        if self._matches(service, profile):
            if state != "ready" or mode != desired:
                claim_ram, claim_vram = _claim(profile, options)
                spec = self._container_spec(service, profile, recreate=False)
                with self._cv:
                    self.store.update_service(
                        service_id,
                        state="ready",
                        mode=desired,
                        desired_state=desired,
                        reserve_ram_mib=claim_ram,
                        reserve_vram_mib=claim_vram,
                        config_hash=spec.config_hash,
                        fail_sticky=0,
                    )
            return
        self.set_state(service_id, desired, options=options, actor="reconcile")

    def _validate(
        self,
        service_id: str,
        target: str,
        evict: list[str] | None,
        force: bool,
        options: dict[str, Any] | None,
    ) -> tuple[ServiceSpec, Profile | None, str, list[str], dict[str, Any]]:
        if not isinstance(target, str) or target not in _VERTICES:
            raise SwitchError(400, "BAD_STATE", "state must be off, cpu or gpu")
        if not isinstance(force, bool):
            raise SwitchError(400, "BAD_OPTIONS", "force must be a boolean")
        if evict is None:
            evict = []
        if not isinstance(evict, list) or not all(isinstance(item, str) for item in evict):
            raise SwitchError(400, "BAD_OPTIONS", "evict must be a list of service ids")
        service = self._known(service_id)
        profile = self._require_profile(service, target, force)
        with self._cv:
            stored = dict(self.store.get_service(service_id).options)
        normalized = self._normalize_options(service, target, options, stored, profile)
        return service, profile, target, list(evict), normalized

    def _try_noop(
        self,
        service: ServiceSpec,
        profile: Profile | None,
        target: str,
        options: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._cv:
            row = self.store.get_service(service.id)
            if self.store.active_transition(service.id) or row.fail_sticky or row.desired_state != target:
                return None
            if target == "off":
                already_off = row.state == "off"
            else:
                already_off = False
                if row.state != "ready" or row.mode != target or row.options != options:
                    return None
        if target == "off":
            if not already_off:
                return None
            try:
                info = self.runtime.inspect(service.container_name)
            except SwitchError:
                return None
            if info is not None and info.running:
                return None
            return {"noop": True, "transition": None}
        if profile is None:
            return None
        try:
            matched = self._matches(service, profile)
        except SwitchError:
            return None
        if not matched:
            return None
        return {"noop": True, "transition": None}

    def _prepare(
        self,
        service: ServiceSpec,
        profile: Profile | None,
        target: str,
        evict: list[str],
        force: bool,
        options: dict[str, Any],
        actor: str,
        recreate: bool,
        sample: Any,
    ) -> str:
        with self._cv:
            row = self.store.get_service(service.id)
            if self.store.active_transition(service.id):
                raise SwitchError(423, "BUSY", f"{service.id} already has a transition in progress")
            for other in evict:
                if self.store.active_transition(other):
                    raise SwitchError(423, "BUSY", f"{other} already has a transition in progress")
            holders = []
            preemptible: dict[str, bool] = {}
            for item in self.catalog.services:
                current = self.store.get_service(item.id)
                holders.append(
                    Holder(
                        id=item.id,
                        reserve_ram_mib=current.reserve_ram_mib,
                        reserve_vram_mib=current.reserve_vram_mib,
                        measured_ram_mib=current.measured_ram_mib or 0,
                        measured_vram_mib=current.measured_vram_mib or 0,
                    )
                )
                preemptible[item.id] = item.preemptible
            claim_ram, claim_vram = (0, 0) if profile is None else _claim(profile, options)
            decision = admit(
                subject=service.title,
                target_id=service.id,
                holders=holders,
                known_ids={item.id for item in self.catalog.services},
                preemptible=preemptible,
                sample=sample,
                ram_reserve_mib=self.catalog.host.ram_reserve_mib,
                vram_reserve_mib=self.catalog.host.vram_reserve_mib,
                claim_ram_mib=claim_ram,
                claim_vram_mib=claim_vram,
                evict=evict,
                force=force,
            )
            if not decision.ok:
                assert decision.error is not None
                raise decision.error
            needs_force = bool(profile is not None and not profile.available)
            needs_force = needs_force or any(not preemptible[item] for item in decision.evict)
            if target == "off":
                reserve_ram, reserve_vram = row.reserve_ram_mib, row.reserve_vram_mib
                new_state = "stopping"
            else:
                reserve_ram = max(row.reserve_ram_mib, claim_ram)
                reserve_vram = max(row.reserve_vram_mib, claim_vram)
                new_state = "starting"
            transition_id = "t_" + uuid.uuid4().hex[:12]
            self.store.update_service(
                service.id,
                desired_state=target,
                state=new_state,
                reserve_ram_mib=reserve_ram,
                reserve_vram_mib=reserve_vram,
                options=options if target != "off" else row.options,
                fail_sticky=0,
                retry_count=0,
                next_retry_at=None,
                last_error=None,
            )
            self.store.insert_transition(
                transition_id=transition_id,
                service_id=service.id,
                from_state=row.desired_state,
                to_state=target,
                actor=actor,
                forced=bool(force and needs_force),
                evict=list(decision.evict),
                options=options,
                from_options=row.options,
                recreate=recreate,
            )
            self.store.add_audit(
                actor, "transition", service.id, f"{row.desired_state}->{target}"
            )
            return transition_id

    def _execute(self, transition_id: str) -> SwitchError | None:
        try:
            with self._cv:
                transition = self.store.get_transition(transition_id)
            if transition.status != "running":
                return None
            for service_id in transition.evict:
                self._phase(transition_id, "evicting")
                self._bring_down(service_id, actor=transition.actor, reason="evict")
            service = self.catalog.get(transition.service_id)
            if transition.to_state == "off":
                self._phase(transition_id, "stopping")
                self._bring_down(service.id, actor=transition.actor, reason="stop")
                self._finish(transition, "ok", None)
                return None
            profile = service.profiles[transition.to_state]
            self._converge(service, profile, transition.options, transition.recreate, transition_id)
            self._finish(transition, "ok", None)
            return None
        except SwitchError as exc:
            self._rollback(transition_id, exc)
            return exc
        except Exception as exc:
            log.exception("transition %s crashed", transition_id)
            wrapped = SwitchError(502, "RUNTIME_FAILED", str(exc))
            self._rollback(transition_id, wrapped)
            return wrapped

    def _converge(
        self,
        service: ServiceSpec,
        profile: Profile,
        options: dict[str, Any],
        recreate: bool,
        transition_id: str | None,
    ) -> None:
        spec = self._container_spec(service, profile, recreate=recreate)
        before = self.sampler.sample()
        if transition_id:
            self._phase(transition_id, "creating")
        self._ensure_network()
        self.runtime.ensure(spec)
        if transition_id:
            self._phase(transition_id, "starting")
        self.runtime.start(spec)
        if transition_id:
            self._phase(transition_id, "warming")
        self._wait_healthy(service, spec)
        stats = self.runtime.stats(spec.name)
        after = self.sampler.sample()
        measured_ram = None if stats is None else stats.ram_mib
        measured_vram = None
        if (
            profile.calibrate_vram
            and before.vram_used_mib is not None
            and after.vram_used_mib is not None
        ):
            measured_vram = max(0, after.vram_used_mib - before.vram_used_mib)
        claim_ram, claim_vram = _claim(profile, options)
        if transition_id:
            self._phase(transition_id, "committing")
        with self._cv:
            self.store.update_service(
                service.id,
                desired_state=profile.mode,
                state="ready",
                mode=profile.mode,
                config_hash=spec.config_hash,
                reserve_ram_mib=claim_ram,
                reserve_vram_mib=claim_vram,
                measured_ram_mib=measured_ram,
                measured_vram_mib=measured_vram,
                measured_at=utcnow(),
                options=options,
                fail_sticky=0,
                last_error=None,
            )

    def _rollback(self, transition_id: str, exc: SwitchError) -> None:
        with self._cv:
            transition = self.store.get_transition(transition_id)
        if transition.status != "running":
            return
        restored = False
        try:
            if transition.from_state == "off":
                self._bring_down(transition.service_id, actor=transition.actor, reason="rollback")
                restored = True
            else:
                service = self.catalog.get(transition.service_id)
                profile = service.profiles.get(transition.from_state)
                if profile is not None and profile.available:
                    self._converge(service, profile, transition.from_options, False, None)
                    restored = True
        except Exception:
            log.exception("rollback of %s failed", transition_id)
            restored = False
        with self._cv:
            row = self.store.get_service(transition.service_id)
            if not restored:
                self.store.update_service(
                    transition.service_id,
                    desired_state=transition.from_state,
                    state="failed",
                    mode=None,
                    reserve_ram_mib=0,
                    reserve_vram_mib=0,
                    fail_sticky=1,
                    last_error=exc.message,
                    options=transition.from_options,
                )
            else:
                fields: dict[str, Any] = {
                    "desired_state": transition.from_state,
                    "fail_sticky": 0,
                    "last_error": exc.message,
                    "options": transition.from_options,
                }
                if transition.actor == "reconcile":
                    retry = row.retry_count + 1
                    fields["retry_count"] = retry
                    fields["next_retry_at"] = _later(min(60, 5 * retry))
                    if retry >= 3:
                        fields["fail_sticky"] = 1
                        fields["state"] = "failed"
                self.store.update_service(transition.service_id, **fields)
            self._finish(transition, "failed", exc)

    def _finish(self, transition: TransitionRow, status: str, error: SwitchError | None) -> None:
        # Caller may already hold the lock (rollback). Re-entering is safe.
        with self._cv:
            current = self.store.get_transition(transition.id)
            if current.status != "running":
                return
            finished = utcnow()
            fields: dict[str, Any] = {
                "status": status,
                "finished_at": finished,
                "duration_ms": duration_ms(current.started_at, finished),
            }
            if error is not None:
                fields["error_code"] = error.code
                fields["error_detail"] = error.message
            else:
                fields["phase"] = "committing"
            self.store.update_transition(transition.id, **fields)
            self._counts[(transition.service_id, transition.to_state, status)] = (
                self._counts.get((transition.service_id, transition.to_state, status), 0) + 1
            )
            self.store.add_audit(
                transition.actor,
                "transition_" + status,
                transition.service_id,
                error.code if error else transition.to_state,
            )
        log.info(
            "transition %s %s %s -> %s %s",
            transition.id,
            transition.service_id,
            transition.from_state,
            transition.to_state,
            status,
        )

    def _bring_down(self, service_id: str, *, actor: str, reason: str) -> None:
        service = self.catalog.get(service_id)
        with self._cv:
            grace = self._grace_locked(service_id)
        info = self.runtime.inspect(service.container_name)
        if info and info.running:
            self.runtime.stop(service.container_name, grace)
            info = self.runtime.inspect(service.container_name)
            if info and info.running:
                self.runtime.kill(service.container_name)
        with self._cv:
            self.store.update_service(
                service_id,
                desired_state="off",
                state="off",
                mode=None,
                reserve_ram_mib=0,
                reserve_vram_mib=0,
                fail_sticky=0,
                last_error=None,
            )
            self.store.add_audit(actor, reason, service_id, "off")

    def _wait_healthy(self, service: ServiceSpec, spec: ContainerSpec) -> None:
        profile = service.profiles[spec.mode]
        deadline = time.monotonic() + profile.startup_timeout_s
        while True:
            info = self.runtime.inspect(spec.name)
            if info is None or not info.running:
                raise SwitchError(
                    502,
                    "CONTAINER_EXITED",
                    f"{spec.name} exited before it became healthy",
                    {"log_tail": self._tail(spec.name)},
                )
            if self.runtime.probe(spec):
                return
            if time.monotonic() >= deadline:
                raise SwitchError(
                    502,
                    "HEALTH_TIMEOUT",
                    f"{spec.name} did not become healthy in {profile.startup_timeout_s}s",
                    {"log_tail": self._tail(spec.name)},
                )
            remaining = deadline - time.monotonic()
            time.sleep(min(service.health_interval_s, max(0.01, remaining)))

    def _matches(self, service: ServiceSpec, profile: Profile) -> bool:
        spec = self._container_spec(service, profile, recreate=False)
        info = self.runtime.inspect(service.container_name)
        if info is None or not info.running or info.mode != profile.mode:
            return False
        if info.config_hash != spec.config_hash:
            return False
        return self.runtime.probe(spec)

    def _ensure_network(self) -> None:
        """Create ``host.network`` once per process, before the first container.

        The network is shared by every managed service, so it is created here
        instead of in a compose file or a quadlet.
        """
        network = self.catalog.host.network
        if not network or self._network_ready:
            return
        with self._network_lock:
            if self._network_ready:
                return
            self.runtime.ensure_network(network, self.catalog.host.network_subnet)
            self._network_ready = True

    def _container_spec(self, service: ServiceSpec, profile: Profile, *, recreate: bool) -> ContainerSpec:
        return ContainerSpec(
            name=service.container_name,
            service_id=service.id,
            mode=profile.mode,
            image=profile.image,
            command=profile.command,
            env=profile.env,
            networks=profile.networks,
            devices=profile.devices,
            binds=profile.binds,
            volumes=profile.volumes,
            ports=profile.ports,
            memory_limit_mib=profile.memory_limit_mib,
            shm_size_mib=profile.shm_size_mib,
            health_url=service.health_url,
            health_timeout_s=service.health_timeout_s,
            stop_grace_s=profile.stop_grace_s,
            recreate=recreate,
        )

    def _grace_locked(self, service_id: str) -> float:
        service = self.catalog.get(service_id)
        row = self.store.get_service(service_id)
        if row.mode and row.mode in service.profiles:
            return service.profiles[row.mode].stop_grace_s
        if service.profiles:
            return max(item.stop_grace_s for item in service.profiles.values())
        return 10.0

    def _phase(self, transition_id: str, phase: str) -> None:
        with self._cv:
            self.store.update_transition(transition_id, phase=phase)

    def _tail(self, name: str) -> str:
        try:
            text = self.runtime.logs(name, 32)
        except SwitchError:
            return ""
        return redact(text, list(self.catalog.secret_values))

    def _vram_of(self, service_id: str, used: int) -> int:
        with self._cv:
            rows = {row.id: row for row in self.store.list_services()}
        accounted = 0
        own = 0
        for service in self.catalog.services:
            row = rows.get(service.id)
            if row is None:
                continue
            weight = max(row.measured_vram_mib or 0, row.reserve_vram_mib)
            accounted += weight
            if service.id == service_id:
                own = weight
        unknown = max(0, used - accounted)
        # Prefer the reservation when this service has not been calibrated yet.
        if own:
            return own
        return max(0, used - (accounted - own) - unknown)

    def _known(self, service_id: str) -> ServiceSpec:
        try:
            return self.catalog.get(service_id)
        except KeyError as exc:
            raise SwitchError(404, "UNKNOWN_SERVICE", f"unknown service {service_id}") from exc

    def _require_profile(self, service: ServiceSpec, target: str, force: bool) -> Profile | None:
        if target == "off":
            return None
        profile = service.profiles.get(target)
        if profile is None:
            raise SwitchError(
                422,
                "STATE_NOT_SUPPORTED",
                f"{service.title} has no {target} profile",
                {"reason": f"no {target} profile is defined"},
            )
        if not profile.available and not force:
            raise SwitchError(
                422,
                "STATE_NOT_SUPPORTED",
                profile.reason or f"{target} is disabled",
                {"reason": profile.reason},
            )
        return profile

    def _normalize_options(
        self,
        service: ServiceSpec,
        target: str,
        incoming: dict[str, Any] | None,
        stored: dict[str, Any],
        profile: Profile | None,
    ) -> dict[str, Any]:
        if target == "off":
            if incoming:
                raise SwitchError(400, "BAD_OPTIONS", "off does not take options")
            return {}
        merged = dict(stored)
        if incoming is not None:
            if not isinstance(incoming, dict):
                raise SwitchError(400, "BAD_OPTIONS", "options must be an object")
            unknown = set(incoming) - {"references"}
            if unknown:
                raise SwitchError(400, "BAD_OPTIONS", f"unknown options: {sorted(unknown)}")
            if "references" in incoming:
                merged["references"] = incoming["references"]
        refs = merged.get("references", 0) or 0
        if isinstance(refs, bool) or not isinstance(refs, int):
            raise SwitchError(400, "BAD_OPTIONS", "references must be an integer")
        limit = profile.max_references if profile else 10
        if refs < 0 or refs > limit:
            raise SwitchError(400, "BAD_OPTIONS", f"references must be between 0 and {limit}")
        if profile and profile.vram_per_reference_mib == 0 and refs:
            raise SwitchError(
                400,
                "BAD_OPTIONS",
                "references is only valid for profiles that declare vram_per_reference_mib",
            )
        if refs:
            merged["references"] = refs
        else:
            merged.pop("references", None)
        del service
        return merged

    def _transition_view(self, transition_id: str) -> dict[str, Any]:
        with self._cv:
            return self._row_view(self.store.get_transition(transition_id))

    def _row_view(self, row: TransitionRow) -> dict[str, Any]:
        error = None
        if row.error_code:
            error = {"code": row.error_code, "message": row.error_detail or ""}
        return {
            "id": row.id,
            "service_id": row.service_id,
            "from": row.from_state,
            "to": row.to_state,
            "status": row.status,
            "phase": row.phase,
            "error": error,
            "forced": row.forced,
            "actor": row.actor,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "duration_ms": row.duration_ms,
            "evict": list(row.evict),
        }


def _claim(profile: Profile, options: dict[str, Any]) -> tuple[int, int]:
    refs = int(options.get("references", 0) or 0)
    return profile.claim_ram_mib, profile.claim_vram_mib + profile.vram_per_reference_mib * refs


def _later(seconds: int) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
