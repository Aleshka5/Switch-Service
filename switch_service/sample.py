# SPDX-License-Identifier: Apache-2.0
"""Host RAM/VRAM samples. Fixture mode is for laptops and tests; live mode reads the host."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass

from switch_service.admission import Sample
from switch_service.catalog import Fixture, HostSpec
from switch_service.util import utcnow


def parse_meminfo(text: str) -> tuple[int, int]:
    """Return ``(total_mib, available_mib)`` from ``/proc/meminfo`` text."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        number = rest.strip().split()[0]
        values[key] = int(number)
    # meminfo reports kibibytes.
    return values["MemTotal"] // 1024, values["MemAvailable"] // 1024


def parse_nvidia_csv(text: str) -> tuple[int, int, str, str]:
    """Parse one csv line: total, used, name, compute capability."""
    line = text.strip().splitlines()[0]
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 4:
        raise ValueError(f"unexpected nvidia-smi line: {line!r}")
    total = int(float(parts[0]))
    used = int(float(parts[1]))
    cap = parts[-1]
    name = ",".join(parts[2:-1]).strip()
    return total, used, name, cap


@dataclass
class FixtureSampler:
    fixture: Fixture
    device_index: int = 0

    def sample(self) -> Sample:
        return Sample(
            ram_total_mib=self.fixture.ram_total_mib,
            ram_available_mib=self.fixture.ram_available_mib,
            vram_total_mib=self.fixture.vram_total_mib,
            vram_used_mib=self.fixture.vram_used_mib,
            cpu_threads=self.fixture.cpu_threads,
            load1=self.fixture.load1,
            device_index=self.device_index,
            device_name=self.fixture.device_name,
            compute_cap=self.fixture.compute_cap,
            sampled_at=utcnow(),
            source="fixture",
        )


class LiveSampler:
    def __init__(self, device_index: int = 0) -> None:
        self.device_index = device_index

    def sample(self) -> Sample:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            total, available = parse_meminfo(handle.read())
        try:
            load1 = float(open("/proc/loadavg", encoding="utf-8").read().split()[0])
        except (OSError, ValueError, IndexError):
            load1 = 0.0
        vram_total = None
        vram_used = None
        name = None
        cap = None
        reading = _nvidia(self.device_index)
        if reading is not None:
            vram_total, vram_used, name, cap = reading
        return Sample(
            ram_total_mib=total,
            ram_available_mib=available,
            vram_total_mib=vram_total,
            vram_used_mib=vram_used,
            cpu_threads=os.cpu_count() or 1,
            load1=load1,
            device_index=self.device_index,
            device_name=name,
            compute_cap=cap,
            sampled_at=utcnow(),
            source="live",
        )


class CachingSampler:
    def __init__(self, inner: FixtureSampler | LiveSampler, ttl_s: float) -> None:
        self.inner = inner
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._cached: Sample | None = None
        self._at = 0.0

    def sample(self) -> Sample:
        if self.ttl_s <= 0:
            return self.inner.sample()
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now - self._at < self.ttl_s:
                return self._cached
            sample = self.inner.sample()
            self._cached = sample
            self._at = now
            return sample


def sampler_for(host: HostSpec) -> CachingSampler:
    if host.sample == "fixture":
        if host.fixture is None:
            raise RuntimeError("fixture sample requested without fixture numbers")
        inner: FixtureSampler | LiveSampler = FixtureSampler(host.fixture, host.vram_device)
    else:
        inner = LiveSampler(host.vram_device)
    return CachingSampler(inner, host.observe_cache_s)


def _nvidia(device: int) -> tuple[int, int, str, str] | None:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                f"--id={device}",
                "--query-gpu=memory.total,memory.used,name,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return parse_nvidia_csv(completed.stdout)
    except (ValueError, IndexError):
        return None
