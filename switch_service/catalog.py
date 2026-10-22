# SPDX-License-Identifier: Apache-2.0
"""YAML catalogue of managed inference services."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from switch_service.errors import CatalogError

_ID = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MODES = ("cpu", "gpu")


@dataclass
class Fixture:
    ram_total_mib: int
    ram_available_mib: int
    vram_total_mib: int
    vram_used_mib: int
    cpu_threads: int = 1
    load1: float = 0.0
    device_name: str = "fixture-gpu"
    compute_cap: str = "0.0"


@dataclass(frozen=True)
class Profile:
    mode: str
    available: bool
    reason: str | None
    image: str
    command: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    networks: tuple[str, ...]
    devices: tuple[str, ...]
    binds: tuple[tuple[str, str, bool], ...]
    volumes: tuple[tuple[str, str, bool], ...]
    ports: tuple[tuple[int, int], ...]
    memory_limit_mib: int
    shm_size_mib: int | None
    claim_ram_mib: int
    claim_vram_mib: int
    vram_per_reference_mib: int
    max_references: int
    calibrate_vram: bool
    startup_timeout_s: float
    stop_grace_s: float


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    title: str
    kind: str
    container_name: str
    preemptible: bool
    consumers: tuple[str, ...]
    notes: tuple[str, ...]
    health_url: str | None
    health_timeout_s: float
    health_interval_s: float
    profiles: dict[str, Profile]

    @property
    def supported_states(self) -> list[str]:
        return ["off", *[mode for mode in _MODES if mode in self.profiles]]

    def unavailable(self) -> dict[str, dict[str, str]]:
        missing = {
            mode: {"reason": f"no {mode} profile is defined"}
            for mode in _MODES
            if mode not in self.profiles
        }
        disabled = {
            profile.mode: {"reason": profile.reason or "disabled"}
            for profile in self.profiles.values()
            if not profile.available
        }
        return {**missing, **disabled}


@dataclass(frozen=True)
class HostSpec:
    ram_reserve_mib: int
    vram_reserve_mib: int
    vram_device: int
    reconcile_interval_s: float
    observe_cache_s: float
    sample: str
    fixture: Fixture | None


@dataclass
class Catalog:
    host: HostSpec
    services: tuple[ServiceSpec, ...]
    secret_values: tuple[str, ...] = field(default_factory=tuple)
    path: str | None = None

    def get(self, service_id: str) -> ServiceSpec:
        for service in self.services:
            if service.id == service_id:
                return service
        raise KeyError(service_id)

    @classmethod
    def load(cls, path: str, secrets_file: str | None = None) -> Catalog:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if not isinstance(raw, dict):
            raise CatalogError("catalog root must be a mapping")
        env = _environment(secrets_file)
        secrets: set[str] = set()
        substituted = _walk(raw, env, secrets)
        catalog = cls.from_dict(substituted)
        catalog.secret_values = tuple(sorted(s for s in secrets if len(s) >= 4))
        catalog.path = path
        return catalog

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Catalog:
        if not isinstance(raw, dict):
            raise CatalogError("catalog root must be a mapping")
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise CatalogError("defaults must be a mapping")
        host = _host(raw.get("host") or {})
        items = raw.get("services")
        if not isinstance(items, list) or not items:
            raise CatalogError("services must be a non-empty list")
        services = tuple(_service(item, defaults) for item in items)
        ids = [service.id for service in services]
        if len(ids) != len(set(ids)):
            raise CatalogError("service ids must be unique")
        names = [service.container_name for service in services]
        if len(names) != len(set(names)):
            raise CatalogError("container names must be unique")
        return cls(host=host, services=services)


def _environment(secrets_file: str | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = secrets_file or os.environ.get("SWITCH_SECRETS", "")
    if path:
        if not os.path.exists(path):
            raise CatalogError(f"secrets file not found: {path}")
        env.update(_parse_env_file(path))
    env.update(os.environ)
    return env


def _parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("export "):
                text = text[len("export ") :]
            if "=" not in text:
                continue
            key, value = text.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _walk(node: Any, env: dict[str, str], secrets: set[str]) -> Any:
    if isinstance(node, str):
        return _substitute(node, env, secrets)
    if isinstance(node, list):
        return [_walk(item, env, secrets) for item in node]
    if isinstance(node, dict):
        return {key: _walk(value, env, secrets) for key, value in node.items()}
    return node


def _substitute(value: str, env: dict[str, str], secrets: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in env or env[key] == "":
            raise CatalogError(f"missing environment variable {key}")
        secrets.add(env[key])
        return env[key]

    return _VAR.sub(replace, value)


def _host(raw: dict[str, Any]) -> HostSpec:
    sample = str(raw.get("sample", "live"))
    if sample not in {"live", "fixture"}:
        raise CatalogError("host.sample must be 'live' or 'fixture'")
    fixture = None
    if sample == "fixture":
        block = raw.get("fixture")
        if not isinstance(block, dict):
            raise CatalogError("host.fixture is required when sample is fixture")
        fixture = Fixture(
            ram_total_mib=_positive(block, "ram_total_mib", zero=False),
            ram_available_mib=_nonneg(block, "ram_available_mib"),
            vram_total_mib=_positive(block, "vram_total_mib", zero=False),
            vram_used_mib=_nonneg(block, "vram_used_mib"),
            cpu_threads=int(block.get("cpu_threads", 1)),
            load1=float(block.get("load1", 0.0)),
            device_name=str(block.get("device_name", "fixture-gpu")),
            compute_cap=str(block.get("compute_cap", "0.0")),
        )
    return HostSpec(
        ram_reserve_mib=_nonneg(raw, "ram_reserve_mib"),
        vram_reserve_mib=_nonneg(raw, "vram_reserve_mib"),
        vram_device=int(raw.get("vram_device", 0)),
        reconcile_interval_s=float(raw.get("reconcile_interval_s", 15)),
        observe_cache_s=float(raw.get("observe_cache_s", 2)),
        sample=sample,
        fixture=fixture,
    )


def _service(raw: dict[str, Any], defaults: dict[str, Any]) -> ServiceSpec:
    if not isinstance(raw, dict):
        raise CatalogError("each service must be a mapping")
    service_id = _ident(raw.get("id"), "service id")
    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise CatalogError(f"{service_id}: profiles must be a non-empty mapping")
    unknown = set(profiles_raw) - set(_MODES)
    if unknown:
        raise CatalogError(f"{service_id}: unknown profiles {sorted(unknown)}")
    health = raw.get("health") or {}
    if health and not isinstance(health, dict):
        raise CatalogError(f"{service_id}: health must be a mapping")
    health_url = health.get("url")
    profiles = {
        mode: _profile(service_id, mode, body, defaults, health_url)
        for mode, body in profiles_raw.items()
    }
    name = str(raw.get("container_name") or service_id)
    if not _ID.match(name):
        raise CatalogError(f"{service_id}: container_name {name!r} is not a safe name")
    return ServiceSpec(
        id=service_id,
        title=str(raw.get("title") or service_id),
        kind=str(raw.get("kind") or "model"),
        container_name=name,
        preemptible=bool(raw.get("preemptible", True)),
        consumers=tuple(str(item) for item in raw.get("consumers") or []),
        notes=tuple(str(item) for item in raw.get("notes") or []),
        health_url=str(health_url) if health_url else None,
        health_timeout_s=float(health.get("timeout_s", defaults.get("health_timeout_s", 2))),
        health_interval_s=float(defaults.get("health_interval_s", 0.5)),
        profiles=profiles,
    )


def _profile(
    service_id: str,
    mode: str,
    raw: dict[str, Any],
    defaults: dict[str, Any],
    health_url: str | None,
) -> Profile:
    del health_url
    if not isinstance(raw, dict):
        raise CatalogError(f"{service_id}.{mode} must be a mapping")
    claim = raw.get("claim") or {}
    if not isinstance(claim, dict):
        raise CatalogError(f"{service_id}.{mode}: claim must be a mapping")
    claim_ram = _nonneg(claim, "ram_mib")
    claim_vram = _nonneg(claim, "vram_mib")
    headroom = int(defaults.get("memory_headroom_mib", 2048))
    if "memory_limit_mib" in raw:
        memory_limit = _positive(raw, "memory_limit_mib", zero=False)
    else:
        memory_limit = claim_ram + headroom
    available = bool(raw.get("available", True))
    reason = raw.get("reason")
    if not available and not reason:
        raise CatalogError(f"{service_id}.{mode}: available false requires reason")
    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise CatalogError(f"{service_id}.{mode}: env must be a mapping")
    command = tuple(str(part) for part in raw.get("command") or [])
    image = str(raw.get("image") or "")
    if not image:
        raise CatalogError(f"{service_id}.{mode}: image is required")
    return Profile(
        mode=mode,
        available=available,
        reason=str(reason) if reason else None,
        image=image,
        command=command,
        env=tuple((str(key), str(value)) for key, value in env_raw.items()),
        networks=tuple(str(item) for item in raw.get("networks") or []),
        devices=tuple(str(item) for item in raw.get("devices") or []),
        binds=_binds(service_id, raw.get("volumes") or []),
        volumes=_named_volumes(service_id, raw.get("volumes") or []),
        ports=tuple(_port(service_id, item) for item in raw.get("ports") or []),
        memory_limit_mib=memory_limit,
        shm_size_mib=_size_mib(raw["shm_size"]) if "shm_size" in raw else None,
        claim_ram_mib=claim_ram,
        claim_vram_mib=claim_vram,
        vram_per_reference_mib=int(raw.get("vram_per_reference_mib", 0)),
        max_references=int(raw.get("max_references", defaults.get("max_references", 10))),
        calibrate_vram=bool(raw.get("calibrate_vram", True)),
        startup_timeout_s=float(raw.get("startup_timeout_s", defaults.get("startup_timeout_s", 30))),
        stop_grace_s=float(raw.get("stop_grace_s", defaults.get("stop_grace_s", 10))),
    )


def _binds(service_id: str, volumes: list[Any]) -> tuple[tuple[str, str, bool], ...]:
    parsed = []
    for item in volumes:
        source, dest, read_only, kind = _volume(service_id, str(item))
        if kind == "bind":
            parsed.append((source, dest, read_only))
    return tuple(parsed)


def _named_volumes(service_id: str, volumes: list[Any]) -> tuple[tuple[str, str, bool], ...]:
    parsed = []
    for item in volumes:
        source, dest, read_only, kind = _volume(service_id, str(item))
        if kind == "volume":
            parsed.append((source, dest, read_only))
    return tuple(parsed)


def _volume(service_id: str, text: str) -> tuple[str, str, bool, str]:
    parts = text.split(":")
    if len(parts) == 2:
        source, dest, mode = parts[0], parts[1], "rw"
    elif len(parts) == 3:
        source, dest, mode = parts
    else:
        raise CatalogError(f"{service_id}: bad volume {text!r}")
    if mode not in {"ro", "rw"}:
        raise CatalogError(f"{service_id}: volume mode must be ro or rw ({text})")
    if not dest.startswith("/"):
        raise CatalogError(f"{service_id}: volume destination must be absolute ({text})")
    if not source:
        raise CatalogError(f"{service_id}: volume source is empty ({text})")
    kind = "bind" if source.startswith("/") else "volume"
    return source, dest, mode == "ro", kind


def _port(service_id: str, text: str) -> tuple[int, int]:
    parts = str(text).split(":")
    if len(parts) == 2:
        host, container = parts
    elif len(parts) == 3:
        host, container = parts[1], parts[2]
    else:
        raise CatalogError(f"{service_id}: bad port {text!r}")
    try:
        host_port, container_port = int(host), int(container)
    except ValueError as exc:
        raise CatalogError(f"{service_id}: bad port {text!r}") from exc
    if not (1 <= host_port <= 65535 and 1 <= container_port <= 65535):
        raise CatalogError(f"{service_id}: port out of range {text}")
    return host_port, container_port


def _size_mib(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text.endswith("gib"):
        return int(float(text[:-3]) * 1024)
    if text.endswith("g"):
        return int(float(text[:-1]) * 1024)
    if text.endswith("mib"):
        return int(float(text[:-3]))
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(text)


def _ident(value: Any, label: str) -> str:
    text = str(value or "")
    if not _ID.match(text):
        raise CatalogError(f"{label} {text!r} must match {_ID.pattern}")
    return text


def _nonneg(raw: dict[str, Any], key: str) -> int:
    if key not in raw:
        raise CatalogError(f"missing {key}")
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogError(f"{key} must be an integer >= 0")
    return value


def _positive(raw: dict[str, Any], key: str, *, zero: bool) -> int:
    value = _nonneg(raw, key)
    if not zero and value <= 0:
        raise CatalogError(f"{key} must be > 0")
    return value
