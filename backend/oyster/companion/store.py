"""The companion's own SQLite file (`companion.db` in the data directory), separate from oyster.db.

One connection in WAL mode behind a lock, a busy timeout, and idempotent CREATE ... IF NOT EXISTS tables
(same approach as oyster/db/sqlite_store.py). Holds: enrolled hosts, pairing codes (salted hashes only) and
the append-only audit log. The audit table is protected twice: no statement in this package modifies or
removes its rows, and triggers abort any UPDATE or DELETE on it.
Device tokens are never stored here: they are created and kept by the main store.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from oyster.companion.models import AuditEntry, HostRecord
from oyster.store import Conflict

log = logging.getLogger("oyster.companion")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hosts (
  host_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  adapter TEXT NOT NULL,
  metrics TEXT NOT NULL,
  services TEXT NOT NULL,
  device_ids TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing (
  pairing_id TEXT PRIMARY KEY,
  code_salt TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  bound_device_id TEXT,
  simulated INTEGER NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  failed_attempts INTEGER NOT NULL DEFAULT 0,
  redeemed_at REAL,
  burned_at REAL,
  device_id TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_u BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_is_append_only_d BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


@dataclass(frozen=True)
class PairingRow:
    pairing_id: str
    code_salt: str
    code_hash: str
    bound_device_id: str | None
    simulated: bool
    created_at: float
    expires_at: float
    failed_attempts: int
    redeemed_at: float | None
    burned_at: float | None
    device_id: str | None


def _host(row: sqlite3.Row) -> HostRecord:
    return HostRecord(
        host_id=row["host_id"],
        display_name=row["display_name"],
        adapter=row["adapter"],
        metrics=json.loads(row["metrics"]),
        services=json.loads(row["services"]),
        device_ids=json.loads(row["device_ids"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class CompanionStore:
    def __init__(self, db_path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                try:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    log.exception("rollback failed")
                raise

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # --- hosts ---
    def put_host(self, host: HostRecord, max_hosts: int) -> bool:
        """Insert or replace (keeping created_at). True when the host is new. Conflict at the host cap."""
        with self._tx() as db:
            exists = db.execute("SELECT 1 FROM hosts WHERE host_id=?", (host.host_id,)).fetchone()
            if exists is None and db.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] >= max_hosts:
                raise Conflict("conflict", f"at most {max_hosts} hosts can be enrolled")
            db.execute(
                "INSERT INTO hosts(host_id, display_name, adapter, metrics, services, device_ids,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET"
                " display_name=excluded.display_name, adapter=excluded.adapter, metrics=excluded.metrics,"
                " services=excluded.services, device_ids=excluded.device_ids,"
                " updated_at=excluded.updated_at",
                (
                    host.host_id,
                    host.display_name,
                    host.adapter,
                    json.dumps(host.metrics),
                    json.dumps(host.services),
                    json.dumps(host.device_ids),
                    host.created_at,
                    host.updated_at,
                ),
            )
        return exists is None

    def get_host(self, host_id: str) -> HostRecord | None:
        rows = self._q("SELECT * FROM hosts WHERE host_id=?", (host_id,))
        return _host(rows[0]) if rows else None

    def list_hosts(self) -> list[HostRecord]:
        return [_host(r) for r in self._q("SELECT * FROM hosts ORDER BY host_id")]

    def delete_host(self, host_id: str) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM hosts WHERE host_id=?", (host_id,)).rowcount == 1

    # --- pairing (every state change is one conditional statement inside a write transaction) ---
    def create_pairing(self, row: PairingRow) -> None:
        """Store a new code; any code still outstanding is burned (one live code at a time)."""
        with self._tx() as db:
            db.execute(
                "UPDATE pairing SET burned_at=? WHERE redeemed_at IS NULL AND burned_at IS NULL",
                (row.created_at,),
            )
            db.execute(
                "INSERT INTO pairing(pairing_id, code_salt, code_hash, bound_device_id, simulated,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                (
                    row.pairing_id,
                    row.code_salt,
                    row.code_hash,
                    row.bound_device_id,
                    int(row.simulated),
                    row.created_at,
                    row.expires_at,
                ),
            )

    def latest_pairing(self) -> PairingRow | None:
        rows = self._q("SELECT * FROM pairing ORDER BY created_at DESC, rowid DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return PairingRow(
            pairing_id=r["pairing_id"],
            code_salt=r["code_salt"],
            code_hash=r["code_hash"],
            bound_device_id=r["bound_device_id"],
            simulated=bool(r["simulated"]),
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            failed_attempts=r["failed_attempts"],
            redeemed_at=r["redeemed_at"],
            burned_at=r["burned_at"],
            device_id=r["device_id"],
        )

    def record_pairing_failure(self, pairing_id: str, now: float, limit: int) -> tuple[int, bool]:
        """Count one failed attempt against a live code; burn it at `limit`. Returns (count, burned)."""
        with self._tx() as db:
            db.execute(
                "UPDATE pairing SET failed_attempts=failed_attempts+1 WHERE pairing_id=?"
                " AND redeemed_at IS NULL AND burned_at IS NULL",
                (pairing_id,),
            )
            count = db.execute(
                "SELECT failed_attempts FROM pairing WHERE pairing_id=?", (pairing_id,)
            ).fetchone()[0]
            burned = False
            if count >= limit:
                burned = (
                    db.execute(
                        "UPDATE pairing SET burned_at=? WHERE pairing_id=? AND burned_at IS NULL"
                        " AND redeemed_at IS NULL",
                        (now, pairing_id),
                    ).rowcount
                    == 1
                )
            return count, burned

    def claim_pairing(self, pairing_id: str, device_id: str, now: float) -> bool:
        """Single use: True for exactly one caller, and only while the code is live."""
        with self._tx() as db:
            return (
                db.execute(
                    "UPDATE pairing SET redeemed_at=?, device_id=? WHERE pairing_id=?"
                    " AND redeemed_at IS NULL AND burned_at IS NULL AND expires_at > ?",
                    (now, device_id, pairing_id, now),
                ).rowcount
                == 1
            )

    # --- audit (insert and select only) ---
    def audit_append(self, ts: float, actor: str, action: str, target: str, outcome: str, detail: str) -> int:
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO audit(ts, actor, action, target, outcome, detail) VALUES (?,?,?,?,?,?)",
                (ts, actor, action, target, outcome, detail),
            )
            return int(cur.lastrowid or 0)

    def audit_list(self, limit: int, before_id: int | None = None) -> list[AuditEntry]:
        """Newest first. `before_id` pages backwards."""
        if before_id is None:
            rows = self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._q("SELECT * FROM audit WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit))
        return [
            AuditEntry(
                id=r["id"],
                ts=r["ts"],
                actor=r["actor"],
                action=r["action"],
                target=r["target"],
                outcome=r["outcome"],
                detail=r["detail"],
            )
            for r in rows
        ]
