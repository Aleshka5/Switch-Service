# SPDX-License-Identifier: Apache-2.0
"""SQLite persistence for desired state, reservations and the audit log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from switch_service.util import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
  id TEXT PRIMARY KEY,
  desired_state TEXT NOT NULL,
  state TEXT NOT NULL,
  mode TEXT,
  config_hash TEXT,
  measured_ram_mib INTEGER,
  measured_vram_mib INTEGER,
  measured_at TEXT,
  reserve_ram_mib INTEGER NOT NULL DEFAULT 0,
  reserve_vram_mib INTEGER NOT NULL DEFAULT 0,
  options_json TEXT,
  last_error TEXT,
  fail_sticky INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
  id TEXT PRIMARY KEY,
  service_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  status TEXT NOT NULL,
  phase TEXT,
  error_code TEXT,
  error_detail TEXT,
  forced INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  evict_json TEXT,
  options_json TEXT,
  from_options_json TEXT,
  recreate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transitions_service_time
  ON transitions(service_id, started_at DESC);

CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  service_id TEXT,
  detail TEXT
);
"""


@dataclass
class ServiceRow:
    id: str
    desired_state: str
    state: str
    mode: str | None
    config_hash: str | None
    measured_ram_mib: int | None
    measured_vram_mib: int | None
    measured_at: str | None
    reserve_ram_mib: int
    reserve_vram_mib: int
    options: dict[str, Any]
    last_error: str | None
    fail_sticky: bool
    retry_count: int
    next_retry_at: str | None
    updated_at: str


@dataclass
class TransitionRow:
    id: str
    service_id: str
    from_state: str
    to_state: str
    status: str
    phase: str | None
    error_code: str | None
    error_detail: str | None
    forced: bool
    actor: str
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    evict: list[str]
    options: dict[str, Any]
    from_options: dict[str, Any]
    recreate: bool = False


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def ensure_service(self, service_id: str) -> None:
        now = utcnow()
        self.conn.execute(
            """
            INSERT INTO services (id, desired_state, state, updated_at)
            VALUES (?, 'off', 'off', ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (service_id, now),
        )
        self.conn.commit()

    def get_service(self, service_id: str) -> ServiceRow:
        row = self.conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        if row is None:
            raise KeyError(service_id)
        return _service(row)

    def list_services(self) -> list[ServiceRow]:
        rows = self.conn.execute("SELECT * FROM services ORDER BY id").fetchall()
        return [_service(row) for row in rows]

    def update_service(self, service_id: str, **fields: Any) -> None:
        if "options" in fields:
            fields["options_json"] = json.dumps(fields.pop("options"), sort_keys=True)
        if "fail_sticky" in fields:
            fields["fail_sticky"] = 1 if fields["fail_sticky"] else 0
        allowed = {
            "desired_state",
            "state",
            "mode",
            "config_hash",
            "measured_ram_mib",
            "measured_vram_mib",
            "measured_at",
            "reserve_ram_mib",
            "reserve_vram_mib",
            "options_json",
            "last_error",
            "fail_sticky",
            "retry_count",
            "next_retry_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"unknown service fields: {sorted(unknown)}")
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [service_id]
        self.conn.execute(f"UPDATE services SET {assignments} WHERE id = ?", values)
        self.conn.commit()

    def insert_transition(
        self,
        *,
        transition_id: str,
        service_id: str,
        from_state: str,
        to_state: str,
        actor: str,
        forced: bool,
        evict: list[str],
        options: dict[str, Any],
        from_options: dict[str, Any],
        recreate: bool = False,
    ) -> TransitionRow:
        started = utcnow()
        self.conn.execute(
            """
            INSERT INTO transitions (
              id, service_id, from_state, to_state, status, phase, forced, actor,
              started_at, evict_json, options_json, from_options_json, recreate
            ) VALUES (?, ?, ?, ?, 'running', 'admission', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                service_id,
                from_state,
                to_state,
                1 if forced else 0,
                actor,
                started,
                json.dumps(evict),
                json.dumps(options, sort_keys=True),
                json.dumps(from_options, sort_keys=True),
                1 if recreate else 0,
            ),
        )
        self.conn.commit()
        return self.get_transition(transition_id)

    def get_transition(self, transition_id: str) -> TransitionRow:
        row = self.conn.execute(
            "SELECT * FROM transitions WHERE id = ?", (transition_id,)
        ).fetchone()
        if row is None:
            raise KeyError(transition_id)
        return _transition(row)

    def update_transition(self, transition_id: str, **fields: Any) -> None:
        allowed = {"phase", "status", "error_code", "error_detail", "finished_at", "duration_ms"}
        unknown = set(fields) - allowed
        if unknown:
            raise KeyError(f"unknown transition fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [transition_id]
        self.conn.execute(f"UPDATE transitions SET {assignments} WHERE id = ?", values)
        self.conn.commit()

    def active_transition(self, service_id: str) -> TransitionRow | None:
        row = self.conn.execute(
            """
            SELECT * FROM transitions
            WHERE service_id = ? AND status = 'running'
            ORDER BY started_at DESC LIMIT 1
            """,
            (service_id,),
        ).fetchone()
        return _transition(row) if row else None

    def running_transitions(self) -> list[TransitionRow]:
        rows = self.conn.execute(
            "SELECT * FROM transitions WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
        return [_transition(row) for row in rows]

    def list_transitions(self, limit: int) -> list[TransitionRow]:
        rows = self.conn.execute(
            "SELECT * FROM transitions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_transition(row) for row in rows]

    def add_audit(self, actor: str, action: str, service_id: str | None, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO audit (at, actor, action, service_id, detail) VALUES (?, ?, ?, ?, ?)",
            (utcnow(), actor, action, service_id, detail),
        )
        self.conn.commit()


def _loads(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _service(row: sqlite3.Row) -> ServiceRow:
    mode = row["mode"] or None
    return ServiceRow(
        id=row["id"],
        desired_state=row["desired_state"],
        state=row["state"],
        mode=mode,
        config_hash=row["config_hash"],
        measured_ram_mib=row["measured_ram_mib"],
        measured_vram_mib=row["measured_vram_mib"],
        measured_at=row["measured_at"],
        reserve_ram_mib=row["reserve_ram_mib"] or 0,
        reserve_vram_mib=row["reserve_vram_mib"] or 0,
        options=_loads(row["options_json"]),
        last_error=row["last_error"],
        fail_sticky=bool(row["fail_sticky"]),
        retry_count=row["retry_count"] or 0,
        next_retry_at=row["next_retry_at"],
        updated_at=row["updated_at"],
    )


def _transition(row: sqlite3.Row) -> TransitionRow:
    evict = json.loads(row["evict_json"] or "[]")
    return TransitionRow(
        id=row["id"],
        service_id=row["service_id"],
        from_state=row["from_state"] or "off",
        to_state=row["to_state"],
        status=row["status"],
        phase=row["phase"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        forced=bool(row["forced"]),
        actor=row["actor"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        evict=list(evict),
        options=_loads(row["options_json"]),
        from_options=_loads(row["from_options_json"]),
        recreate=bool(row["recreate"]) if "recreate" in row.keys() else False,
    )
