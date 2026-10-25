"""The Claude Code link's own SQLite file (`claude.db` in the data directory).

Tables: approvals (one row per tool call waiting for an answer), nonces (one per device and approval,
issued when that device lists the approval), status (the last few sessions' usage line) and an append-only
audit table. Same style as the Control Deck store: one connection in WAL mode behind an RLock, every write
path in a BEGIN IMMEDIATE transaction, idempotent table creation. Two guards live in the schema itself:
`state` is limited to the known set, and a row can be `allowed` or `denied` only when it names the device
that decided (a CHECK), so an allow without a recorded device decision cannot even be stored. The audit
table refuses UPDATE and DELETE (triggers).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from oyster.claude.models import DECIDED, STATES, Approval, Status

log = logging.getLogger("oyster.claude")

SCHEMA_VERSION = "1"
DB_NAME = "claude.db"
MAX_PAGE = 50
DEFAULT_PAGE = 10
_CODE = re.compile(r"^[a-z0-9_.]{1,40}$")

_STATE_LIST = ",".join(f"'{s}'" for s in STATES)
_DECIDED_LIST = ",".join(f"'{s}'" for s in DECIDED)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  approval_id TEXT NOT NULL UNIQUE,
  session TEXT NOT NULL,
  tool TEXT NOT NULL,
  summary TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ({_STATE_LIST})),
  reason TEXT,
  decided_by TEXT,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  decided_at REAL,
  CHECK ((state IN ({_DECIDED_LIST})) = (decided_by IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS approvals_by_state ON approvals(state, seq);
CREATE TABLE IF NOT EXISTS nonces (
  approval_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  issued_at REAL NOT NULL,
  PRIMARY KEY (approval_id, device_id)
);
CREATE TABLE IF NOT EXISTS status (
  session TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  cost_usd REAL NOT NULL,
  context_pct REAL,
  five_hour_pct REAL,
  seven_day_pct REAL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  device_id TEXT,
  event TEXT NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT,
  approval_id TEXT,
  tool TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_approval ON audit(approval_id, seq);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
"""

_APPROVAL_COLUMNS = (
    "seq, approval_id, session, tool, summary, state, reason, decided_by, created_at, expires_at, decided_at"
)
_STATUS_COLUMNS = "session, model, cost_usd, context_pct, five_hour_pct, seven_day_pct, updated_at"
_AUDIT_COLUMNS = "seq, ts, actor_kind, actor_id, device_id, event, outcome, reason, approval_id, tool"


class StaleApproval(RuntimeError):
    """A compare-and-set on an approval found a different state than expected (a programming error)."""


@dataclass(frozen=True)
class AuditEntry:
    """One state change or refused request. Holds no secret: no token, nonce or summary text."""

    ts: float
    actor_kind: str  # "device" | "owner" | "system"
    actor_id: str
    event: str  # approval.<state> | request.denied
    outcome: str  # pending | allowed | denied | expired | cancelled | refused
    device_id: str | None = None
    approval_id: str | None = None
    tool: str | None = None
    reason: str | None = None
    seq: int = 0

    def __post_init__(self) -> None:
        for value in (self.event, self.outcome, self.reason):
            if value is not None and not _CODE.match(value):
                raise ValueError("audit codes are short lowercase identifiers")


def entry_wire(e: AuditEntry) -> dict:
    d: dict = {"seq": e.seq, "ts": int(e.ts), "actor": f"{e.actor_kind}:{e.actor_id}", "event": e.event}
    d["outcome"] = e.outcome
    optional = {"reason": e.reason, "approval_id": e.approval_id, "tool": e.tool, "device_id": e.device_id}
    d.update({k: v for k, v in optional.items() if v is not None})
    return d


def clamp_limit(limit: int | None) -> int:
    return DEFAULT_PAGE if limit is None else max(1, min(MAX_PAGE, limit))


def _approval(r: tuple) -> Approval:
    return Approval(
        seq=r[0],
        approval_id=r[1],
        session=r[2],
        tool=r[3],
        summary=r[4],
        state=r[5],
        reason=r[6],
        decided_by=r[7],
        created_at=r[8],
        expires_at=r[9],
        decided_at=r[10],
    )


def _status(r: tuple) -> Status:
    return Status(
        session=r[0],
        model=r[1],
        cost_usd=r[2],
        context_pct=r[3],
        five_hour_pct=r[4],
        seven_day_pct=r[5],
        updated_at=r[6],
    )


