# SPDX-License-Identifier: Apache-2.0
"""RAM/VRAM admission.

The ledger and the live sample are two separate constraints. They are not
subtracted from each other:

* ledger free = (total - reserve) - claims of other managed services
* live free  = what the machine actually has left, counting unmanaged usage

A start is allowed only when the new claim fits in both. Eviction is a
permission list: services are stopped only when the fit requires it.

A measurement is a fact about a running container, not about a service's history:
``SwitchCore`` only passes it for a service that is ready in a mode that holds the
resource. A 27 GiB reading left over from a stopped LLM must not make the card
look full.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from switch_service.errors import SwitchError


@dataclass(frozen=True)
class Holder:
    id: str
    reserve_ram_mib: int
    reserve_vram_mib: int
    measured_ram_mib: int = 0
    measured_vram_mib: int = 0


@dataclass(frozen=True)
class Sample:
    ram_total_mib: int
    ram_available_mib: int
    vram_total_mib: int | None
    vram_used_mib: int | None
    cpu_threads: int = 0
    load1: float = 0.0
    device_index: int = 0
    device_name: str | None = None
    compute_cap: str | None = None
    sampled_at: str = ""
    source: str = "live"


@dataclass(frozen=True)
class Decision:
    ok: bool
    evict: tuple[str, ...] = ()
    error: SwitchError | None = None
    details: dict[str, Any] = field(default_factory=dict)


def vram_weight(holder: Holder) -> int:
    """VRAM a holder is accounted for: the larger of its measurement and its claim.

    A measurement describes one run of one container. ``SwitchCore`` drops it for
    a service that is not running, so a stopped service never keeps the card busy.
    """
    return max(holder.measured_vram_mib, holder.reserve_vram_mib)


def unmanaged_vram(holders: list[Holder], sample: Sample) -> int:
    """GPU memory no managed service accounts for: other processes and the driver."""
    used = sample.vram_used_mib
    if used is None:
        return 0
    return max(0, used - sum(vram_weight(holder) for holder in holders))


def live_free_vram(
    holders: list[Holder],
    sample: Sample,
    vram_reserve_mib: int,
    freeing: set[str] | None = None,
) -> int | None:
    """VRAM the host can hand out right now, counting unmanaged usage.

    ``freeing`` names holders whose footprint is released before the new claim is
    taken: the switch target (it is replaced, not added to itself) and everything
    evicted for it.
    """
    if sample.vram_total_mib is None or sample.vram_used_mib is None:
        return None
    capacity = max(0, sample.vram_total_mib - vram_reserve_mib)
    gone = freeing or set()
    held = sum(vram_weight(holder) for holder in holders if holder.id not in gone)
    return capacity - (unmanaged_vram(holders, sample) + held)


def live_free_ram(
    holders: list[Holder],
    sample: Sample,
    ram_reserve_mib: int,
    freeing: set[str] | None = None,
) -> tuple[int, int]:
    """``(ledger_free, live_free)`` for RAM.

    ``MemAvailable`` already excludes the RSS of running models, so their claims
    are not subtracted again; only the RSS about to be released is added back.
    """
    gone = freeing or set()
    capacity = max(0, sample.ram_total_mib - ram_reserve_mib)
    ledger = capacity - sum(
        max(0, holder.reserve_ram_mib) for holder in holders if holder.id not in gone
    )
    freed = sum(max(0, holder.measured_ram_mib) for holder in holders if holder.id in gone)
    live = sample.ram_available_mib + freed - ram_reserve_mib
    return ledger, live


def admit(
    *,
    subject: str,
    target_id: str,
    holders: list[Holder],
    known_ids: set[str],
    preemptible: dict[str, bool],
    sample: Sample,
    ram_reserve_mib: int,
    vram_reserve_mib: int,
    claim_ram_mib: int,
    claim_vram_mib: int,
    evict: list[str],
    force: bool,
) -> Decision:
    """Decide whether ``target_id`` may take ``claim_*``."""
    if claim_ram_mib < 0 or claim_vram_mib < 0:
        raise SwitchError(400, "BAD_OPTIONS", "claims must be non-negative")
    if target_id in evict:
        raise SwitchError(400, "BAD_OPTIONS", "a service cannot evict itself")
    if len(evict) != len(set(evict)):
        raise SwitchError(400, "BAD_OPTIONS", "evict ids must be unique")
    unknown = [item for item in evict if item not in known_ids]
    if unknown:
        raise SwitchError(400, "BAD_OPTIONS", f"unknown service in evict: {', '.join(unknown)}")

    # Turning a service off releases resources. It always fits.
    if claim_ram_mib == 0 and claim_vram_mib == 0:
        return Decision(ok=True, evict=())

    if claim_vram_mib > 0 and (sample.vram_total_mib is None or sample.vram_used_mib is None):
        raise SwitchError(
            503,
            "RUNTIME_UNAVAILABLE",
            "GPU accounting is unavailable (nvidia-smi returned no sample)",
        )

    def binding(chosen: set[str]) -> dict[str, Any]:
        gone = set(chosen) | {target_id}
        ledger_ram, live_ram = live_free_ram(holders, sample, ram_reserve_mib, gone)
        numbers: dict[str, Any] = {
            "ledger_ram_mib": ledger_ram,
            "live_ram_mib": live_ram,
            "ram_mib": min(ledger_ram, live_ram),
            "ram_source": "live" if live_ram < ledger_ram else "ledger",
        }
        if sample.vram_total_mib is None:
            numbers.update(
                {
                    "ledger_vram_mib": None,
                    "live_vram_mib": None,
                    "vram_mib": None,
                    "vram_source": None,
                }
            )
            return numbers
        vram_capacity = max(0, sample.vram_total_mib - vram_reserve_mib)
        ledger_vram = vram_capacity - sum(
            max(0, holder.reserve_vram_mib) for holder in holders if holder.id not in gone
        )
        live_vram = live_free_vram(holders, sample, vram_reserve_mib, gone)
        if live_vram is None:
            live_vram = ledger_vram
        numbers.update(
            {
                "ledger_vram_mib": ledger_vram,
                "live_vram_mib": live_vram,
                "vram_mib": min(ledger_vram, live_vram),
                "vram_source": "live" if live_vram < ledger_vram else "ledger",
                "unmanaged_vram_mib": unmanaged_vram(holders, sample),
            }
        )
        return numbers

    def fits(chosen: set[str]) -> bool:
        numbers = binding(chosen)
        if claim_ram_mib > numbers["ram_mib"]:
            return False
        if claim_vram_mib > 0 and claim_vram_mib > numbers["vram_mib"]:
            return False
        return True

    by_id = {holder.id: holder for holder in holders}
    chosen: list[str] = []
    if not fits(set()):
        for service_id in evict:
            holder = by_id.get(service_id)
            if holder is None or (holder.reserve_ram_mib <= 0 and holder.reserve_vram_mib <= 0):
                continue
            chosen.append(service_id)
            if fits(set(chosen)):
                break
        else:
            chosen = list(evict)
            if not fits(set(chosen)):
                details = _resource_details(
                    holders,
                    preemptible,
                    binding(set()),
                    binding(set(chosen)),
                    claim_ram_mib,
                    claim_vram_mib,
                    ram_reserve_mib,
                    vram_reserve_mib,
                    target_id,
                    _suggestions(holders, preemptible, target_id, fits),
                )
                return Decision(
                    ok=False,
                    error=_insufficient(
                        subject,
                        claim_ram_mib,
                        claim_vram_mib,
                        binding(set(chosen)),
                        details,
                    ),
                )

    for service_id in chosen:
        if not preemptible.get(service_id, False) and not force:
            raise SwitchError(
                409,
                "CANNOT_EVICT",
                f"{service_id} is not preemptible; pass force to stop it",
                {
                    "service_id": service_id,
                    "suggestions": _suggestions(holders, preemptible, target_id, fits),
                },
            )

    return Decision(ok=True, evict=tuple(chosen), details=binding(set(chosen)))


def _insufficient(
    subject: str,
    claim_ram: int,
    claim_vram: int,
    numbers: dict[str, Any],
    details: dict[str, Any],
) -> SwitchError:
    ram_short = claim_ram > numbers["ram_mib"]
    vram_short = claim_vram > 0 and numbers["vram_mib"] is not None and claim_vram > numbers["vram_mib"]
    if vram_short:
        source = numbers["vram_source"]
        available = numbers["vram_mib"]
        if source == "live":
            message = (
                f"{subject} needs {claim_vram} MiB VRAM; {available} MiB left once "
                "unmanaged GPU usage is counted."
            )
        else:
            message = (
                f"{subject} needs {claim_vram} MiB VRAM, {available} MiB free after "
                "the reserve and current claims."
            )
        details = {
            **details,
            "required_mib": claim_vram,
            "available_mib": available,
            "resource": "vram",
        }
        return SwitchError(409, "INSUFFICIENT_VRAM", message, details)
    if ram_short:
        source = numbers["ram_source"]
        available = numbers["ram_mib"]
        if source == "live":
            message = (
                f"{subject} needs {claim_ram} MiB RAM; {available} MiB left after the "
                "reserve and memory that is free right now."
            )
        else:
            message = (
                f"{subject} needs {claim_ram} MiB RAM, {available} MiB free after "
                "the reserve and current claims."
            )
        details = {
            **details,
            "required_mib": claim_ram,
            "available_mib": available,
            "resource": "ram",
        }
        return SwitchError(409, "INSUFFICIENT_RAM", message, details)
    return SwitchError(409, "INSUFFICIENT_RAM", f"{subject} does not fit", details)


def _resource_details(
    holders: list[Holder],
    preemptible: dict[str, bool],
    before: dict[str, Any],
    after: dict[str, Any],
    claim_ram: int,
    claim_vram: int,
    ram_reserve: int,
    vram_reserve: int,
    target_id: str,
    suggestions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "required_ram_mib": claim_ram,
        "required_vram_mib": claim_vram,
        "available_ram_mib": before["ram_mib"],
        "available_vram_mib": before["vram_mib"],
        "available_ram_after_evict_mib": after["ram_mib"],
        "available_vram_after_evict_mib": after["vram_mib"],
        "ram_reserve_mib": ram_reserve,
        "vram_reserve_mib": vram_reserve,
        "unmanaged_vram_mib": before.get("unmanaged_vram_mib"),
        "holders": [
            {
                "id": holder.id,
                "ram_mib": holder.reserve_ram_mib,
                "vram_mib": holder.reserve_vram_mib,
                "preemptible": preemptible.get(holder.id, False),
            }
            for holder in holders
            if holder.id != target_id
            and (holder.reserve_ram_mib > 0 or holder.reserve_vram_mib > 0)
        ],
        "suggestions": suggestions,
    }


def _suggestions(
    holders: list[Holder],
    preemptible: dict[str, bool],
    target_id: str,
    fits,
) -> list[dict[str, Any]]:
    candidates = [
        holder
        for holder in holders
        if holder.id != target_id and (holder.reserve_ram_mib > 0 or holder.reserve_vram_mib > 0)
    ]
    candidates.sort(key=lambda holder: (holder.reserve_vram_mib, holder.reserve_ram_mib), reverse=True)
    suggestions: list[dict[str, Any]] = []
    for holder in candidates:
        suggestions.append(_suggestion([holder], preemptible, fits({holder.id})))
        if len(suggestions) >= 4:
            break
    if len(candidates) >= 2 and len(suggestions) < 4:
        pair = candidates[:2]
        suggestions.append(_suggestion(pair, preemptible, fits({item.id for item in pair})))
    return suggestions


def _suggestion(holders: list[Holder], preemptible: dict[str, bool], enough: bool) -> dict[str, Any]:
    return {
        "evict": [holder.id for holder in holders],
        "frees_ram_mib": sum(holder.reserve_ram_mib for holder in holders),
        "frees_vram_mib": sum(holder.reserve_vram_mib for holder in holders),
        "requires_force": any(not preemptible.get(holder.id, False) for holder in holders),
        "enough": enough,
    }
