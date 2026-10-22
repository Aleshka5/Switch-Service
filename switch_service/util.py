# SPDX-License-Identifier: Apache-2.0
"""Small helpers shared by the control plane."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

_SECRET_ASSIGN = re.compile(
    r"(?i)((?:api[-_]?key|hf_token|authorization|bearer|token)\s*[:=]\s*(?:bearer\s+)?)\S+"
)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_ms(started_at: str, finished_at: str) -> int:
    start = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    end = datetime.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return max(0, int((end - start).total_seconds() * 1000))


def redact(text: str, secrets: list[str]) -> str:
    """Mask known secret values and common credential assignments."""
    cleaned = text
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, "…[redacted]…")
    return _SECRET_ASSIGN.sub(r"\1…[redacted]…", cleaned)


def canonical_hash(payload: dict[str, Any]) -> str:
    import hashlib

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def other_vertices(state: str) -> list[str]:
    """The two triangle edges that leave ``state``."""
    if state == "off":
        return ["cpu", "gpu"]
    if state == "cpu":
        return ["off", "gpu"]
    if state == "gpu":
        return ["off", "cpu"]
    raise ValueError(f"not a vertex: {state}")
