"""Append-only audit trail and history for the Control Deck.

One row per job state change, per denied request and per owner profile change: who (device or owner id),
what (profile, slot, action type names), when, outcome, job id and a hash of the idempotency key. Rows hold
no secrets: never a token, a nonce, the plain idempotency key, an action argument or a request body.
This module only inserts and selects; the table additionally refuses modification at the database level
(triggers created in store.py). The same connection and transaction as the state change is used, so a
state change and its audit row commit together.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

MAX_PAGE = 50
DEFAULT_PAGE = 10
_CODE = re.compile(r"^[a-z0-9_.]{1,40}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  device_id TEXT,
  event TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT,
  profile_id TEXT,
  slot INTEGER,
  action_types TEXT NOT NULL,
  job_id TEXT,
  key_hash TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_device ON audit(device_id, seq);
CREATE INDEX IF NOT EXISTS audit_by_job ON audit(job_id, seq);
"""


_SELECT = (
    "SELECT seq, ts, actor_kind, actor_id, device_id, event, outcome, reason, profile_id, slot,"
    " action_types, job_id, key_hash FROM audit"
)


def key_hash(device_id: str, key: str) -> str:
    """Stored in place of the idempotency key (scoped to the device)."""
    return hashlib.sha256(f"{device_id}\0{key}".encode()).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    ts: float
    actor_kind: str  # "device" | "owner" | "system"
    actor_id: str
    event: str  # job.<state> | request.denied | profile.put | profile.deleted
    outcome: str  # pending | succeeded | failed | cancelled | expired | unknown_outcome | denied | ok
    device_id: str | None = None  # the device the entry concerns (scopes the device's own history)
    profile_id: str | None = None
    slot: int | None = None
    action_types: tuple[str, ...] = ()
    job_id: str | None = None
    key_hash: str | None = None
    reason: str | None = None
    seq: int = 0

    def __post_init__(self) -> None:
        for value in (self.event, self.outcome, self.reason):
            if value is not None and not _CODE.match(value):
                raise ValueError("audit codes are short lowercase identifiers")


def insert_entry(conn: sqlite3.Connection, e: AuditEntry) -> int:
    cur = conn.execute(
        "INSERT INTO audit(ts, actor_kind, actor_id, device_id, event, outcome, reason, profile_id, slot,"
        " action_types, job_id, key_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            e.ts,
            e.actor_kind,
            e.actor_id,
            e.device_id,
            e.event,
            e.outcome,
            e.reason,
            e.profile_id,
            e.slot,
            ",".join(e.action_types),
            e.job_id,
            e.key_hash,
        ),
    )
    return int(cur.lastrowid or 0)


def clamp_limit(limit: int | None) -> int:
    return DEFAULT_PAGE if limit is None else max(1, min(MAX_PAGE, limit))


def fetch_page(
    conn: sqlite3.Connection,
    *,
    device_id: str | None = None,
    job_id: str | None = None,
    before: int | None = None,
    limit: int | None = None,
) -> list[AuditEntry]:
    """Newest first, at most `limit` rows. `device_id` scopes to one device (None = every entry); `before`
    is an exclusive seq."""
    where, params = ["1=1"], []
    if device_id is not None:
        where.append("device_id = ?")
        params.append(device_id)
    if job_id is not None:
        where.append("job_id = ?")
        params.append(job_id)
    if before is not None:
        where.append("seq < ?")
        params.append(before)
    sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?"  # noqa: S608 - fixed fragments
    rows = conn.execute(sql, (*params, limit or DEFAULT_PAGE)).fetchall()
    return [
        AuditEntry(
            seq=r[0],
            ts=r[1],
            actor_kind=r[2],
            actor_id=r[3],
            device_id=r[4],
            event=r[5],
            outcome=r[6],
            reason=r[7],
            profile_id=r[8],
            slot=r[9],
            action_types=tuple(t for t in r[10].split(",") if t),
            job_id=r[11],
            key_hash=r[12],
        )
        for r in rows
    ]


def entry_wire(e: AuditEntry, *, owner: bool) -> dict:
    d: dict = {"seq": e.seq, "ts": int(e.ts), "event": e.event, "outcome": e.outcome}
    optional = {
        "reason": e.reason,
        "profile_id": e.profile_id,
        "slot": e.slot,
        "actions": list(e.action_types) or None,
        "job_id": e.job_id,
    }
    if owner:
        optional |= {
            "actor": f"{e.actor_kind}:{e.actor_id}",
            "device_id": e.device_id,
            "idempotency_key_hash": e.key_hash,
        }
    d.update({k: v for k, v in optional.items() if v is not None})
    return d
