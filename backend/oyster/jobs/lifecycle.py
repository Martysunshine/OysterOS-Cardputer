"""Job and run lifecycle rules (docs/API.md). Forward-only; terminal states are final.

queued -> delivered -> [awaiting_local_confirmation] -> running
       -> succeeded | failed | cancelled | expired | unknown_outcome
"""

from __future__ import annotations

from oyster.policy import catalogue

QUEUED = "queued"
DELIVERED = "delivered"
AWAITING = "awaiting_local_confirmation"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
EXPIRED = "expired"
UNKNOWN_OUTCOME = "unknown_outcome"

TERMINAL = (SUCCEEDED, FAILED, CANCELLED, EXPIRED, UNKNOWN_OUTCOME)
ACTIVE = (QUEUED, DELIVERED, AWAITING, RUNNING)
IN_FLIGHT = (DELIVERED, AWAITING, RUNNING)  # the device may already be executing
DEVICE_REPORTABLE = (AWAITING, RUNNING)  # statuses accepted on POST /v1/jobs/{id}/status
_ORDER = {QUEUED: 0, DELIVERED: 1, AWAITING: 2, RUNNING: 3}

# status_reason values recorded with backend-side terminal transitions
REASON_DEADLINE = "deadline"  # expiry sweep; a later result is kept for audit only
REASON_STALE_SCOPE = "stale_scope"  # target scope_version raised before delivery
REASON_RUN_CANCELLED = "run_cancelled"
REASON_RESULT = "result"
REASON_CONFIRMATION_TIMEOUT = "confirmation_timeout"  # orchestrator stopped waiting for local approval
REASON_RUN_ENDED = "run_ended"  # the run that requested the job finished before the job started
REASON_TARGET_CHANGED = "target_policy_changed"  # allowed operations/profile no longer permit the job
REASON_RESTART = "backend_restarted"  # startup reconciliation after a crash or restart

# deadline_s counts from job creation (the caller includes delivery margin and any confirmation wait).

RUN_TERMINAL = ("succeeded", "failed", "cancelled", "expired", "budget_exhausted", "ai_unavailable")


def can_device_transition(current: str, new: str, requires_local_confirmation: bool = False) -> bool:
    """A device status report must move strictly forward from an in-flight state.

    A confirmation-gated job may start (`running`) only from `awaiting_local_confirmation`: the
    confirmation step cannot be skipped by reporting `running` straight after delivery."""
    if requires_local_confirmation and new == RUNNING and current != AWAITING:
        return False
    return current in IN_FLIGHT and new in DEVICE_REPORTABLE and _ORDER[new] > _ORDER[current]


def not_started(status: str, requires_local_confirmation: bool) -> bool:
    """True when the device cannot have started executing the job (expire_job/cancel_job may end it).

    A confirmation-gated job executes only after its `running` report is accepted, so it has not started
    while queued, delivered or awaiting confirmation. A non-gated delivered job may already be running
    (its status report is advisory), so only a queued one is certainly not started.
    """
    if status in (QUEUED, AWAITING):
        return True
    return status == DELIVERED and requires_local_confirmation


def status_for_outcome(outcome: str) -> str:
    """Job status after a device result. 'unsupported' is a failure (the outcome stays on the result)."""
    return FAILED if outcome == "unsupported" else outcome


def expiry_status(operation: str, status: str, requires_local_confirmation: bool = False) -> str:
    """Status a job takes when its deadline passes without a result.

    A job that cannot have started (queued, awaiting confirmation, or delivered and confirmation-gated)
    is expired. An in-flight target-changing job may have executed before connectivity was lost, so it
    becomes unknown_outcome and is never retried automatically.
    """
    if not_started(status, requires_local_confirmation):
        return EXPIRED
    if (
        status in IN_FLIGHT
        and catalogue.is_supported(operation)
        and catalogue.changes_target_state(operation)
    ):
        return UNKNOWN_OUTCOME
    return EXPIRED
