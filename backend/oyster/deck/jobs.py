"""Control Deck jobs: state machine, idempotency, local confirmation and deadlines.

States and the explicit transition table (`TRANSITIONS`, checked by `can_transition`):

    queued -> awaiting_confirmation -> running -> succeeded | failed | unknown_outcome
    queued -> running                      (non-impactful actions skip confirmation)
    queued | awaiting_confirmation -> cancelled | expired
    queued -> failed                       (runner queue full, restart before the runner took it)

`running` means the job was handed to the runner: an approved impactful job is `running` from the moment
the device confirms it. A running job cannot be cancelled. `unknown_outcome` exists for impactful jobs only:
the action may have run but its result is lost. It is never retried automatically.

Rules enforced here (ADR-005): impactfulness comes only from the action type (actions.py); a confirmation
must come from the SAME authenticated device with the exact `{job_id, nonce}` inside the 120 s window; a
confirmation carried in the run request is never read; an expired or decided job cannot be started by a
late confirm. Idempotency: the `Idempotency-Key` (per device) maps to exactly one job; the same key with the
same request returns the stored job, with a different request it is a 409, and a key never re-executes.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import queue
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from oyster.api.errors import ApiError
from oyster.deck import audit
from oyster.deck.actions import ALLOWLIST
from oyster.deck.audit import AuditEntry
from oyster.deck.profiles import Profile, ProfileInvalid, dump_profile, load_profile

if TYPE_CHECKING:
    from oyster.deck.store import DeckStore

QUEUED = "queued"
AWAITING = "awaiting_confirmation"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"
UNKNOWN_OUTCOME = "unknown_outcome"

STATES = (QUEUED, AWAITING, RUNNING, SUCCEEDED, FAILED, CANCELLED, EXPIRED, UNKNOWN_OUTCOME)
TERMINAL = (SUCCEEDED, FAILED, CANCELLED, EXPIRED, UNKNOWN_OUTCOME)
LIVE = (QUEUED, AWAITING, RUNNING)

TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({AWAITING, RUNNING, CANCELLED, EXPIRED, FAILED}),
    AWAITING: frozenset({RUNNING, CANCELLED, EXPIRED}),
    RUNNING: frozenset({SUCCEEDED, FAILED, UNKNOWN_OUTCOME}),
    **{s: frozenset() for s in TERMINAL},
}

KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,80}$"
KEY_MIN, KEY_MAX = 8, 80


def can_transition(old: str, new: str, impactful: bool) -> bool:
    """Legal transitions only. Confirmation and unknown_outcome apply to impactful jobs; an impactful job
    reaches `running` only through its confirmation, never directly from `queued`."""
    if new not in TRANSITIONS.get(old, ()):
        return False
    if new == AWAITING or new == UNKNOWN_OUTCOME:
        return impactful
    if old == QUEUED and new == RUNNING:
        return not impactful
    return True


class DeckError(ApiError):
    """An API error raised by the service layer (same body shape as every other route)."""


class IllegalTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class DeckConfig:
    confirm_window_s: float = 120.0  # from job creation to the confirmation deadline
    queue_wait_s: float = 120.0  # longest an accepted job may wait for the runner
    run_grace_s: float = 10.0  # added to the summed action timeouts to form a job's run budget
    runs_per_minute: int = 20  # run requests per device
    confirms_per_minute: int = 60  # confirm and cancel requests per device
    queue_size: int = 16
    max_profiles: int = 32


@dataclass(frozen=True)
class DeckJob:
    job_id: str
    device_id: str
    profile_id: str
    slot: int
    key_hash: str
    fingerprint: str
    impactful: bool
    executor: str
    state: str
    actions: tuple[dict, ...]  # snapshot of the allowlisted actions taken when the job was created
    steps: tuple[dict, ...]
    created_at: float
    updated_at: float
    run_budget_s: float
    reason: str | None = None
    nonce: str | None = None
    confirm_deadline: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    seq: int = 0

    @property
    def action_types(self) -> tuple[str, ...]:
        return tuple(a["type"] for a in self.actions)


def pending_steps(actions: tuple[dict, ...]) -> tuple[dict, ...]:
    return tuple(
        {"index": i, **{k: a[k] for k in ("type", "host_id", "service") if k in a}, "outcome": "pending"}
        for i, a in enumerate(actions)
    )


def job_wire(job: DeckJob, *, view: str) -> dict:
    """`view` is "device" (the owning device: sees its nonce while awaiting) or "owner" (never the nonce)."""
    d: dict = {
        "job_id": job.job_id,
        "profile_id": job.profile_id,
        "slot": job.slot,
        "state": job.state,
        "requires_confirmation": job.impactful,
        "executor": job.executor,
        "created_at": int(job.created_at),
        "steps": list(job.steps),
    }
    if job.state == AWAITING:
        if job.confirm_deadline is not None:
            d["confirm_deadline"] = int(math.ceil(job.confirm_deadline))
        if view == "device" and job.nonce:
            d["nonce"] = job.nonce
    if job.reason:
        d["reason"] = job.reason
    if job.finished_at is not None:
        d["finished_at"] = int(job.finished_at)
    if job.state == UNKNOWN_OUTCOME:
        d["outcome_known"] = False
        d["note"] = "The action may have run. Check the host before running it again; it is never retried."
    if view == "owner":
        d["device_id"] = job.device_id
    return d


class RateLimiter:
    """Sliding-window limiter per key (clock injectable). `check` returns None when allowed, else the
    seconds to wait."""

    def __init__(
        self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.time, max_keys: int = 1024
    ):
        self.limit, self.window_s, self._clock, self.max_keys = limit, window_s, clock, max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> int | None:
        with self._lock:
            now = self._clock()
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, math.ceil(q[0] + self.window_s - now))
            q.append(now)
            if len(self._hits) > self.max_keys:
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= now - self.window_s]:
                    self._hits.pop(k, None)
            return None


def _fingerprint(profile_id: str, slot: int) -> str:
    """The identity of a run request. The body is not part of it: it carries nothing the server reads."""
    return hashlib.sha256(f"{profile_id}\0{slot}".encode()).hexdigest()


_SYSTEM = ("system", "deck")


class DeckService:
    """All job logic. Every state change is one transaction that also writes its audit row."""

    def __init__(
        self,
        store: DeckStore,
        *,
        clock: Callable[[], float] = time.time,
        config: DeckConfig | None = None,
        executor_kind: str = "mock",
        new_id: Callable[[], str] | None = None,
        new_nonce: Callable[[], str] | None = None,
    ):
        self.store = store
        self.clock = clock
        self.config = config or DeckConfig()
        self.executor_kind = executor_kind
        self.new_id = new_id or (lambda: "dj-" + secrets.token_hex(8))
        self.new_nonce = new_nonce or (lambda: secrets.token_urlsafe(16))
        self._submit: Callable[[str], None] = lambda job_id: None
        self._has_capacity: Callable[[], bool] = lambda: True

    def attach_runner(self, submit: Callable[[str], None], has_capacity: Callable[[], bool]) -> None:
        self._submit, self._has_capacity = submit, has_capacity

    # --- internals ---------------------------------------------------------------------------------

    def _audit(
        self, conn, actor: tuple[str, str], job: DeckJob, *, event: str, outcome: str, reason: str | None
    ) -> None:
        audit.insert_entry(
            conn,
            AuditEntry(
                ts=self.clock(),
                actor_kind=actor[0],
                actor_id=actor[1],
                device_id=job.device_id,
                event=event,
                outcome=outcome,
                reason=reason,
                profile_id=job.profile_id,
                slot=job.slot,
                action_types=job.action_types,
                job_id=job.job_id,
                key_hash=job.key_hash,
            ),
        )

    def _move(
        self, conn, job: DeckJob, new: str, *, actor: tuple[str, str], reason: str | None = None, **changes
    ) -> DeckJob:
        if not can_transition(job.state, new, job.impactful):
            raise IllegalTransition(f"{job.state} -> {new}")
        now = self.clock()
        if job.state == AWAITING:
            changes.setdefault("nonce", None)  # a nonce is single-use
        if new in TERMINAL:
            changes.setdefault("finished_at", now)
        moved = replace(job, state=new, reason=reason, updated_at=now, **changes)
        self.store.update_job(conn, job.state, moved)
        self._audit(
            conn,
            actor,
            moved,
            event=f"job.{new}",
            outcome=new if new in TERMINAL else "pending",
            reason=reason,
        )
        return moved

    def _deny(
        self,
        conn,
        actor: tuple[str, str],
        status: int,
        code: str,
        detail: str,
        *,
        reason: str,
        device_id: str | None,
        profile_id: str | None = None,
        slot: int | None = None,
        job_id: str | None = None,
        key_hash: str | None = None,
        action_types: tuple[str, ...] = (),
        headers: dict | None = None,
    ) -> DeckError:
        audit.insert_entry(
            conn,
            AuditEntry(
                ts=self.clock(),
                actor_kind=actor[0],
                actor_id=actor[1],
                device_id=device_id,
                event="request.denied",
                outcome="denied",
                reason=reason,
                profile_id=profile_id,
                slot=slot,
                action_types=action_types,
                job_id=job_id,
                key_hash=key_hash,
            ),
        )
        return DeckError(status, code, detail, headers)

    def _sweep(self, conn, job: DeckJob, now: float) -> DeckJob:
        """Apply deadlines to one job (lazily on access, and periodically by the runner)."""
        if job.state in TERMINAL:
            return job
        cfg = self.config
        if job.state == AWAITING and job.confirm_deadline is not None and now >= job.confirm_deadline:
            return self._move(conn, job, EXPIRED, actor=_SYSTEM, reason="confirmation_timeout")
        if job.state == QUEUED and now >= job.created_at + cfg.queue_wait_s:
            return self._move(conn, job, EXPIRED, actor=_SYSTEM, reason="queue_timeout")
        if job.state == RUNNING:
            started = job.started_at is not None
            limit = job.run_budget_s if started else cfg.queue_wait_s
            if now >= (job.started_at if started else job.updated_at) + limit:
                new = UNKNOWN_OUTCOME if job.impactful else FAILED
                why = "run_timeout" if started else "stalled"
                return self._move(conn, job, new, actor=_SYSTEM, reason=why)
        return job

    @staticmethod
    def _not_found() -> tuple[int, str, str]:
        return 404, "not_found", "unknown id"

    # --- profiles (owner) --------------------------------------------------------------------------

    def put_profile(self, profile: Profile) -> str:
        """Create or replace a profile. Replacing with different content cancels its unstarted jobs."""
        body = dump_profile(profile)
        owner = ("owner", "owner")
        with self.store.tx() as conn:
            old = self.store.get_profile_json(conn, profile.profile_id)
            if old is None and self.store.count_profiles(conn) >= self.config.max_profiles:
                raise DeckError(409, "limit_reached", f"at most {self.config.max_profiles} profiles")
            self.store.put_profile(conn, profile.profile_id, body, self.clock())
            result = "created" if old is None else ("unchanged" if old == body else "updated")
            audit.insert_entry(
                conn,
                AuditEntry(
                    ts=self.clock(),
                    actor_kind=owner[0],
                    actor_id=owner[1],
                    event="profile.put",
                    outcome="ok",
                    reason=result,
                    profile_id=profile.profile_id,
                ),
            )
            if result == "updated":
                self._cancel_unstarted(conn, profile.profile_id, owner, "profile_changed")
        return result

    def delete_profile(self, profile_id: str) -> None:
        owner = ("owner", "owner")
        with self.store.tx() as conn:
            if not self.store.delete_profile(conn, profile_id):
                raise DeckError(*self._not_found())
            audit.insert_entry(
                conn,
                AuditEntry(
                    ts=self.clock(),
                    actor_kind=owner[0],
                    actor_id=owner[1],
                    event="profile.deleted",
                    outcome="ok",
                    profile_id=profile_id,
                ),
            )
            self._cancel_unstarted(conn, profile_id, owner, "profile_deleted")

    def _cancel_unstarted(self, conn, profile_id: str, actor: tuple[str, str], reason: str) -> None:
        for job in self.store.live_jobs(conn, profile_id=profile_id):
            if job.state in (QUEUED, AWAITING):
                self._move(conn, job, CANCELLED, actor=actor, reason=reason)

    def get_profile(self, profile_id: str) -> Profile:
        with self.store.tx() as conn:
            body = self.store.get_profile_json(conn, profile_id)
        if body is None:
            raise DeckError(*self._not_found())
        try:
            return load_profile(body)
        except ProfileInvalid:
            raise DeckError(409, "profile_invalid", "stored profile no longer passes validation") from None

    def list_profiles(
        self, device_id: str | None = None, after: str | None = None, limit: int = 6
    ) -> tuple[list[Profile], str | None]:
        """Valid profiles ordered by id; a device sees only those bound to it. Returns (page, next_after)."""
        with self.store.tx() as conn:
            rows = self.store.list_profile_json(conn)
        out: list[Profile] = []
        for pid, body in rows:
            if after is not None and pid <= after:
                continue
            try:
                p = load_profile(body)
            except ProfileInvalid:
                continue  # fail closed: an invalid stored profile is invisible and cannot run
            if device_id is None or device_id in p.device_ids:
                out.append(p)
        more = len(out) > limit
        page = out[:limit]
        return page, (page[-1].profile_id if more and page else None)

    # --- run ---------------------------------------------------------------------------------------

    def run_slot(self, device_id: str, profile_id: str, slot: int, key: str) -> tuple[DeckJob, bool]:
        """Admit a run request. Returns (job, duplicate). Never executes anything itself."""
        kh = audit.key_hash(device_id, key)
        fp = _fingerprint(profile_id, slot)
        actor = ("device", device_id)
        with self.store.tx() as conn:
            now = self.clock()
            existing = self.store.job_by_key(conn, device_id, kh)
            if existing is not None:
                if existing.fingerprint == fp:
                    return self._sweep(conn, existing, now), True
                result: DeckJob | DeckError = self._deny(
                    conn,
                    actor,
                    409,
                    "conflict",
                    "this Idempotency-Key was used for a different request",
                    reason="idempotency_conflict",
                    device_id=device_id,
                    profile_id=profile_id,
                    slot=slot,
                    key_hash=kh,
                )
            else:
                result = self._admit(conn, now, device_id, profile_id, slot, kh, fp)
        if isinstance(result, DeckError):
            raise result
        if not result.impactful:
            self._enqueue(result.job_id)
        return result, False

    def _admit(
        self, conn, now: float, device_id: str, profile_id: str, slot: int, kh: str, fp: str
    ) -> DeckJob | DeckError:
        actor = ("device", device_id)
        ctx = {"device_id": device_id, "profile_id": profile_id, "slot": slot, "key_hash": kh}
        body = self.store.get_profile_json(conn, profile_id)
        if body is None:
            return self._deny(conn, actor, 404, "not_found", "unknown id", reason="unknown_profile", **ctx)
        try:
            profile = load_profile(body)
        except ProfileInvalid as e:
            return self._deny(
                conn,
                actor,
                409,
                e.reason,
                "the stored profile does not pass validation",
                reason=e.reason,
                **ctx,
            )
        if device_id not in profile.device_ids:
            return self._deny(conn, actor, 404, "not_found", "unknown id", reason="not_bound", **ctx)
        chosen = profile.slot(slot)
        if chosen is None:
            return self._deny(conn, actor, 404, "not_found", "unknown id", reason="empty_slot", **ctx)
        actions = tuple(a.model_dump(mode="json") for a in chosen.actions)
        impactful = chosen.impactful
        if not impactful and not self._has_capacity():
            return self._deny(
                conn,
                actor,
                503,
                "busy",
                "the job queue is full; retry shortly",
                reason="busy",
                action_types=tuple(a["type"] for a in actions),
                headers={"Retry-After": "5"},
                **ctx,
            )
        budget = sum(ALLOWLIST[a["type"]].timeout_s for a in actions) + self.config.run_grace_s
        job = DeckJob(
            job_id=self.new_id(),
            device_id=device_id,
            profile_id=profile_id,
            slot=slot,
            key_hash=kh,
            fingerprint=fp,
            impactful=impactful,
            executor=self.executor_kind,
            state=QUEUED,
            actions=actions,
            steps=pending_steps(actions),
            created_at=now,
            updated_at=now,
            run_budget_s=budget,
        )
        job = self.store.insert_job(conn, job)
        self._audit(conn, actor, job, event="job.queued", outcome="pending", reason=None)
        if impactful:
            job = self._move(
                conn,
                job,
                AWAITING,
                actor=actor,
                nonce=self.new_nonce(),
                confirm_deadline=now + self.config.confirm_window_s,
            )
        return job

    def _enqueue(self, job_id: str) -> None:
        try:
            self._submit(job_id)
        except queue.Full:
            self._fail_unqueued(job_id)
            raise DeckError(
                503, "busy", "the job queue is full; the job was not started", {"Retry-After": "5"}
            ) from None

    def _fail_unqueued(self, job_id: str) -> None:
        with self.store.tx() as conn:
            job = self.store.get_job(conn, job_id)
            if job is not None and job.state in (QUEUED, RUNNING) and job.started_at is None:
                self._move(conn, job, FAILED, actor=_SYSTEM, reason="queue_full")

    # --- confirm / cancel / read --------------------------------------------------------------------

    def confirm(self, device_id: str, job_id: str, body_job_id: str, nonce: str, approved: bool) -> DeckJob:
        actor = ("device", device_id)
        with self.store.tx() as conn:
            now = self.clock()
            job = self.store.get_job(conn, job_id)
            if job is None or job.device_id != device_id:
                outcome: DeckJob | DeckError = self._deny(
                    conn, actor, *self._not_found(), reason="unknown_job", device_id=device_id
                )
            else:
                outcome = self._decide(conn, actor, now, job, body_job_id, nonce, approved)
        if isinstance(outcome, DeckError):
            raise outcome
        if approved:
            self._enqueue(outcome.job_id)
        return outcome

    def _decide(
        self, conn, actor, now, job: DeckJob, body_job_id: str, nonce: str, approved: bool
    ) -> DeckJob | DeckError:
        ctx = {
            "device_id": job.device_id,
            "profile_id": job.profile_id,
            "slot": job.slot,
            "job_id": job.job_id,
            "key_hash": job.key_hash,
            "action_types": job.action_types,
        }
        if body_job_id != job.job_id:
            return self._deny(
                conn,
                actor,
                422,
                "validation_failed",
                "job_id in the body does not match the URL",
                reason="job_id_mismatch",
                **ctx,
            )
        job = self._sweep(conn, job, now)
        if job.state != AWAITING:
            if job.state == EXPIRED:
                return self._deny(
                    conn, actor, 409, "expired", "the confirmation window has closed", reason="expired", **ctx
                )
            return self._deny(
                conn,
                actor,
                409,
                "already_decided",
                "the job is no longer awaiting confirmation",
                reason="not_awaiting",
                **ctx,
            )
        if not job.nonce or not hmac.compare_digest(job.nonce.encode(), nonce.encode()):
            return self._deny(
                conn, actor, 403, "forbidden", "confirmation rejected", reason="bad_nonce", **ctx
            )
        if not approved:
            return self._move(conn, job, CANCELLED, actor=actor, reason="declined")
        if not self._has_capacity():
            return self._deny(
                conn,
                actor,
                503,
                "busy",
                "the job queue is full; confirm again shortly",
                reason="busy",
                headers={"Retry-After": "5"},
                **ctx,
            )
        return self._move(conn, job, RUNNING, actor=actor)

    def cancel(self, device_id: str, job_id: str) -> DeckJob:
        actor = ("device", device_id)
        with self.store.tx() as conn:
            now = self.clock()
            job = self.store.get_job(conn, job_id)
            if job is None or job.device_id != device_id:
                err = self._deny(conn, actor, *self._not_found(), reason="unknown_job", device_id=device_id)
            else:
                job = self._sweep(conn, job, now)
                if job.state == CANCELLED:
                    return job
                if job.state in (QUEUED, AWAITING):
                    return self._move(conn, job, CANCELLED, actor=actor, reason="cancelled_by_device")
                err = self._deny(
                    conn,
                    actor,
                    409,
                    "not_cancellable",
                    "a started or finished job cannot be cancelled",
                    reason="not_cancellable",
                    device_id=device_id,
                    profile_id=job.profile_id,
                    slot=job.slot,
                    job_id=job.job_id,
                    key_hash=job.key_hash,
                    action_types=job.action_types,
                )
        raise err

    def get_job(self, job_id: str, device_id: str | None = None) -> DeckJob:
        """`device_id=None` is the owner (any job); a device gets 404 for any job that is not its own."""
        with self.store.tx() as conn:
            job = self.store.get_job(conn, job_id)
            if job is None or (device_id is not None and job.device_id != device_id):
                raise DeckError(*self._not_found())
            return self._sweep(conn, job, self.clock())

    def list_jobs(
        self,
        *,
        device_id: str | None = None,
        state: str | None = None,
        before: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[DeckJob], int | None]:
        """Newest first. Returns (page, next_before): `next_before` is set when more rows exist."""
        n = audit.clamp_limit(limit)
        with self.store.tx() as conn:
            self._sweep_all(conn)
            rows = self.store.list_jobs(conn, device_id=device_id, state=state, before=before, limit=n + 1)
        return rows[:n], (rows[n - 1].seq if len(rows) > n else None)

    def history(
        self,
        *,
        device_id: str | None = None,
        job_id: str | None = None,
        before: int | None = None,
        limit: int | None = None,
    ) -> tuple[list[AuditEntry], int | None]:
        """Newest first. Returns (page, next_before): `next_before` is set when more rows exist."""
        n = audit.clamp_limit(limit)
        with self.store.tx() as conn:
            rows = audit.fetch_page(conn, device_id=device_id, job_id=job_id, before=before, limit=n + 1)
        return rows[:n], (rows[n - 1].seq if len(rows) > n else None)

    # --- runner side ---------------------------------------------------------------------------------

    def begin(self, job_id: str) -> DeckJob | None:
        """Claim a job for execution: exactly once, and only if it is still startable (a cancelled,
        expired or already started job returns None)."""
        with self.store.tx() as conn:
            now = self.clock()
            job = self.store.get_job(conn, job_id)
            if job is None:
                return None
            job = self._sweep(conn, job, now)
            if job.state == QUEUED and not job.impactful:
                return self._move(conn, job, RUNNING, actor=_SYSTEM, started_at=now)
            if job.state == RUNNING and job.started_at is None:
                started = replace(job, started_at=now, updated_at=now)
                self.store.update_job(conn, RUNNING, started)
                return started
            return None

    def finish(self, job_id: str, state: str, steps: list[dict], reason: str | None) -> DeckJob | None:
        """Record the outcome of a running job. Ignored (None) when the job already ended, e.g. by a
        deadline sweep: the first recorded outcome stands."""
        with self.store.tx() as conn:
            job = self.store.get_job(conn, job_id)
            if job is None or job.state != RUNNING:
                return None
            if state == UNKNOWN_OUTCOME and not job.impactful:
                state = FAILED
            return self._move(conn, job, state, actor=_SYSTEM, reason=reason, steps=tuple(steps))

    def sweep(self) -> int:
        with self.store.tx() as conn:
            return self._sweep_all(conn)

    def _sweep_all(self, conn) -> int:
        now, changed = self.clock(), 0
        for job in self.store.live_jobs(conn):
            if self._sweep(conn, job, now) is not job:
                changed += 1
        return changed

    def recover_after_restart(self) -> dict[str, int]:
        """After a crash or restart nothing is executing: a job found `running` ended unobserved
        (unknown_outcome when impactful, failed otherwise) and a `queued` one never started (failed).
        Nothing is re-run. Jobs awaiting confirmation keep their deadline and nonce."""
        counts = {UNKNOWN_OUTCOME: 0, FAILED: 0}
        with self.store.tx() as conn:
            for job in self.store.live_jobs(conn):
                if job.state == RUNNING:
                    new = UNKNOWN_OUTCOME if job.impactful else FAILED
                elif job.state == QUEUED:
                    new = FAILED
                else:
                    continue
                self._move(conn, job, new, actor=("system", "startup"), reason="backend_restarted")
                counts[new] += 1
        return counts
