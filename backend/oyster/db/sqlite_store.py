"""SQLite implementation of oyster.store.Store (ADR-004).

One connection in WAL mode guarded by an RLock (API handlers, the run worker and the expiry sweep share
it). Write paths run in BEGIN IMMEDIATE transactions so a rejected batch leaves no partial state.
Events, job bodies, results and runs are JSON columns. Device tokens are stored only as salted hashes.
Ownership: every method that takes device_id raises NotFound for ids bound to another device.
Durability: synchronous=FULL, so an acknowledged event/result batch survives power loss.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path

from oyster import auth
from oyster.contracts import DEVICE_OPERATIONS, Event, Finding, Job, JobResult, TargetProfile
from oyster.jobs import lifecycle as lc
from oyster.policy import catalogue
from oyster.policy.catalogue import CatalogueError
from oyster.settings import RunLimits
from oyster.store import Conflict, DeviceRec, JobRec, NotFound, RunRec, SessionRec

log = logging.getLogger("oyster.db")

SCHEMA_VERSION = 1
MAX_LATE_RESULTS = 8
SESSION_SOURCE_MODES = ("live", "simulated")
SESSION_ID_RE = re.compile(r"^sess-[0-9a-zA-Z_-]{1,40}$")
_REASON_RE = re.compile(r"^[a-z0-9_]{1,40}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  simulated INTEGER NOT NULL,
  token_salt TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  rotated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS targets (
  target_id TEXT PRIMARY KEY,
  scope_version INTEGER NOT NULL,
  body TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  source_mode TEXT NOT NULL,
  capabilities TEXT NOT NULL,
  room_label TEXT,
  created_at REAL NOT NULL,
  linked_baseline_session_id TEXT
);
CREATE TABLE IF NOT EXISTS events (
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  event_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  source_mode TEXT NOT NULL,
  target_id TEXT,
  body TEXT NOT NULL,
  received_at REAL NOT NULL,
  PRIMARY KEY (session_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_by_seq ON events(session_id, sequence);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  run_id TEXT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  device_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL,
  status_reason TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  delivered_at REAL,
  deadline_at REAL NOT NULL,
  result_key TEXT,
  result_body TEXT,
  late_results TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_by_device ON jobs(device_id, status, created_at);
CREATE INDEX IF NOT EXISTS jobs_by_run ON jobs(run_id);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  status TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_session ON runs(session_id, created_at);
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  body TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_by_session ON findings(session_id, created_at);
"""

