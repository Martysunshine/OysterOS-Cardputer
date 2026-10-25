"""The Control Deck's own SQLite file (`deck.db` in the data directory): profiles, jobs and the audit table.

One connection in WAL mode behind an RLock (API handlers and the runner thread share it); every write path
runs in a BEGIN IMMEDIATE transaction. Table creation is idempotent. The audit table refuses modification
at the database level (triggers), in addition to audit.py only ever inserting and selecting.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from oyster.deck import audit
from oyster.deck.jobs import STATES, DeckJob

log = logging.getLogger("oyster.deck")

SCHEMA_VERSION = "1"
DB_NAME = "deck.db"

_STATE_LIST = ",".join(f"'{s}'" for s in STATES)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles (
  profile_id TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL UNIQUE,
  device_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 9),
  key_hash TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  impactful INTEGER NOT NULL CHECK (impactful IN (0, 1)),
  executor TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ({_STATE_LIST})),
  reason TEXT,
  actions TEXT NOT NULL,
  steps TEXT NOT NULL,
  nonce TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  run_budget_s REAL NOT NULL,
  confirm_deadline REAL,
  started_at REAL,
  finished_at REAL,
  UNIQUE (device_id, key_hash)
);
CREATE INDEX IF NOT EXISTS jobs_by_state ON jobs(state);
CREATE INDEX IF NOT EXISTS jobs_by_device ON jobs(device_id, seq);
{audit.SCHEMA}
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
"""

_JOB_COLUMNS = (
    "seq, job_id, device_id, profile_id, slot, key_hash, fingerprint, impactful, executor, state, reason,"
    " actions, steps, nonce, created_at, updated_at, run_budget_s, confirm_deadline, started_at, finished_at"
)


class StaleJob(RuntimeError):
    """A compare-and-set on a job row found a different state than expected (a programming error)."""


def _job(r: tuple) -> DeckJob:
    return DeckJob(
        seq=r[0],
        job_id=r[1],
        device_id=r[2],
        profile_id=r[3],
        slot=r[4],
        key_hash=r[5],
        fingerprint=r[6],
        impactful=bool(r[7]),
        executor=r[8],
        state=r[9],
        reason=r[10],
        actions=tuple(json.loads(r[11])),
        steps=tuple(json.loads(r[12])),
        nonce=r[13],
        created_at=r[14],
        updated_at=r[15],
        run_budget_s=r[16],
        confirm_deadline=r[17],
        started_at=r[18],
        finished_at=r[19],
    )


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class DeckStore:
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
            self._db.execute(
                f"PRAGMA synchronous={synchronous}"
            )  # FULL: an acknowledged change survives power loss
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

    # --- profiles ---
    def get_profile_json(self, conn: sqlite3.Connection, profile_id: str) -> str | None:
        row = conn.execute("SELECT body FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return row[0] if row else None

    def list_profile_json(self, conn: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = conn.execute("SELECT profile_id, body FROM profiles ORDER BY profile_id")
        return [(r[0], r[1]) for r in rows]

    def count_profiles(self, conn: sqlite3.Connection) -> int:
        return conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]

    def put_profile(self, conn: sqlite3.Connection, profile_id: str, body: str, now: float) -> None:
        conn.execute(
            "INSERT INTO profiles(profile_id, body, updated_at) VALUES (?,?,?) ON CONFLICT(profile_id)"
            " DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (profile_id, body, now),
        )

    def delete_profile(self, conn: sqlite3.Connection, profile_id: str) -> bool:
        return conn.execute("DELETE FROM profiles WHERE profile_id=?", (profile_id,)).rowcount > 0

    # --- jobs ---
    def insert_job(self, conn: sqlite3.Connection, job: DeckJob) -> DeckJob:
        cur = conn.execute(
            "INSERT INTO jobs(job_id, device_id, profile_id, slot, key_hash, fingerprint, impactful,"
            " executor, state, reason, actions, steps, nonce, created_at, updated_at, run_budget_s,"
            " confirm_deadline, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.job_id,
                job.device_id,
                job.profile_id,
                job.slot,
                job.key_hash,
                job.fingerprint,
                int(job.impactful),
                job.executor,
                job.state,
                job.reason,
                _dumps(list(job.actions)),
                _dumps(list(job.steps)),
                job.nonce,
                job.created_at,
                job.updated_at,
                job.run_budget_s,
                job.confirm_deadline,
                job.started_at,
                job.finished_at,
            ),
        )
        return replace(job, seq=int(cur.lastrowid or 0))

    def update_job(self, conn: sqlite3.Connection, expect_state: str, job: DeckJob) -> None:
        """Compare-and-set on the state; writes only the columns that change during a job's life."""
        n = conn.execute(
            "UPDATE jobs SET state=?, reason=?, steps=?, nonce=?, updated_at=?, confirm_deadline=?,"
            " started_at=?, finished_at=? WHERE job_id=? AND state=?",
            (
                job.state,
                job.reason,
                _dumps(list(job.steps)),
                job.nonce,
                job.updated_at,
                job.confirm_deadline,
                job.started_at,
                job.finished_at,
                job.job_id,
                expect_state,
            ),
        ).rowcount
        if n != 1:
            raise StaleJob(job.job_id)

    def get_job(self, conn: sqlite3.Connection, job_id: str) -> DeckJob | None:
        row = conn.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE job_id=?", (job_id,)).fetchone()  # noqa: S608
        return _job(row) if row else None

    def job_by_key(self, conn: sqlite3.Connection, device_id: str, key_hash: str) -> DeckJob | None:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE device_id=? AND key_hash=?"  # noqa: S608
        row = conn.execute(sql, (device_id, key_hash)).fetchone()
        return _job(row) if row else None

    def live_jobs(self, conn: sqlite3.Connection, profile_id: str | None = None) -> list[DeckJob]:
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state IN ('queued','awaiting_confirmation','running')"  # noqa: S608
        params: tuple = ()
        if profile_id is not None:
            sql += " AND profile_id=?"
            params = (profile_id,)
        return [_job(r) for r in conn.execute(sql + " ORDER BY seq", params)]

    def list_jobs(
        self,
        conn: sqlite3.Connection,
        *,
        device_id: str | None,
        state: str | None,
        before: int | None,
        limit: int,
    ) -> list[DeckJob]:
        where, params = ["1=1"], []
        for clause, value in (("device_id=?", device_id), ("state=?", state), ("seq<?", before)):
            if value is not None:
                where.append(clause)
                params.append(value)
        sql = f"SELECT {_JOB_COLUMNS} FROM jobs WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT ?"  # noqa: S608
        return [_job(r) for r in conn.execute(sql, (*params, limit))]
