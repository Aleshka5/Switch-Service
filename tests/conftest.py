# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest

from switch_service.catalog import Catalog
from switch_service.core import SwitchCore
from switch_service.runtime import FakeRuntime
from switch_service.sample import sampler_for
from switch_service.store import Store


def service(
    service_id: str,
    *,
    gpu_vram: int = 4096,
    gpu_ram: int = 512,
    cpu_ram: int = 1024,
    preemptible: bool = True,
    cpu: bool = True,
    cpu_available: bool = True,
    reason: str | None = None,
    vram_per_reference: int = 0,
) -> dict:
    profiles: dict = {}
    if cpu:
        cpu_profile: dict = {
            "image": "localhost/demo:idle",
            "command": ["sleep", "infinity"],
            "claim": {"ram_mib": cpu_ram, "vram_mib": 0},
        }
        if not cpu_available:
            cpu_profile["available"] = False
            cpu_profile["reason"] = reason or "disabled"
        profiles["cpu"] = cpu_profile
    gpu_profile: dict = {
        "image": "localhost/demo:idle",
        "command": ["sleep", "infinity"],
        "devices": ["nvidia.com/gpu=all"],
        "claim": {"ram_mib": gpu_ram, "vram_mib": gpu_vram},
    }
    if vram_per_reference:
        gpu_profile["vram_per_reference_mib"] = vram_per_reference
    profiles["gpu"] = gpu_profile
    return {
        "id": service_id,
        "title": service_id,
        "preemptible": preemptible,
        "health": {"url": f"http://{service_id}:8080/health", "timeout_s": 1},
        "profiles": profiles,
    }


def make_core(tmp_path: Path, services: list[dict], **host_overrides) -> SwitchCore:
    host = {
        "ram_reserve_mib": 8192,
        "vram_reserve_mib": 1536,
        "sample": "fixture",
        "observe_cache_s": 0,
        "fixture": {
            "ram_total_mib": 90112,
            "ram_available_mib": 70000,
            "vram_total_mib": 32768,
            "vram_used_mib": 440,
            "cpu_threads": 16,
            "load1": 0.1,
            "device_name": "Tesla PG503-216",
            "compute_cap": "7.0",
        },
    }
    host.update(host_overrides)
    catalog = Catalog.from_dict(
        {
            "host": host,
            "defaults": {
                "health_interval_s": 0.02,
                "startup_timeout_s": 2,
                "memory_headroom_mib": 2048,
            },
            "services": services,
        }
    )
    store = Store(str(tmp_path / "state.sqlite"))
    runtime = FakeRuntime()
    return SwitchCore(
        catalog,
        store,
        runtime,
        sampler_for(catalog.host),
        synchronous=True,
        reconcile=False,
    )


@pytest.fixture
def core(tmp_path: Path) -> SwitchCore:
    built = make_core(
        tmp_path,
        [
            service("alpha"),
            service("beta", gpu_vram=8192),
            service(
                "large",
                gpu_vram=30720,
                gpu_ram=2048,
                cpu_ram=32768,
                preemptible=False,
                cpu_available=False,
            ),
        ],
    )
    yield built
    built.close()