_RUN_FIELDS = tuple(f.name for f in fields(RunRec))


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class SqliteStore:
    """Thread-safe SQLite store. `clock` is injectable so tests can move time deterministically."""

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
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    # --- infrastructure ---
    def now(self) -> float:
        return self._clock()

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
                self._rollback()  # also when COMMIT itself fails: never leave the connection in a transaction
                raise

    def _rollback(self) -> None:
        try:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
        except sqlite3.Error:
            log.exception("rollback failed")

    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _q1(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # --- devices ---
    def create_device(self, device_id: str, display_name: str, simulated: bool) -> str:
        """Enrol or re-enrol; re-enrolling rotates the token (the old one stops matching).
        Returns the token once."""
        token = auth.new_device_token()
        salt, digest = auth.hash_token(token)
        now = self.now()
        with self._tx() as db:
            db.execute(
                "INSERT INTO devices(device_id, display_name, simulated, token_salt, token_hash,"
                " created_at, rotated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET"
                " display_name=excluded.display_name, simulated=excluded.simulated,"
                " token_salt=excluded.token_salt, token_hash=excluded.token_hash,"
                " rotated_at=excluded.rotated_at",
                (device_id, display_name, int(bool(simulated)), salt, digest, now, now),
            )
        return token

    def device_for_token(self, token: str) -> DeviceRec | None:
        if not token or len(token) > auth.MAX_TOKEN_LEN:
            return None
        found = None
        for row in self._q("SELECT device_id, display_name, simulated, token_salt, token_hash FROM devices"):
            # compare against every row so timing does not depend on the match position
            if auth.verify_token(token, row["token_salt"], row["token_hash"]) and found is None:
                found = DeviceRec(row["device_id"], row["display_name"], bool(row["simulated"]))
        return found

    def get_device(self, device_id: str) -> DeviceRec:
        row = self._q1(
            "SELECT device_id, display_name, simulated FROM devices WHERE device_id=?", (device_id,)
        )
        if row is None:
            raise NotFound(device_id)
        return DeviceRec(row["device_id"], row["display_name"], bool(row["simulated"]))

    def list_devices(self) -> list[DeviceRec]:
        return [
            DeviceRec(r["device_id"], r["display_name"], bool(r["simulated"]))
            for r in self._q("SELECT device_id, display_name, simulated FROM devices ORDER BY device_id")
        ]

    # --- targets ---
    def upsert_target(self, t: TargetProfile) -> TargetProfile:
        """Insert or replace. scope_version may stay equal or rise (raising it makes older jobs stale)."""
        body = canonical_json(t.model_dump(mode="json"))
        with self._tx() as db:
            row = db.execute("SELECT scope_version FROM targets WHERE target_id=?", (t.target_id,)).fetchone()
            if row is not None and t.scope_version < row["scope_version"]:
                raise Conflict(
                    "stale_scope",
                    f"scope_version {t.scope_version} is lower than stored {row['scope_version']}",
                )
            db.execute(
                "INSERT INTO targets(target_id, scope_version, body, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(target_id) DO UPDATE SET scope_version=excluded.scope_version,"
                " body=excluded.body, updated_at=excluded.updated_at",
                (t.target_id, t.scope_version, body, self.now()),
            )
        return t

    def get_target(self, target_id: str) -> TargetProfile | None:
        row = self._q1("SELECT body FROM targets WHERE target_id=?", (target_id,))
        return TargetProfile.model_validate_json(row["body"]) if row else None

    def list_targets(self) -> list[TargetProfile]:
        return [
            TargetProfile.model_validate_json(r["body"])
            for r in self._q("SELECT body FROM targets ORDER BY target_id")
        ]

    # --- sessions ---
    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRec:
        return SessionRec(
            session_id=row["session_id"],
            device_id=row["device_id"],
            source_mode=row["source_mode"],
            capabilities=json.loads(row["capabilities"]),
            room_label=row["room_label"],
            created_at=row["created_at"],
            linked_baseline_session_id=row["linked_baseline_session_id"],
        )

    def register_session(
        self,
        device_id: str,
        session_id: str | None,
        source_mode: str,
        capabilities: list[str],
        room_label: str | None,
    ) -> tuple[SessionRec, bool]:
        if source_mode not in SESSION_SOURCE_MODES:
            raise Conflict("validation_failed", f"source_mode must be one of {SESSION_SOURCE_MODES}")
        if session_id is not None and not SESSION_ID_RE.match(session_id):
            raise Conflict("validation_failed", "session_id must match ^sess-[0-9a-zA-Z_-]{1,40}$")
        caps = list(dict.fromkeys(capabilities))
        bad = [c for c in caps if c not in DEVICE_OPERATIONS]
        if bad:
            raise Conflict("validation_failed", f"unknown capabilities {bad}")
        with self._tx() as db:
            dev = db.execute("SELECT simulated FROM devices WHERE device_id=?", (device_id,)).fetchone()
            if dev is None:
                raise NotFound(device_id)
            if dev["simulated"] and source_mode != "simulated":
                raise Conflict("validation_failed", "a simulated device may only open simulated sessions")
            if session_id is not None:
                row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is not None:
                    if row["device_id"] != device_id:
                        raise Conflict("conflict", "session id is bound to another device")
                    if row["source_mode"] != source_mode:
                        raise Conflict("conflict", "session exists with a different source_mode")
                    return self._session(row), False
            sid = session_id or _new_id("sess")
            db.execute(
                "INSERT INTO sessions(session_id, device_id, source_mode, capabilities, room_label,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (sid, device_id, source_mode, json.dumps(caps), room_label, self.now()),
            )
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            return self._session(row), True

    def get_session(self, session_id: str, device_id: str | None = None) -> SessionRec:
        row = self._q1("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if row is None or (device_id is not None and row["device_id"] != device_id):
            raise NotFound(session_id)
        return self._session(row)

    def list_sessions(self, device_id: str | None = None) -> list[SessionRec]:
        if device_id is None:
            rows = self._q("SELECT * FROM sessions ORDER BY created_at, session_id")
        else:
            rows = self._q(
                "SELECT * FROM sessions WHERE device_id=? ORDER BY created_at, session_id", (device_id,)
            )
        return [self._session(r) for r in rows]

    def link_baseline(self, session_id: str, baseline_session_id: str | None) -> SessionRec:
        """Owner action: link an earlier session of the SAME device as the comparison baseline (reports may
        cite it); a baseline owned by another device is rejected."""
        with self._tx() as db:
            if db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone() is None:
                raise NotFound(session_id)
            if baseline_session_id is not None:
                if baseline_session_id == session_id:
                    raise Conflict("validation_failed", "a session cannot be its own baseline")
                own = db.execute(
                    "SELECT device_id FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                base = db.execute(
                    "SELECT device_id FROM sessions WHERE session_id=?", (baseline_session_id,)
                ).fetchone()
                if base is None:
                    raise NotFound(baseline_session_id)
                if base["device_id"] != own["device_id"]:
                    raise Conflict("validation_failed", "a baseline must be a session of the same device")
            db.execute(
                "UPDATE sessions SET linked_baseline_session_id=? WHERE session_id=?",
                (baseline_session_id, session_id),
            )
        return self.get_session(session_id)

    # --- events ---
    def add_events(self, device_id: str, session_id: str, events: list[Event]) -> tuple[int, int, int]:
        """Atomic batch. Identical duplicate -> counted; conflicting one -> Conflict (nothing stored)."""
        accepted = duplicates = 0
        now = self.now()
        with self._tx() as db:
            srow = db.execute(
                "SELECT device_id, source_mode FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if srow is None or srow["device_id"] != device_id:
                raise NotFound(session_id)
            simulated_session = srow["source_mode"] == "simulated"
            for ev in events:
                if ev.session_id != session_id or ev.device_id != device_id:
                    raise Conflict("validation_failed", f"{ev.event_id}: session_id/device_id do not match")
                if (ev.source_mode.value == "simulated") != simulated_session:
                    raise Conflict(
                        "validation_failed",
                        f"{ev.event_id}: source_mode {ev.source_mode.value} does not "
                        f"match the {srow['source_mode']} session",
                    )
                body = canonical_json(ev.model_dump(mode="json"))
                old = db.execute(
                    "SELECT body FROM events WHERE session_id=? AND event_id=?", (session_id, ev.event_id)
                ).fetchone()
                if old is not None:
                    if old["body"] != body:
                        raise Conflict("conflict", f"{ev.event_id} conflicts with the stored event")
                    duplicates += 1
                    continue
                db.execute(
                    "INSERT INTO events(session_id, event_id, device_id, sequence, operation, status,"
                    " source_mode, target_id, body, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        ev.event_id,
                        device_id,
                        ev.sequence,
                        ev.operation,
                        ev.status.value,
                        ev.source_mode.value,
                        ev.target_id,
                        body,
                        now,
                    ),
                )
                accepted += 1
            mx = db.execute(
                "SELECT MAX(sequence) AS m FROM events WHERE session_id=?", (session_id,)
            ).fetchone()["m"]
        return accepted, duplicates, int(mx) if mx is not None else 0

    def get_events(
        self,
        session_id: str,
        event_ids: list[str] | None = None,
        limit: int = 500,
        newest: bool = False,
    ) -> list[Event]:
        """Ascending sequence order. `limit` keeps the first events, or with newest=True the last ones."""
        if event_ids is None and newest:
            rows = self._q(
                "SELECT body FROM (SELECT body, sequence, rowid AS rid FROM events WHERE session_id=?"
                " ORDER BY sequence DESC, rowid DESC LIMIT ?) ORDER BY sequence, rid",
                (session_id, int(limit)),
            )
        elif event_ids is None:
            rows = self._q(
                "SELECT body FROM events WHERE session_id=? ORDER BY sequence, rowid LIMIT ?",
                (session_id, int(limit)),
            )
        else:
            ids = list(dict.fromkeys(event_ids))
            if not ids:
                return []
            rows = []
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                rows += self._q(
                    f"SELECT body, sequence FROM events WHERE session_id=? AND event_id IN ({marks})"  # noqa: S608
                    " ORDER BY sequence, rowid",
                    (session_id, *chunk),
                )
            rows = rows[: int(limit)]
        return [Event.model_validate_json(r["body"]) for r in rows]

    def count_events(self, session_id: str) -> int:
        return int(self._q1("SELECT COUNT(*) AS n FROM events WHERE session_id=?", (session_id,))["n"])

    def missing_event_ids(self, session_id: str, event_ids: list[str]) -> list[str]:
        ids = list(dict.fromkeys(event_ids))
        with self._lock:
            return self._missing_events(self._db, session_id, ids)

    @staticmethod
    def _missing_events(db: sqlite3.Connection, session_id: str, ids: list[str]) -> list[str]:
        missing = []
        for eid in ids:
            if (
                db.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND event_id=?", (session_id, eid)
                ).fetchone()
                is None
            ):
                missing.append(eid)
        return missing

    def artifact_ids(self, session_id: str) -> list[str]:
        """Artifact ids reported by accepted job results of this session (citable evidence)."""
        out: list[str] = []
        for r in self._q(
            "SELECT result_body FROM jobs WHERE session_id=? AND result_body IS NOT NULL", (session_id,)
        ):
            out += json.loads(r["result_body"]).get("artifact_ids", [])
        return list(dict.fromkeys(out))

    def missing_evidence_ids(
        self, session_id: str, ids: list[str], *, include_baseline: bool = True
    ) -> list[str]:
        """Ids that are neither events nor result artifacts of the session (or of its linked baseline)."""
        sess = self.get_session(session_id)
        scopes = [session_id]
        if include_baseline and sess.linked_baseline_session_id:
            scopes.append(sess.linked_baseline_session_id)
        remaining = list(dict.fromkeys(ids))
        for sid in scopes:
            if not remaining:
                break
            arts = set(self.artifact_ids(sid))
            remaining = [i for i in self.missing_event_ids(sid, remaining) if i not in arts]
        return remaining

    # --- jobs ---
    @staticmethod
    def _jobrec(row: sqlite3.Row) -> JobRec:
        return JobRec(
            job=Job.model_validate_json(row["body"]),
            run_id=row["run_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            result=JobResult.model_validate_json(row["result_body"]) if row["result_body"] else None,
        )

    def create_job(
        self,
        *,
        run_id: str | None,
        session_id: str,
        operation: str,
        arguments: dict,
        target_id: str | None,
        scope_version: int,
        deadline_s: float,
        limits: dict,
        requires_local_confirmation: bool,
    ) -> JobRec:
        """Validate and queue a job. Rejects unsupported operations, capabilities the device lacks, unknown
        targets, operations the target does not allow, stale scope versions and arguments/limits over bounds.
        requires_local_confirmation = derived(operation, test_id + enrolled profile) OR the caller flag: a
        caller can raise it, never lower it."""
        try:
            spec = catalogue.get(operation)
            deadline = catalogue.resolve_deadline(operation, deadline_s)
            lim = catalogue.resolve_limits(operation, limits)
        except CatalogueError as e:
            raise Conflict(e.code, e.detail) from None
        now = self.now()
        with self._tx() as db:
            srow = db.execute(
                "SELECT device_id, capabilities FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if srow is None:
                raise NotFound(session_id)
            if operation not in json.loads(srow["capabilities"]):
                raise Conflict(
                    "unsupported_operation", f"the device did not declare {operation} for this session"
                )
            if run_id is not None:
                rrow = db.execute(
                    "SELECT session_id, status, body FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if rrow is None or rrow["session_id"] != session_id:
                    raise NotFound(run_id)
                if rrow["status"] in lc.RUN_TERMINAL or json.loads(rrow["body"]).get("cancel_requested"):
                    raise Conflict("cancelled", "run is finished or cancellation was requested")
            profile = None
            if spec.requires_target:
                if not target_id:
                    raise Conflict("validation_failed", f"{operation} requires an enrolled target_id")
                trow = db.execute("SELECT body FROM targets WHERE target_id=?", (target_id,)).fetchone()
                if trow is None:
                    raise NotFound(target_id)
                target = TargetProfile.model_validate_json(trow["body"])
                if operation not in target.allowed_operations:
                    raise Conflict("operation_not_allowed", f"{operation} is not allowed for {target_id}")
                if scope_version != target.scope_version:
                    raise Conflict(
                        "stale_scope", f"scope_version {scope_version} != current {target.scope_version}"
                    )
                profile = catalogue.load_profile(target.profile_id)
            elif target_id is not None:
                raise Conflict("validation_failed", f"{operation} does not take a target_id")
            try:
                args = catalogue.validate_arguments(operation, arguments, profile)
            except CatalogueError as e:
                raise Conflict(e.code, e.detail) from None
            confirm = catalogue.job_requires_local_confirmation(operation, args, profile) or bool(
                requires_local_confirmation
            )
            job = Job(
                job_id=_new_id("job"),
                session_id=session_id,
                device_id=srow["device_id"],
                target_id=target_id,
                operation=operation,
                arguments=args,
                scope_version=scope_version,
                deadline_s=deadline,
                limits=lim,
                requires_local_confirmation=confirm,
                nonce=_new_id("n") if confirm else None,
            )
            db.execute(
                "INSERT INTO jobs(job_id, run_id, session_id, device_id, operation, body, status, created_at,"
                " updated_at, deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job.job_id,
                    run_id,
                    session_id,
                    srow["device_id"],
                    operation,
                    canonical_json(job.model_dump(mode="json")),
                    lc.QUEUED,
                    now,
                    now,
                    now + deadline,
                ),
            )
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            return self._jobrec(row)

    def next_job_for_device(self, device_id: str) -> Job | None:
        """Deliver at most one queued job (oldest first): queued -> delivered. A job whose target
        scope_version moved, or whose operation/arguments the target's current allowed_operations and
        profile no longer permit, fails instead of being delivered."""
        now = self.now()
        self.expire_jobs(now)
        with self._tx() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE device_id=? AND status=? ORDER BY created_at, rowid",
                (device_id, lc.QUEUED),
            ).fetchall()
            for row in rows:
                job = Job.model_validate_json(row["body"])
                if job.target_id is not None:
                    reason = self._delivery_problem(db, job)
                    if reason is not None:
                        db.execute(
                            "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE job_id=?",
                            (lc.FAILED, reason, now, job.job_id),
                        )
                        continue
                db.execute(
                    "UPDATE jobs SET status=?, delivered_at=?, updated_at=? WHERE job_id=?",
                    (lc.DELIVERED, now, now, job.job_id),
                )
                return job
        return None

    @staticmethod
    def _delivery_problem(db: sqlite3.Connection, job: Job) -> str | None:
        """Status reason when a queued job may no longer be delivered, else None."""
        t = db.execute("SELECT body FROM targets WHERE target_id=?", (job.target_id,)).fetchone()
        if t is None:
            return lc.REASON_STALE_SCOPE
        target = TargetProfile.model_validate_json(t["body"])
        if target.scope_version != job.scope_version:
            return lc.REASON_STALE_SCOPE
        op = job.operation.value
        if op not in target.allowed_operations:
            return lc.REASON_TARGET_CHANGED
        profile = catalogue.load_profile(target.profile_id)
        if profile is None:
            return lc.REASON_TARGET_CHANGED
        try:
            args = catalogue.validate_arguments(op, job.arguments, profile)
        except CatalogueError:
            return lc.REASON_TARGET_CHANGED
        gated = catalogue.job_requires_local_confirmation(op, args, profile)
        if gated and not job.requires_local_confirmation:
            return lc.REASON_TARGET_CHANGED
        return None

    def _job_row_for_device(self, db: sqlite3.Connection, device_id: str, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["device_id"] != device_id:
            raise NotFound(job_id)
        return row

    def set_job_status(self, device_id: str, job_id: str, status: str) -> JobRec:
        """Device lifecycle report. Forward-only; repeating the current status is an idempotent no-op.
        A report for a job that already ended (expired, cancelled, ...) is a Conflict: the device must not
        execute it (a confirmation-gated job runs only after its `running` report is accepted, and that
        report is accepted only from awaiting_local_confirmation)."""
        if status not in lc.DEVICE_REPORTABLE:
            raise Conflict("validation_failed", f"status must be one of {lc.DEVICE_REPORTABLE}")
        with self._tx() as db:
            row = self._job_row_for_device(db, device_id, job_id)
            cur = row["status"]
            if cur in lc.TERMINAL:
                raise Conflict("conflict", f"job is {cur}; it must not be executed")
            if cur != status:
                gated = Job.model_validate_json(row["body"]).requires_local_confirmation
                if not lc.can_device_transition(cur, status, gated):
                    why = (
                        "a confirmation-gated job may run only after awaiting_local_confirmation"
                        if gated and status == lc.RUNNING and cur != lc.AWAITING
                        else "not a forward transition"
                    )
                    raise Conflict("conflict", f"{cur} -> {status}: {why}")
                db.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (status, self.now(), job_id)
                )
            return self._jobrec(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def submit_job_result(self, device_id: str, result: JobResult) -> tuple[JobRec, bool]:
        """Idempotent by idempotency_key; never triggers re-execution. Returns (record, duplicate).

        events_missing (Conflict) when any event_id is not yet uploaded to the job's session. A result for a
        job the backend already ended (deadline sweep, or expire_job/cancel_job after delivery) is stored for
        audit only (status unchanged)."""
        body = canonical_json(result.model_dump(mode="json"))
        with self._tx() as db:
            row = self._job_row_for_device(db, device_id, result.job_id)
            job = Job.model_validate_json(row["body"])
            c = result.confirmation
            if c is not None and (c.job_id != job.job_id or c.nonce != job.nonce):
                raise Conflict("conflict", "confirmation does not match this job's id and nonce")
            if row["result_key"] is not None:
                if row["result_key"] == result.idempotency_key:
                    if row["result_body"] == body:
                        return self._jobrec(row), True
                    raise Conflict("conflict", "idempotency_key reused with different content")
                raise Conflict("conflict", "job already has a different result")
            late = json.loads(row["late_results"])
            for item in late:
                if item["idempotency_key"] == result.idempotency_key:
                    if canonical_json(item["result"]) == body:
                        return self._jobrec(row), True
                    raise Conflict("conflict", "idempotency_key reused with different content")
            missing = self._missing_events(db, job.session_id, list(dict.fromkeys(result.event_ids)))
            if missing:
                shown = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
                raise Conflict("events_missing", f"not uploaded to {job.session_id}: {shown}")
            now = self.now()
            ended_by_backend = row["status_reason"] == lc.REASON_DEADLINE or (
                row["delivered_at"] is not None
                and row["status_reason"] not in (None, lc.REASON_RESULT)
            )
            if row["status"] in lc.TERMINAL and ended_by_backend:
                if len(late) >= MAX_LATE_RESULTS:
                    raise Conflict("conflict", "too many late results stored for this job")
                late.append(
                    {
                        "idempotency_key": result.idempotency_key,
                        "received_at": now,
                        "result": json.loads(body),
                    }
                )
                db.execute(
                    "UPDATE jobs SET late_results=?, updated_at=? WHERE job_id=?",
                    (json.dumps(late), now, job.job_id),
                )
                return self._jobrec(
                    db.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
                ), False
            if row["status"] not in lc.IN_FLIGHT:
                raise Conflict("conflict", f"job is {row['status']}; no result is expected")
            if job.requires_local_confirmation and result.outcome.value in ("succeeded", "unknown_outcome"):
                if c is None or not c.approved:
                    raise Conflict(
                        "confirmation_required",
                        "an executed confirmation-gated job must carry its approved local confirmation",
                    )
                if row["status"] != lc.RUNNING:
                    # `running` is accepted only from awaiting_local_confirmation, so a gated job that is
                    # not running never passed through the confirmation step
                    raise Conflict(
                        "confirmation_required",
                        f"job is {row['status']}: a confirmation-gated job executes only after "
                        "awaiting_local_confirmation and an accepted running report",
                    )
            db.execute(
                "UPDATE jobs SET status=?, status_reason=?, result_key=?, result_body=?, updated_at=?"
                " WHERE job_id=?",
                (
                    lc.status_for_outcome(result.outcome.value),
                    lc.REASON_RESULT,
                    result.idempotency_key,
                    body,
                    now,
                    job.job_id,
                ),
            )
            return self._jobrec(
                db.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            ), False

    def get_job(self, job_id: str) -> JobRec:
        row = self._q1("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if row is None:
            raise NotFound(job_id)
        return self._jobrec(row)

    def get_job_audit(self, job_id: str) -> dict:
        """Owner diagnostics: status reason and late results kept for audit."""
        row = self._q1("SELECT status, status_reason, late_results FROM jobs WHERE job_id=?", (job_id,))
        if row is None:
            raise NotFound(job_id)
        return {
            "status": row["status"],
            "status_reason": row["status_reason"],
            "late_results": json.loads(row["late_results"]),
        }

    def list_jobs(self, session_id: str) -> list[JobRec]:
        return [
            self._jobrec(r)
            for r in self._q(
                "SELECT * FROM jobs WHERE session_id=? ORDER BY created_at, rowid", (session_id,)
            )
        ]

    def expire_jobs(self, now: float) -> int:
        with self._tx() as db:
            marks = ",".join("?" * len(lc.ACTIVE))
            rows = db.execute(
                f"SELECT job_id, operation, status, body FROM jobs WHERE status IN ({marks})"  # noqa: S608
                " AND deadline_at < ?",
                (*lc.ACTIVE, now),
            ).fetchall()
            for r in rows:
                gated = bool(json.loads(r["body"]).get("requires_local_confirmation"))
                db.execute(
                    "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE job_id=?",
                    (
                        lc.expiry_status(r["operation"], r["status"], gated),
                        lc.REASON_DEADLINE,
                        now,
                        r["job_id"],
                    ),
                )
        return len(rows)

    def expire_job(self, job_id: str, reason: str) -> JobRec:
        """Backend-side expiry of one job that cannot have started (lifecycle.not_started) -> expired.
        Any other job is returned unchanged; a later `running` report for an expired job is a Conflict."""
        return self._end_unstarted(job_id, lc.EXPIRED, reason)

    def cancel_job(self, job_id: str, reason: str) -> JobRec:
        """Like expire_job, but the job ends cancelled (e.g. its run finished before the job started)."""
        return self._end_unstarted(job_id, lc.CANCELLED, reason)

    def _end_unstarted(self, job_id: str, status: str, reason: str) -> JobRec:
        if not isinstance(reason, str) or not _REASON_RE.match(reason):
            raise Conflict("validation_failed", "reason must match ^[a-z0-9_]{1,40}$")
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound(job_id)
            job = Job.model_validate_json(row["body"])
            if lc.not_started(row["status"], job.requires_local_confirmation):
                db.execute(
                    "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE job_id=?",
                    (status, reason, self.now(), job_id),
                )
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._jobrec(row)

    def cancel_jobs_for_run(self, run_id: str) -> int:
        with self._tx() as db:
            return self._cancel_queued(db, run_id)

    def _cancel_queued(self, db: sqlite3.Connection, run_id: str) -> int:
        cur = db.execute(
            "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE run_id=? AND status=?",
            (lc.CANCELLED, lc.REASON_RUN_CANCELLED, self.now(), run_id, lc.QUEUED),
        )
        return cur.rowcount

    # --- runs ---
    @staticmethod
    def _runrec(row: sqlite3.Row) -> RunRec:
        data = json.loads(row["body"])
        data.update(
            run_id=row["run_id"],
            session_id=row["session_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return RunRec(**{k: v for k, v in data.items() if k in _RUN_FIELDS})

    def create_run(
        self, session_id: str, question: str, provider: dict, limits: RunLimits | None = None
    ) -> RunRec:
        """Create a queued run. With `limits`, Conflict('rate_limited') when the session already has an
        active run, too many runs are queued overall, or its device created too many runs in the last hour."""
        now = self.now()
        prov = {
            "name": str(provider.get("name") or "unknown"),
            "model": provider.get("model"),
            "is_fake": bool(provider.get("is_fake", False)),
        }
        run = RunRec(
            run_id=_new_id("run"),
            session_id=session_id,
            question=question,
            status="queued",
            provider=prov,
            created_at=now,
            updated_at=now,
        )
        with self._tx() as db:
            srow = db.execute("SELECT device_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if srow is None:
                raise NotFound(session_id)
            if limits is not None:
                self._check_run_limits(db, session_id, srow["device_id"], now, limits)
            db.execute(
                "INSERT INTO runs(run_id, session_id, status, body, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (run.run_id, session_id, run.status, json.dumps(asdict(run)), now, now),
            )
        return run

    @staticmethod
    def _check_run_limits(
        db: sqlite3.Connection, session_id: str, device_id: str, now: float, limits: RunLimits
    ) -> None:
        marks = ",".join("?" * len(lc.RUN_TERMINAL))
        active = db.execute(
            f"SELECT COUNT(*) AS n FROM runs WHERE session_id=? AND status NOT IN ({marks})",  # noqa: S608
            (session_id, *lc.RUN_TERMINAL),
        ).fetchone()["n"]
        if active >= limits.max_active_per_session:
            raise Conflict(
                "rate_limited",
                f"this session already has {active} active run(s); wait for it to finish or cancel it",
            )
        queued = db.execute("SELECT COUNT(*) AS n FROM runs WHERE status='queued'").fetchone()["n"]
        if queued >= limits.max_queued_total:
            raise Conflict("rate_limited", f"{queued} runs are already queued; try again shortly")
        recent = db.execute(
            "SELECT COUNT(*) AS n FROM runs r JOIN sessions s ON s.session_id = r.session_id"
            " WHERE s.device_id=? AND r.created_at > ?",
            (device_id, now - 3600.0),
        ).fetchone()["n"]
        if recent >= limits.max_per_device_per_hour:
            raise Conflict(
                "rate_limited", f"this device created {recent} runs in the last hour (limit "
                f"{limits.max_per_device_per_hour})"
            )

    def get_run(self, run_id: str, device_id: str | None = None) -> RunRec:
        row = self._q1(
            "SELECT r.*, s.device_id AS owner FROM runs r JOIN sessions s ON s.session_id = r.session_id"
            " WHERE r.run_id=?",
            (run_id,),
        )
        if row is None or (device_id is not None and row["owner"] != device_id):
            raise NotFound(run_id)
        return self._runrec(row)

    def list_runs(self, session_id: str) -> list[RunRec]:
        return [
            self._runrec(r)
            for r in self._q(
                "SELECT * FROM runs WHERE session_id=? ORDER BY created_at, rowid", (session_id,)
            )
        ]

    def save_run(self, run: RunRec) -> None:
        """Persist the worker's view. A terminal status is final and a cancel request is never cleared;
        the passed object is updated in place to reflect both."""
        with self._tx() as db:
            row = db.execute("SELECT status, body FROM runs WHERE run_id=?", (run.run_id,)).fetchone()
            if row is None:
                raise NotFound(run.run_id)
            stored = json.loads(row["body"])
            run.cancel_requested = bool(run.cancel_requested or stored.get("cancel_requested"))
            if row["status"] in lc.RUN_TERMINAL:
                run.status = row["status"]
            run.updated_at = self.now()
            db.execute(
                "UPDATE runs SET status=?, body=?, updated_at=? WHERE run_id=?",
                (run.status, json.dumps(asdict(run)), run.updated_at, run.run_id),
            )

    def request_run_cancel(self, run_id: str, device_id: str | None = None) -> RunRec:
        """Cooperative cancel: queued jobs of the run are cancelled; a run not yet started is cancelled now,
        an active run is flagged and finalised by the worker. Terminal runs are returned unchanged."""
        run = self.get_run(run_id, device_id)
        if run.status in lc.RUN_TERMINAL:
            return run
        with self._tx() as db:
            row = db.execute("SELECT status, body FROM runs WHERE run_id=?", (run_id,)).fetchone()
            data = json.loads(row["body"])
            status = row["status"]
            if status not in lc.RUN_TERMINAL:
                data["cancel_requested"] = True
                if status == "queued":
                    status = data["status"] = "cancelled"
                    data["error"] = data.get("error") or "cancelled before the run started"
                now = self.now()
                data["updated_at"] = now
                db.execute(
                    "UPDATE runs SET status=?, body=?, updated_at=? WHERE run_id=?",
                    (status, json.dumps(data), now, run_id),
                )
            self._cancel_queued(db, run_id)
        return self.get_run(run_id)

    # --- startup reconciliation ---
    def reconcile_startup(self) -> dict:
        """After a crash or restart no run worker is working: fail every non-terminal run ("backend
        restarted") and end those runs' jobs that cannot have started (queued, awaiting confirmation,
        delivered and gated) as cancelled, so nothing stays actionable. Jobs that may already be executing
        are left to their deadline and result. Returns counts."""
        now = self.now()
        marks = ",".join("?" * len(lc.RUN_TERMINAL))
        failed = cancelled = 0
        with self._tx() as db:
            runs = db.execute(
                f"SELECT run_id, body FROM runs WHERE status NOT IN ({marks})",  # noqa: S608
                lc.RUN_TERMINAL,
            ).fetchall()
            for r in runs:
                data = json.loads(r["body"])
                data.update(
                    status="failed",
                    error="backend restarted while the run was active; no answer was produced",
                    pending_action=None,
                    cancel_requested=bool(data.get("cancel_requested")),
                    updated_at=now,
                )
                db.execute(
                    "UPDATE runs SET status='failed', body=?, updated_at=? WHERE run_id=?",
                    (json.dumps(data), now, r["run_id"]),
                )
                failed += 1
                for j in db.execute(
                    "SELECT job_id, status, body FROM jobs WHERE run_id=?", (r["run_id"],)
                ).fetchall():
                    gated = bool(json.loads(j["body"]).get("requires_local_confirmation"))
                    if j["status"] in lc.ACTIVE and lc.not_started(j["status"], gated):
                        db.execute(
                            "UPDATE jobs SET status=?, status_reason=?, updated_at=? WHERE job_id=?",
                            (lc.CANCELLED, lc.REASON_RESTART, now, j["job_id"]),
                        )
                        cancelled += 1
        return {"runs_failed": failed, "jobs_cancelled": cancelled}

    # --- findings ---
    def add_finding(self, f: Finding) -> None:
        """Insert or replace by finding_id. Every evidence id must exist in the finding's session (events or
        result artifacts) or its linked baseline; otherwise Conflict('events_missing')."""
        self.get_session(f.session_id)
        missing = self.missing_evidence_ids(f.session_id, f.evidence_ids)
        if missing:
            raise Conflict(
                "events_missing", f"finding cites evidence outside {f.session_id}: {', '.join(missing[:10])}"
            )
        with self._tx() as db:
            db.execute(
                "INSERT INTO findings(finding_id, session_id, body, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(finding_id) DO UPDATE SET session_id=excluded.session_id, body=excluded.body",
                (f.finding_id, f.session_id, canonical_json(f.model_dump(mode="json")), self.now()),
            )

    def list_findings(self, session_id: str) -> list[Finding]:
        return [
            Finding.model_validate_json(r["body"])
            for r in self._q(
                "SELECT body FROM findings WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]
