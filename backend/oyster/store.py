"""Persistence seam shared by the API layer and the AI orchestrator.

Implemented with SQLite in oyster/db/sqlite_store.py (ADR-004). The orchestrator depends only on this
Protocol. Methods are synchronous and thread-safe (API handlers and the run worker share one Store).
Ownership: methods taking device_id refuse ids bound to another device with NotFound, never leaking data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from oyster.contracts import Event, Finding, Job, JobResult, TargetProfile
from oyster.settings import RunLimits


class NotFound(LookupError):
    """Unknown id, or an id bound to a different device (indistinguishable on purpose)."""


class Conflict(RuntimeError):
    """Id owned by another device, conflicting duplicate, or invalid lifecycle transition."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class DeviceRec:
    device_id: str
    display_name: str
    simulated: bool


@dataclass
class SessionRec:
    session_id: str
    device_id: str
    source_mode: str                      # "live" | "simulated"
    capabilities: list[str]               # operations the device implements
    room_label: str | None
    created_at: float
    linked_baseline_session_id: str | None = None


@dataclass
class JobRec:
    job: Job
    run_id: str | None
    status: str                           # queued|delivered|awaiting_local_confirmation|running|<terminal>
    created_at: float
    updated_at: float
    result: JobResult | None = None


@dataclass
class RunRec:
    run_id: str
    session_id: str
    question: str
    status: str                           # see schemas/run.schema.json
    provider: dict                        # {"name","model","is_fake"}
    created_at: float
    updated_at: float
    actions_used: int = 0
    provider_requests_used: int = 0
    elapsed_s: float = 0.0
    paused_s: float = 0.0                 # time in local confirmation (excluded from the duration budget)
    steps: list[dict] = field(default_factory=list)
    answer: dict | None = None
    pending_action: dict | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    cancel_requested: bool = False


class Store(Protocol):
    # devices
    def create_device(self, device_id: str, display_name: str, simulated: bool) -> str:
        """Enrol or re-enrol a device; returns its bearer token once."""
        ...

    def device_for_token(self, token: str) -> DeviceRec | None: ...

    # targets
    def upsert_target(self, t: TargetProfile) -> TargetProfile: ...
    def get_target(self, target_id: str) -> TargetProfile | None: ...
    def list_targets(self) -> list[TargetProfile]: ...

    # sessions
    def register_session(self, device_id: str, session_id: str | None, source_mode: str,
                         capabilities: list[str], room_label: str | None) -> tuple[SessionRec, bool]: ...

    def get_session(self, session_id: str, device_id: str | None = None) -> SessionRec:
        """device_id None = owner access."""
        ...

    def list_sessions(self, device_id: str | None = None) -> list[SessionRec]: ...

    def link_baseline(self, session_id: str, baseline_session_id: str | None) -> SessionRec:
        """Owner action: link (or with None unlink) an earlier session as the comparison baseline.
        A baseline owned by a different device is rejected (Conflict validation_failed)."""
        ...

    # events (evidence)
    def add_events(self, device_id: str, session_id: str,
                   events: list[Event]) -> tuple[int, int, int]: ...
    def get_events(self, session_id: str, event_ids: list[str] | None = None,
                   limit: int = 500, newest: bool = False) -> list[Event]:
        """Events in ascending sequence order. Without event_ids, `limit` caps the result: the FIRST
        `limit` events, or with newest=True the LAST `limit` events (still ascending)."""
        ...
    def missing_event_ids(self, session_id: str, event_ids: list[str]) -> list[str]: ...

    # jobs
    def create_job(self, *, run_id: str | None, session_id: str, operation: str, arguments: dict,
                   target_id: str | None, scope_version: int, deadline_s: float, limits: dict,
                   requires_local_confirmation: bool) -> JobRec: ...

    def next_job_for_device(self, device_id: str) -> Job | None:
        """queued -> delivered (at most one job)."""
        ...

    def set_job_status(self, device_id: str, job_id: str, status: str) -> JobRec:
        """Device lifecycle report; Conflict for a non-forward move (e.g. `running` after expiry)."""
        ...

    def submit_job_result(self, device_id: str, result: JobResult) -> tuple[JobRec, bool]:
        """Returns (record, duplicate)."""
        ...

    def get_job(self, job_id: str) -> JobRec: ...
    def expire_jobs(self, now: float) -> int: ...

    def expire_job(self, job_id: str, reason: str) -> JobRec:
        """Backend-side expiry of one job that cannot have started executing -> expired, `reason` recorded:
        queued, awaiting_local_confirmation, or delivered and confirmation-gated (a gated job executes only
        after its `running` report is accepted). Any other job (possibly executing, or terminal) is
        returned unchanged. A later device `running` report for the expired job is rejected (Conflict)."""
        ...

    def cancel_job(self, job_id: str, reason: str) -> JobRec:
        """Like expire_job, but the job ends `cancelled` (the run that asked for it has ended)."""
        ...

    def cancel_jobs_for_run(self, run_id: str) -> int: ...

    # runs
    def create_run(self, session_id: str, question: str, provider: dict,
                   limits: RunLimits | None = None) -> RunRec:
        """With `limits`, Conflict('rate_limited') when the session already has an active run, too many runs
        are queued overall, or the session's device created too many runs in the last hour."""
        ...
    def get_run(self, run_id: str, device_id: str | None = None) -> RunRec: ...
    def list_runs(self, session_id: str) -> list[RunRec]: ...
    def save_run(self, run: RunRec) -> None: ...
    def request_run_cancel(self, run_id: str, device_id: str | None = None) -> RunRec: ...

    # findings
    def add_finding(self, f: Finding) -> None: ...
    def list_findings(self, session_id: str) -> list[Finding]:
        """In insertion order (oldest first)."""
        ...