class ClaudeStore:
    def __init__(self, path: str | Path, *, synchronous: str = "FULL"):
        if synchronous not in ("FULL", "NORMAL", "OFF"):
            raise ValueError(f"unknown synchronous mode {synchronous!r}")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10.0)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(f"PRAGMA synchronous={synchronous}")  # FULL: an answer survives power loss
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
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

    # --- approvals ---------------------------------------------------------------------------------
    def insert_approval(self, conn: sqlite3.Connection, a: Approval) -> Approval:
        cur = conn.execute(
            "INSERT INTO approvals(approval_id, session, tool, summary, state, reason, decided_by,"
            " created_at, expires_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                a.approval_id,
                a.session,
                a.tool,
                a.summary,
                a.state,
                a.reason,
                a.decided_by,
                a.created_at,
                a.expires_at,
                a.decided_at,
            ),
        )
        return replace(a, seq=int(cur.lastrowid or 0))

    def update_approval(self, conn: sqlite3.Connection, expect_state: str, a: Approval) -> None:
        """Compare-and-set on the state; writes only the columns that change during an approval's life."""
        n = conn.execute(
            "UPDATE approvals SET state=?, reason=?, decided_by=?, decided_at=?"
            " WHERE approval_id=? AND state=?",
            (a.state, a.reason, a.decided_by, a.decided_at, a.approval_id, expect_state),
        ).rowcount
        if n != 1:
            raise StaleApproval(a.approval_id)

    def get_approval(self, conn: sqlite3.Connection, approval_id: str) -> Approval | None:
        sql = f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE approval_id=?"  # noqa: S608 - fixed columns
        row = conn.execute(sql, (approval_id,)).fetchone()
        return _approval(row) if row else None

    def peek_state(self, approval_id: str) -> tuple[str, float] | None:
        """(state, expires_at) without a transaction: the long poll reads this every few hundred ms."""
        with self._lock:
            row = self._db.execute(
                "SELECT state, expires_at FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return (row[0], row[1]) if row else None

    def pending(self, conn: sqlite3.Connection, now: float, limit: int) -> list[Approval]:
        """Unexpired pending approvals, newest first."""
        sql = (
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE state='pending' AND expires_at>?"  # noqa: S608
            " ORDER BY seq DESC LIMIT ?"
        )
        return [_approval(r) for r in conn.execute(sql, (now, limit))]

    def count_pending(self, conn: sqlite3.Connection, now: float) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE state='pending' AND expires_at>?", (now,)
        ).fetchone()
        return int(row[0])

    def due(self, conn: sqlite3.Connection, now: float | None) -> list[Approval]:
        """Pending approvals whose window has closed (`now=None`: every pending one)."""
        sql = f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE state='pending'"  # noqa: S608
        params: tuple = ()
        if now is not None:
            sql += " AND expires_at<=?"
            params = (now,)
        return [_approval(r) for r in conn.execute(sql + " ORDER BY seq", params)]

    def prune_approvals(self, conn: sqlite3.Connection, created_before: float) -> int:
        """Drop finished approvals older than the cutoff (the audit table is never pruned)."""
        return conn.execute(
            "DELETE FROM approvals WHERE state!='pending' AND created_at<?", (created_before,)
        ).rowcount

    # --- nonces ------------------------------------------------------------------------------------
    def nonce_for(self, conn: sqlite3.Connection, approval_id: str, device_id: str) -> str | None:
        row = conn.execute(
            "SELECT nonce FROM nonces WHERE approval_id=? AND device_id=?", (approval_id, device_id)
        ).fetchone()
        return row[0] if row else None

    def add_nonce(
        self, conn: sqlite3.Connection, approval_id: str, device_id: str, nonce: str, now: float
    ) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO nonces(approval_id, device_id, nonce, issued_at) VALUES (?,?,?,?)",
            (approval_id, device_id, nonce, now),
        )

    def drop_nonces(self, conn: sqlite3.Connection, approval_id: str) -> None:
        conn.execute("DELETE FROM nonces WHERE approval_id=?", (approval_id,))

    def orphan_nonces(self, conn: sqlite3.Connection) -> int:
        """Remove nonces whose approval is gone or no longer pending (defensive; normally none)."""
        return conn.execute(
            "DELETE FROM nonces WHERE approval_id NOT IN"
            " (SELECT approval_id FROM approvals WHERE state='pending')"
        ).rowcount

    # --- status ------------------------------------------------------------------------------------
    def upsert_status(self, conn: sqlite3.Connection, s: Status, keep: int) -> None:
        conn.execute(
            "INSERT INTO status(session, model, cost_usd, context_pct, five_hour_pct, seven_day_pct,"
            " updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET model=excluded.model,"
            " cost_usd=excluded.cost_usd, context_pct=excluded.context_pct,"
            " five_hour_pct=excluded.five_hour_pct, seven_day_pct=excluded.seven_day_pct,"
            " updated_at=excluded.updated_at",
            (s.session, s.model, s.cost_usd, s.context_pct, s.five_hour_pct, s.seven_day_pct, s.updated_at),
        )
        conn.execute(
            "DELETE FROM status WHERE session NOT IN"
            " (SELECT session FROM status ORDER BY updated_at DESC, rowid DESC LIMIT ?)",
            (keep,),
        )

    def newest_status(self) -> Status | None:
        with self._lock:
            sql = f"SELECT {_STATUS_COLUMNS} FROM status ORDER BY updated_at DESC, rowid DESC LIMIT 1"  # noqa: S608
            row = self._db.execute(sql).fetchone()
        return _status(row) if row else None

    def count_status(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM status").fetchone()[0])

    # --- audit -------------------------------------------------------------------------------------
    def insert_audit(self, conn: sqlite3.Connection, e: AuditEntry) -> int:
        cur = conn.execute(
            "INSERT INTO audit(ts, actor_kind, actor_id, device_id, event, outcome, reason, approval_id,"
            " tool) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                e.ts,
                e.actor_kind,
                e.actor_id,
                e.device_id,
                e.event,
                e.outcome,
                e.reason,
                e.approval_id,
                e.tool,
            ),
        )
        return int(cur.lastrowid or 0)

    def audit_page(
        self, conn: sqlite3.Connection, *, before: int | None, limit: int, approval_id: str | None = None
    ) -> list[AuditEntry]:
        """Newest first; `before` is an exclusive seq."""
        where, params = ["1=1"], []
        if before is not None:
            where.append("seq<?")
            params.append(before)
        if approval_id is not None:
            where.append("approval_id=?")
            params.append(approval_id)
        sql = f"SELECT {_AUDIT_COLUMNS} FROM audit WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?"  # noqa: S608
        rows = conn.execute(sql, (*params, limit)).fetchall()
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
                approval_id=r[8],
                tool=r[9],
            )
            for r in rows
        ]
