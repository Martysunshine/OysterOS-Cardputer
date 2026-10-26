"""In-memory implementation of oyster.store.Store for the AI tests (no SQLite, no network).

Also: FakeClock (deterministic time), AutoDevice (simulated Cardputer answering jobs when the
orchestrator polls), and factories for settings, targets, sessions and events.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import secrets
import threading
from collections.abc import Callable
from pathlib import Path

from oyster.contracts import (
    DEVICE_OPERATIONS,
    Event,
    EventDetails,
    Finding,
    Job,
    JobLimits,
    JobResult,
    Operation,
    TargetProfile,
)
from oyster.settings import RunBudgets, Settings
from oyster.store import Conflict, DeviceRec, JobRec, NotFound, RunRec, SessionRec

TERMINAL_JOB = {"succeeded", "failed", "cancelled", "expired", "unknown_outcome", "unsupported"}
_FORWARD = {
    "queued": {"delivered", "cancelled"},
    "delivered": {"awaiting_local_confirmation", "running", "cancelled"},
    "awaiting_local_confirmation": {"running", "cancelled"},
    "running": set(),
}


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


class FakeStore:
    """Thread-safe in-memory Store. Returns copies so callers cannot mutate stored state."""

    def __init__(self, now: Callable[[], float] | None = None):
        self._lock = threading.RLock()
        self.now = now or FakeClock().time
        self.devices: dict[str, DeviceRec] = {}
        self.tokens: dict[str, str] = {}
        self.targets: dict[str, TargetProfile] = {}
        self.sessions: dict[str, SessionRec] = {}
        self.events: dict[str, dict[str, Event]] = {}
        self.jobs: dict[str, JobRec] = {}
        self.runs: dict[str, RunRec] = {}
        self.findings: dict[str, list[Finding]] = {}
        self.create_job_calls: list[dict] = []
        self.job_reasons: dict[str, str] = {}  # job_id -> reason recorded by expire_job/cancel_job
        self.responder: Callable[[FakeStore, JobRec], None] | None = None
        self._job_seq = itertools.count(1)
        self._run_seq = itertools.count(1)

    # devices
    def create_device(self, device_id: str, display_name: str, simulated: bool) -> str:
        with self._lock:
            token = secrets.token_hex(16)
            self.devices[device_id] = DeviceRec(device_id, display_name, simulated)
            self.tokens = {t: d for t, d in self.tokens.items() if d != device_id}
            self.tokens[token] = device_id
            return token

    def device_for_token(self, token: str) -> DeviceRec | None:
        with self._lock:
            d = self.tokens.get(token)
            return copy.deepcopy(self.devices[d]) if d else None

    # targets
    def upsert_target(self, t: TargetProfile) -> TargetProfile:
        with self._lock:
            self.targets[t.target_id] = t.model_copy(deep=True)
            return t

    def get_target(self, target_id: str) -> TargetProfile | None:
        with self._lock:
            t = self.targets.get(target_id)
            return t.model_copy(deep=True) if t else None

    def list_targets(self) -> list[TargetProfile]:
        with self._lock:
            return [t.model_copy(deep=True) for _, t in sorted(self.targets.items())]

    # sessions
    def register_session(self, device_id, session_id, source_mode, capabilities, room_label):
        with self._lock:
            sid = session_id or f"sess-{secrets.token_hex(4)}-001"
            if sid in self.sessions:
                if self.sessions[sid].device_id != device_id:
                    raise Conflict("conflict", "session owned by another device")
                return copy.deepcopy(self.sessions[sid]), False
            rec = SessionRec(sid, device_id, source_mode, list(capabilities), room_label, self.now())
            self.sessions[sid] = rec
            self.events[sid] = {}
            self.findings[sid] = []
            return copy.deepcopy(rec), True

    def get_session(self, session_id: str, device_id: str | None = None) -> SessionRec:
        with self._lock:
            s = self.sessions.get(session_id)
            if s is None or (device_id is not None and s.device_id != device_id):
                raise NotFound(session_id)
            return copy.deepcopy(s)

    def list_sessions(self, device_id: str | None = None) -> list[SessionRec]:
        with self._lock:
            return [copy.deepcopy(s) for s in self.sessions.values() if device_id in (None, s.device_id)]

    def link_baseline(self, session_id: str, baseline_session_id: str | None) -> SessionRec:
        with self._lock:
            if session_id not in self.sessions:
                raise NotFound(session_id)
            if baseline_session_id is not None:
                if baseline_session_id == session_id:
                    raise Conflict("validation_failed", "a session cannot be its own baseline")
                if baseline_session_id not in self.sessions:
                    raise NotFound(baseline_session_id)
            self.sessions[session_id].linked_baseline_session_id = baseline_session_id
            return copy.deepcopy(self.sessions[session_id])

    # events
    def add_events(self, device_id: str, session_id: str, events: list[Event]) -> tuple[int, int, int]:
        with self._lock:
            sess = self.get_session(session_id, device_id)
            store = self.events[sess.session_id]
            acc = dup = 0
            for e in events:
                if e.session_id != session_id or e.device_id != device_id:
                    raise Conflict("validation_failed", "event session/device mismatch")
                if e.event_id in store:
                    if store[e.event_id] != e:
                        raise Conflict("conflict", "conflicting duplicate event")
                    dup += 1
                    continue
                store[e.event_id] = e.model_copy(deep=True)
                acc += 1
            acked = max((e.sequence for e in store.values()), default=0)
            return acc, dup, acked

    def get_events(
        self,
        session_id: str,
        event_ids: list[str] | None = None,
        limit: int = 500,
        newest: bool = False,
    ) -> list[Event]:
        """Ascending (sequence, event_id); newest=True returns the LAST `limit` events (still ascending)."""
        with self._lock:
            if session_id not in self.sessions:
                raise NotFound(session_id)
            evs = sorted(self.events[session_id].values(), key=lambda e: (e.sequence, e.event_id))
            if event_ids is not None:
                wanted = set(event_ids)
                evs = [e for e in evs if e.event_id in wanted]
            picked = evs[-limit:] if newest and limit > 0 else evs[:limit]
            return [e.model_copy(deep=True) for e in picked]

    def missing_event_ids(self, session_id: str, event_ids: list[str]) -> list[str]:
        with self._lock:
            if session_id not in self.sessions:
                raise NotFound(session_id)
            return [i for i in event_ids if i not in self.events[session_id]]

    # jobs
    def create_job(
        self,
        *,
        run_id,
        session_id,
        operation,
        arguments,
        target_id,
        scope_version,
        deadline_s,
        limits,
        requires_local_confirmation,
    ) -> JobRec:
        with self._lock:
            self.create_job_calls.append(
                dict(
                    run_id=run_id,
                    session_id=session_id,
                    operation=operation,
                    arguments=copy.deepcopy(arguments),
                    target_id=target_id,
                    scope_version=scope_version,
                    deadline_s=deadline_s,
                    limits=copy.deepcopy(limits),
                    requires_local_confirmation=requires_local_confirmation,
                )
            )
            if operation not in DEVICE_OPERATIONS:
                raise Conflict("validation_failed", "unsupported operation")
            sess = self.get_session(session_id)
            if target_id is not None:
                t = self.targets.get(target_id)
                if t is None:
                    raise NotFound(target_id)
                if t.scope_version != scope_version:
                    raise Conflict("stale_scope", "scope version mismatch")
            job = Job(
                job_id=f"job-{next(self._job_seq):06d}",
                session_id=session_id,
                device_id=sess.device_id,
                target_id=target_id,
                operation=Operation(operation),
                arguments=copy.deepcopy(arguments),
                scope_version=scope_version,
                deadline_s=deadline_s,
                limits=JobLimits(**limits),
                requires_local_confirmation=requires_local_confirmation,
                nonce=f"n-{secrets.token_hex(6)}" if requires_local_confirmation else None,
            )
            now = self.now()
            rec = JobRec(job=job, run_id=run_id, status="queued", created_at=now, updated_at=now)
            self.jobs[job.job_id] = rec
            return copy.deepcopy(rec)

    def next_job_for_device(self, device_id: str) -> Job | None:
        with self._lock:
            for rec in self.jobs.values():
                if rec.job.device_id == device_id and rec.status == "queued":
                    rec.status = "delivered"
                    rec.updated_at = self.now()
                    return rec.job.model_copy(deep=True)
            return None

    def set_job_status(self, device_id: str, job_id: str, status: str) -> JobRec:
        with self._lock:
            rec = self.jobs.get(job_id)
            if rec is None or rec.job.device_id != device_id:
                raise NotFound(job_id)
            if status not in _FORWARD.get(rec.status, set()):
                raise Conflict("conflict", f"{rec.status} -> {status} not allowed")
            rec.status = status
            rec.updated_at = self.now()
            return copy.deepcopy(rec)

    def submit_job_result(self, device_id: str, result: JobResult) -> tuple[JobRec, bool]:
        with self._lock:
            rec = self.jobs.get(result.job_id)
            if rec is None or rec.job.device_id != device_id:
                raise NotFound(result.job_id)
            if rec.result is not None:
                if rec.result.idempotency_key == result.idempotency_key:
                    return copy.deepcopy(rec), True
                raise Conflict("conflict", "different result already stored")
            missing = self.missing_event_ids(rec.job.session_id, result.event_ids)
            if missing:
                raise Conflict("events_missing", ",".join(missing))
            rec.result = result.model_copy(deep=True)
            if rec.status not in TERMINAL_JOB:
                rec.status = result.outcome.value
            rec.updated_at = self.now()
            return copy.deepcopy(rec), False

    def get_job(self, job_id: str) -> JobRec:
        with self._lock:
            rec = self.jobs.get(job_id)
            if rec is None:
                raise NotFound(job_id)
            if self.responder is not None and rec.status not in TERMINAL_JOB:
                self.responder(self, rec)
            return copy.deepcopy(self.jobs[job_id])

    def expire_jobs(self, now: float) -> int:
        with self._lock:
            n = 0
            for rec in self.jobs.values():
                if rec.status not in TERMINAL_JOB and rec.created_at + rec.job.deadline_s < now:
                    rec.status = "expired"
                    rec.updated_at = now
                    n += 1
            return n

    def _end_unstarted(self, job_id: str, status: str, reason: str) -> JobRec:
        """Same rule as SqliteStore: only a job that cannot have started (queued, awaiting confirmation,
        or delivered and gated) ends; any other job is returned unchanged."""
        with self._lock:
            rec = self.jobs.get(job_id)
            if rec is None:
                raise NotFound(job_id)
            gated = rec.job.requires_local_confirmation
            if rec.status in ("queued", "awaiting_local_confirmation") or (
                rec.status == "delivered" and gated
            ):
                rec.status = status
                rec.updated_at = self.now()
                self.job_reasons[job_id] = reason
            return copy.deepcopy(rec)

    def expire_job(self, job_id: str, reason: str) -> JobRec:
        return self._end_unstarted(job_id, "expired", reason)

    def cancel_job(self, job_id: str, reason: str) -> JobRec:
        return self._end_unstarted(job_id, "cancelled", reason)

    def cancel_jobs_for_run(self, run_id: str) -> int:
        with self._lock:
            n = 0
            for rec in self.jobs.values():
                if rec.run_id == run_id and rec.status == "queued":
                    rec.status = "cancelled"
                    n += 1
            return n

    # runs
    def create_run(self, session_id: str, question: str, provider: dict) -> RunRec:
        with self._lock:
            self.get_session(session_id)
            now = self.now()
            run = RunRec(
                run_id=f"run-{next(self._run_seq):06d}",
                session_id=session_id,
                question=question,
                status="queued",
                provider=dict(provider),
                created_at=now,
                updated_at=now,
            )
            self.runs[run.run_id] = run
            return copy.deepcopy(run)

    def get_run(self, run_id: str, device_id: str | None = None) -> RunRec:
        with self._lock:
            r = self.runs.get(run_id)
            if r is None:
                raise NotFound(run_id)
            if device_id is not None and self.sessions[r.session_id].device_id != device_id:
                raise NotFound(run_id)
            return copy.deepcopy(r)

    def list_runs(self, session_id: str) -> list[RunRec]:
        with self._lock:
            return [copy.deepcopy(r) for r in self.runs.values() if r.session_id == session_id]

    def save_run(self, run: RunRec) -> None:
        with self._lock:
            if run.run_id not in self.runs:
                raise NotFound(run.run_id)
            self.runs[run.run_id] = copy.deepcopy(run)  # full overwrite (worst case for cancel races)

    def request_run_cancel(self, run_id: str, device_id: str | None = None) -> RunRec:
        with self._lock:
            self.get_run(run_id, device_id)
            self.runs[run_id].cancel_requested = True
            self.cancel_jobs_for_run(run_id)
            return copy.deepcopy(self.runs[run_id])

    # findings
    def add_finding(self, f: Finding) -> None:
        with self._lock:
            self.findings.setdefault(f.session_id, []).append(f.model_copy(deep=True))

    def list_findings(self, session_id: str) -> list[Finding]:
        with self._lock:
            return [f.model_copy(deep=True) for f in self.findings.get(session_id, [])]


# ---------------------------------------------------------------- factories
DEVICE_ID = "sim-cardputer-01"
SESSION_ID = "sess-test-01"
TARGET_ID = "target-lab-01"
ALL_CAPS = list(DEVICE_OPERATIONS)
REAL_FW_CAPS = ["wifi_survey", "ble_discover", "ble_inspect_gatt", "ble_read_profile_value"]
_evt_seq = itertools.count(1000)


def make_settings(tmp_path: Path, **over) -> Settings:
    budgets = over.pop("budgets", RunBudgets())
    s = Settings(
        data_dir=Path(tmp_path),
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token="admin-token-for-tests-0123456789",
        tls_cert=Path(tmp_path) / "c.crt",
        tls_key=Path(tmp_path) / "c.key",
        ai_provider="fake",
        ai_base_url="https://api.groq.com/openai/v1",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_context_events=40,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
        budgets=budgets,
    )
    return dataclasses.replace(s, **over)


def lab_target(target_id: str = TARGET_ID, **over) -> TargetProfile:
    data = {
        "target_id": target_id,
        "display_name": "Oyster lab fixture",
        "profile_id": "oyster-lab-v1",
        "scope_version": 1,
        "allowed_operations": [
            "ble_inspect_gatt",
            "ble_read_profile_value",
            "ble_observe_notifications",
            "ble_check_access_policy",
            "ble_write_lab_value",
            "ble_collect_diagnostics",
        ],
        "identity": {
            "advertised_name": "OysterLab",
            "service_uuid": "862f20d9-fa73-5f89-b130-86ee93e78261",
            "address": None,
            "address_type": "unknown",
        },
        "notes": None,
    }
    data.update(over)
    return TargetProfile.model_validate(data)


def make_event(
    operation: str,
    details: dict,
    *,
    session_id: str = SESSION_ID,
    device_id: str = DEVICE_ID,
    status: str = "success",
    source_mode: str = "simulated",
    target_id: str | None = None,
    event_id: str | None = None,
    sequence: int | None = None,
) -> Event:
    n = next(_evt_seq)
    transport = "wifi" if operation == "wifi_survey" else "ble"
    return Event(
        event_id=event_id or f"evt-{n:06d}",
        session_id=session_id,
        device_id=device_id,
        boot_id="boot-test-01",
        sequence=n if sequence is None else sequence,
        source_mode=source_mode,
        transport=transport,
        target_id=target_id,
        operation=operation,
        timestamp_utc=None,
        clock_quality="unknown",
        uptime_ms=1000 + n,
        status=status,
        details=EventDetails(**details),
    )


def setup_lab(
    store: FakeStore,
    *,
    capabilities: list[str] | None = None,
    source_mode: str = "simulated",
    session_id: str = SESSION_ID,
    device_id: str = DEVICE_ID,
    targets: list[TargetProfile] | None = None,
) -> SessionRec:
    if device_id not in store.devices:
        store.create_device(device_id, "Simulated Cardputer", True)
    for t in targets if targets is not None else [lab_target()]:
        store.upsert_target(t)
    sess, _ = store.register_session(
        device_id, session_id, source_mode, ALL_CAPS if capabilities is None else capabilities, "lab-bench"
    )
    return sess


def default_details(job: Job) -> tuple[str, list[tuple[str, dict]]]:
    """(outcome, [(event_status, details)]) a plausible simulated device produces for a job."""
    op = job.operation.value
    a = job.arguments
    if op == "wifi_survey":
        return "succeeded", [
            (
                "success",
                {
                    "scan": {"mode": "passive"},
                    "complete": True,
                    "overflow_count": 0,
                    "observations": [
                        {
                            "ssid_display": "Oyster2",
                            "bssid": "02:00:00:00:00:01",
                            "channel": 6,
                            "rssi_dbm": -48,
                            "auth_mode": "WPA2_PSK",
                            "wps": "unknown",
                            "pmf": "unknown",
                        }
                    ],
                },
            )
        ]
    if op == "ble_read_profile_value":
        return "succeeded", [
            (
                "success",
                {
                    "profile_id": "oyster-lab-v1",
                    "characteristic_id": a["characteristic_id"],
                    "value_hex": "e803",
                    "value_length": 2,
                    "security_state": {
                        "encrypted": False,
                        "authenticated_pairing": False,
                        "application_authorized": False,
                    },
                },
            )
        ]
    if op == "ble_check_access_policy":
        return "succeeded", [
            (
                "success",
                {
                    "profile_id": "oyster-lab-v1",
                    "test_id": a["test_id"],
                    "characteristic_id": "restricted_enc",
                    "precondition": "connected_unencrypted",
                    "precondition_established": True,
                    "access": "returned",
                    "app_status": None,
                    "raw_att_status": None,
                    "security_state": {
                        "encrypted": False,
                        "authenticated_pairing": False,
                        "application_authorized": False,
                    },
                    "security_transition": False,
                    "value_length": 10,
                    "value_sha256": "ab" * 32,
                    "hash_verified": None,
                },
            )
        ]
    if op == "ble_write_lab_value":
        return "succeeded", [
            (
                "success",
                {"characteristic_id": a["characteristic_id"], "requested": a["value"], "restored": True},
            )
        ]
    return "succeeded", [("success", {"note": "simulated"})]


class AutoDevice:
    """Simulated device driven by the orchestrator's polling (FakeStore.responder).

    mode: "complete" (deliver + confirm when needed + submit result on first poll),
          "never" (deliver, then stay running/awaiting confirmation forever),
          "unknown_outcome" (result outcome unknown_outcome, no events).
    """

    def __init__(
        self,
        *,
        mode: str = "complete",
        behaviours: dict[str, Callable] | None = None,
        source_mode: str = "simulated",
        on_job: Callable[[FakeStore, JobRec], None] | None = None,
    ):
        self.mode = mode
        self.behaviours = behaviours or {}
        self.source_mode = source_mode
        self.on_job = on_job
        self.seen: list[str] = []

    def __call__(self, store: FakeStore, rec: JobRec) -> None:
        job = rec.job
        dev = job.device_id
        if rec.status == "queued":
            rec.status = "delivered"  # this exact job (next_job_for_device would pick the oldest)
            self.seen.append(job.job_id)
            if self.on_job:
                self.on_job(store, rec)
            if job.requires_local_confirmation:
                store.set_job_status(dev, job.job_id, "awaiting_local_confirmation")
            else:
                store.set_job_status(dev, job.job_id, "running")
            if self.mode == "never":
                return
        if self.mode == "never":
            return
        cur = store.jobs[job.job_id]
        if cur.status == "awaiting_local_confirmation":
            store.set_job_status(dev, job.job_id, "running")
        if self.mode == "unknown_outcome":
            res = JobResult(
                job_id=job.job_id,
                idempotency_key=f"idem-{job.job_id}",
                outcome="unknown_outcome",
                error="connection lost after the write was sent",
            )
            store.submit_job_result(dev, res)
            return
        behaviour = self.behaviours.get(job.operation.value, default_details)
        outcome, evs = behaviour(job)
        events = [
            make_event(
                job.operation.value,
                d,
                session_id=job.session_id,
                device_id=dev,
                status=s,
                source_mode=self.source_mode,
                target_id=job.target_id,
            )
            for s, d in evs
        ]
        store.add_events(dev, job.session_id, events)
        store.submit_job_result(
            dev,
            JobResult(
                job_id=job.job_id,
                idempotency_key=f"idem-{job.job_id}",
                outcome=outcome,
                event_ids=[e.event_id for e in events],
            ),
        )


def fake_evaluator(calls: list | None = None, classification: str = "confirmed"):
    """Stand-in for oyster.policy.evaluators.evaluate_assertion (deterministic)."""
    seq = itertools.count(1)

    def evaluate(profile, test_id, events, *, session_id, target_id, retest_of=None):
        if calls is not None:
            calls.append(
                {"test_id": test_id, "event_ids": [e.event_id for e in events], "retest_of": retest_of}
            )
        return Finding(
            finding_id=f"finding-{next(seq):04d}",
            session_id=session_id,
            target_id=target_id,
            profile_id=profile["profile_id"],
            test_id=test_id,
            classification=classification,
            title=f"{test_id} evaluated",
            expected_policy="denied",
            observed_behavior="returned",
            evidence_ids=[e.event_id for e in events],
            severity="medium",
            confidence="medium",
            source_mode="simulated",
            retest_of=retest_of,
        )

    return evaluate
