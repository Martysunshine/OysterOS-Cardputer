"""The Smart Home's own SQLite file (`home.db` in the data directory): an append-only audit table and the
stored answers of idempotent actions.

Audit rows say who (device or owner id), what (room, entity id, generic action, whether the device sent the
confirmation flag) and the outcome, plus a hash of the idempotency key. They never hold a token, the plain
key, the Home Assistant URL or a request body; the table refuses UPDATE and DELETE at the database level.
Replays hold the JSON answer of an executed action for 24 h (at most 1000 rows) so that a repeated
Idempotency-Key returns the stored answer instead of switching something twice.

The file is created on first use, so an installation without Home Assistant never creates it. Each operation
opens and closes its own connection (rollback journal, synchronous=FULL): nothing stays locked or open.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_NAME = "home.db"
MAX_PAGE = 50
DEFAULT_PAGE = 10
REPLAY_TTL_S = 24 * 3600
REPLAY_MAX_ROWS = 1000
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
  room_id TEXT,
  entity_id TEXT,
  action TEXT,
  confirmed INTEGER,
  key_hash TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_device ON audit(device_id, seq);
CREATE TABLE IF NOT EXISTS replays (
  device_id TEXT NOT NULL,
  key_hash TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  response TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY (device_id, key_hash)
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
"""


def key_hash(device_id: str, key: str) -> str:
    """Stored in place of the idempotency key (scoped to the device)."""
    return hashlib.sha256(f"{device_id}\0{key}".encode()).hexdigest()


def fingerprint(entity_id: str, action: str, confirmed: bool) -> str:
    return hashlib.sha256(f"{entity_id}\0{action}\0{int(confirmed)}".encode()).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    ts: float
    actor_kind: str  # "device" | "owner"
    actor_id: str
    event: str  # action | denied
    outcome: str  # ok | failed | unknown_outcome | replayed | denied
    device_id: str | None = None
    reason: str | None = None
    room_id: str | None = None
    entity_id: str | None = None
    action: str | None = None
    confirmed: bool | None = None
    key_hash: str | None = None
    seq: int = 0

    def __post_init__(self) -> None:
        for value in (self.event, self.outcome, self.reason, self.action):
            if value is not None and not _CODE.match(value):
                raise ValueError("audit codes are short lowercase identifiers")


_SELECT = (
    "SELECT seq, ts, actor_kind, actor_id, device_id, event, outcome, reason, room_id, entity_id, action,"
    " confirmed, key_hash FROM audit"
)


def entry_wire(e: AuditEntry) -> dict:
    d: dict = {
        "seq": e.seq,
        "ts": int(e.ts),
        "actor": f"{e.actor_kind}:{e.actor_id}",
        "event": e.event,
        "outcome": e.outcome,
    }
    optional = {
        "device_id": e.device_id,
        "reason": e.reason,
        "room_id": e.room_id,
        "entity_id": e.entity_id,
        "action": e.action,
        "confirmed": e.confirmed,
        "idempotency_key_hash": e.key_hash,
    }
    d.update({k: v for k, v in optional.items() if v is not None})
    return d


class HomeStore:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._ready:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path, timeout=5.0, isolation_level=None)) as conn:
                conn.execute("PRAGMA synchronous=FULL")
                if not self._ready:
                    conn.executescript(SCHEMA)
                    self._ready = True
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")

    # ---- audit ---------------------------------------------------------------------------------------

    def add_audit(self, e: AuditEntry) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO audit(ts, actor_kind, actor_id, device_id, event, outcome, reason, room_id,"
                " entity_id, action, confirmed, key_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    e.ts,
                    e.actor_kind,
                    e.actor_id,
                    e.device_id,
                    e.event,
                    e.outcome,
                    e.reason,
                    e.room_id,
                    e.entity_id,
                    e.action,
                    None if e.confirmed is None else int(e.confirmed),
                    e.key_hash,
                ),
            )
            return int(cur.lastrowid or 0)

    def audit_page(
        self, *, device_id: str | None = None, before: int | None = None, limit: int | None = None
    ) -> tuple[list[AuditEntry], int | None]:
        """Newest first. Returns the rows and the `before` cursor of the next page (None at the end)."""
        n = DEFAULT_PAGE if limit is None else max(1, min(MAX_PAGE, limit))
        where, params = ["1=1"], []
        if device_id is not None:
            where.append("device_id = ?")
            params.append(device_id)
        if before is not None:
            where.append("seq < ?")
            params.append(before)
        sql = f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?"  # noqa: S608 - fixed fragments
        with self._tx() as conn:
            rows = conn.execute(sql, (*params, n + 1)).fetchall()
        entries = [
            AuditEntry(
                seq=r[0],
                ts=r[1],
                actor_kind=r[2],
                actor_id=r[3],
                device_id=r[4],
                event=r[5],
                outcome=r[6],
                reason=r[7],
                room_id=r[8],
                entity_id=r[9],
                action=r[10],
                confirmed=None if r[11] is None else bool(r[11]),
                key_hash=r[12],
            )
            for r in rows[:n]
        ]
        return entries, (entries[-1].seq if len(rows) > n and entries else None)

    # ---- replays -------------------------------------------------------------------------------------

    def get_replay(self, device_id: str, khash: str, now: float) -> tuple[str, str] | None:
        """(fingerprint, stored JSON answer) of an executed action with this key, if still kept."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT fingerprint, response FROM replays WHERE device_id=? AND key_hash=? AND ts>?",
                (device_id, khash, now - REPLAY_TTL_S),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def put_replay(self, device_id: str, khash: str, fp: str, response: str, now: float) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO replays(device_id, key_hash, fingerprint, response, ts)"
                " VALUES (?,?,?,?,?)",
                (device_id, khash, fp, response, now),
            )
            conn.execute("DELETE FROM replays WHERE ts <= ?", (now - REPLAY_TTL_S,))
            conn.execute(
                "DELETE FROM replays WHERE rowid IN"
                " (SELECT rowid FROM replays ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (REPLAY_MAX_ROWS,),
            )
