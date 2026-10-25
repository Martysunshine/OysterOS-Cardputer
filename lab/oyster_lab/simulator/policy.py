"""Device-local job policy, independent of the backend (mirrors oyster_core/local_policy.h).

The simulator validates every job by itself: ids, target, scope, operation support, and limits. The
local-confirmation requirement is derived ONLY locally: from the operation type, and for
ble_check_access_policy from the selected assertion's `operation` in the enrolled target's lab profile
(write/transfer assertions are gated). A backend/model flag that says `requires_local_confirmation: false`
can never lower it (ADR-005). A job id is never re-executed. This module makes no network calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

DEVICE_OPERATIONS = (
    "wifi_survey",
    "ble_discover",
    "ble_inspect_gatt",
    "ble_read_profile_value",
    "ble_observe_notifications",
    "ble_check_access_policy",
    "ble_write_lab_value",
    "ble_collect_diagnostics",
)

# Confirmation is required for state-changing / sensitive operations: writes and the diagnostic transfer.
_CONFIRMATION_OPS = frozenset({"ble_write_lab_value", "ble_collect_diagnostics"})
# Every ble_* operation needs a resolved enrolled target except plain discovery.
_TARGET_OPS = frozenset(
    {
        "ble_inspect_gatt",
        "ble_read_profile_value",
        "ble_observe_notifications",
        "ble_check_access_policy",
        "ble_write_lab_value",
        "ble_collect_diagnostics",
    }
)
# Operations that may change the target (writes, CCCD subscription, transfer chunk requests, access-policy
# assertions that write) and must never be auto-retried after a timeout. Same set as the backend catalogue.
_STATE_CHANGING_OPS = frozenset(
    {
        "ble_observe_notifications",
        "ble_check_access_policy",
        "ble_write_lab_value",
        "ble_collect_diagnostics",
    }
)
# Assertion operations (lab-profile.json `assertions[].operation`) whose check needs local confirmation.
_CONFIRMED_ASSERTION_OPERATIONS = frozenset({"write", "transfer"})

_ID_RE = re.compile(r"^[0-9a-zA-Z_-]{1,40}$")
_SESSION_RE = re.compile(r"^sess-[0-9a-zA-Z_-]{1,40}$")
_JOB_RE = re.compile(r"^job-[0-9a-zA-Z_-]{1,40}$")
_TARGET_RE = re.compile(r"^target-[0-9a-zA-Z_-]{1,40}$")
OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
# Public characteristics a remote job must not read: reading app_auth replaces the fixture's single
# outstanding challenge (firmware lab_remote_read_allowed()).
CHALLENGE_CHARACTERISTICS = frozenset({"app_auth"})


def remote_read_allowed(profile: dict[str, Any] | None, characteristic_id: str) -> bool:
    """A remote job may read only public-policy, side-effect-free characteristics."""
    c = ((profile or {}).get("characteristics") or {}).get(characteristic_id) or {}
    return (
        "read" in c.get("properties", [])
        and c.get("policy", {}).get("read") == "public"
        and characteristic_id not in CHALLENGE_CHARACTERISTICS
    )


def _is_int(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _is_num(v: Any, lo: float, hi: float) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and lo < v <= hi


def validate_arguments(
    operation: str, arguments: Any, profile: dict[str, Any] | None
) -> str | None:
    """Typed argument check done on the device before anything runs. Returns the reason or None.

    Write values must be a documented enum name, characteristic ids must exist in the enrolled profile
    with the needed property, object ids match ^[A-Za-z0-9_-]{1,16}$ and the profile's object.
    """
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    a = arguments
    for key in ("passive", "show_hidden", "active", "restore", "resume"):
        if key in a and not isinstance(a[key], bool):
            return f"{key} must be a boolean"
    if operation == "wifi_survey":
        if "ms_per_channel" in a and not _is_int(a["ms_per_channel"], 1, 10000):
            return "ms_per_channel out of range"
        if "channel" in a and not _is_int(a["channel"], 0, 14):
            return "channel out of range"
        if "room_label" in a and not (isinstance(a["room_label"], str) and len(a["room_label"]) <= 32):
            return "room_label must be a short string"
        return None
    if operation == "ble_discover":
        if "duration_ms" in a and not _is_int(a["duration_ms"], 1, 60000):
            return "duration_ms out of range"
        for key in ("interval_ms", "window_ms"):
            if key in a and not _is_int(a[key], 1, 10000):
                return f"{key} out of range"
        return None
    if operation not in _TARGET_OPS:
        return None
    if profile is None:
        return "enrolled profile is not available on this device"
    chars = profile.get("characteristics", {})
    if operation == "ble_inspect_gatt":
        if "page_index" in a and not _is_int(a["page_index"], 0, 1000):
            return "page_index out of range"
    elif operation in ("ble_read_profile_value", "ble_observe_notifications"):
        cid = a.get("characteristic_id")
        prop = "read" if operation == "ble_read_profile_value" else "notify"
        if not isinstance(cid, str) or prop not in chars.get(cid, {}).get("properties", []):
            return f"characteristic_id is not a {prop}able characteristic of the profile"
        if operation == "ble_observe_notifications":
            if "duration_s" in a and not _is_num(a["duration_s"], 0, 60):
                return "duration_s out of range"
            if "max_events" in a and not _is_int(a["max_events"], 1, 256):
                return "max_events out of range"
    elif operation == "ble_check_access_policy":
        tests = {x.get("test_id") for x in profile.get("assertions", [])}
        if not isinstance(a.get("test_id"), str) or a["test_id"] not in tests:
            return "test_id is not an assertion of the profile"
    elif operation == "ble_write_lab_value":
        cid = a.get("characteristic_id")
        wv = profile.get("write_values", {}).get(cid) if isinstance(cid, str) else None
        if wv is None:
            return "characteristic_id is not a documented write target"
        if not isinstance(a.get("value"), str) or a["value"] not in wv.get("allowed", {}):
            return "value is not an allowed enum name"
    elif operation == "ble_collect_diagnostics":
        oid = a.get("object_id")
        if oid is not None:
            if not isinstance(oid, str) or not OBJECT_ID_RE.match(oid):
                return "object_id must match ^[A-Za-z0-9_-]{1,16}$"
            if oid != profile.get("transfer", {}).get("object_id"):
                return "object_id is not the profile's diagnostic object"
    return None


def operation_requires_local_confirmation(operation: str) -> bool:
    return operation in _CONFIRMATION_OPS


def operation_requires_target(operation: str) -> bool:
    return operation in _TARGET_OPS


def operation_changes_target_state(operation: str) -> bool:
    return operation in _STATE_CHANGING_OPS


def job_requires_local_confirmation(
    operation: str, arguments: dict[str, Any] | None, profile: dict[str, Any] | None
) -> bool:
    """Confirmation for one job, derived locally: the operation type, or for ble_check_access_policy the
    selected assertion's `operation` in the enrolled profile (write/transfer). Unknown profile or test_id
    -> required. The job's own requires_local_confirmation flag is never consulted."""
    if operation_requires_local_confirmation(operation):
        return True
    if operation == "ble_check_access_policy":
        test_id = (arguments or {}).get("test_id")
        assertion = next(
            (a for a in (profile or {}).get("assertions", []) if a.get("test_id") == test_id), None
        )
        return assertion is None or assertion.get("operation") in _CONFIRMED_ASSERTION_OPERATIONS
    return False


# Per-operation upper bounds on job limits (see local_policy.cpp: scans 30 s, other
# operations cfg::kMaxJobDurationMs = 600 s; transfers legitimately ask for 120 s).
_MAX_SCAN_DURATION_S = 30.0
_MAX_CONNECTED_DURATION_S = 600.0
_MAX_BYTES = 65536
_MAX_RESULTS = 256


def limits_within_bounds(operation: str, limits: dict[str, Any]) -> bool:
    max_results = limits.get("max_results")
    max_duration = limits.get("max_duration_s")
    max_bytes = limits.get("max_bytes")
    if max_results is not None and not (1 <= max_results <= _MAX_RESULTS):
        return False
    if max_bytes is not None and not (1 <= max_bytes <= _MAX_BYTES):
        return False
    if max_duration is not None:
        if max_duration <= 0:
            return False
        ceiling = (
            _MAX_SCAN_DURATION_S
            if operation in ("wifi_survey", "ble_discover")
            else _MAX_CONNECTED_DURATION_S
        )
        if max_duration > ceiling:
            return False
    return True


class Verdict(StrEnum):
    accept = "accept"
    reject_malformed = "reject_malformed"
    reject_unknown_operation = "reject_unknown_operation"
    reject_wrong_device = "reject_wrong_device"
    reject_wrong_session = "reject_wrong_session"
    reject_target_required = "reject_target_required"
    reject_unknown_target = "reject_unknown_target"
    reject_operation_not_allowed = "reject_operation_not_allowed"
    reject_stale_scope = "reject_stale_scope"
    reject_args_too_large = "reject_args_too_large"
    reject_limits_exceeded = "reject_limits_exceeded"
    reject_bad_arguments = "reject_bad_arguments"
    reject_duplicate = "reject_duplicate"


# Verdict -> job-result outcome for a rejected job.
_REJECT_OUTCOME = {
    Verdict.reject_unknown_operation: "unsupported",
    Verdict.reject_duplicate: "cancelled",
}


def reject_outcome(verdict: Verdict) -> str:
    return _REJECT_OUTCOME.get(verdict, "failed")


@dataclass
class Decision:
    verdict: Verdict
    requires_local_confirmation: bool = False
    target: dict[str, Any] | None = None
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.accept


MAX_ARGS_BYTES = 512


@dataclass
class PolicyInput:
    device_id: str
    active_session_id: str
    targets: list[dict[str, Any]] = field(default_factory=list)
    executed_job_ids: frozenset[str] = frozenset()
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)  # profile_id -> lab profile


def _find_target(targets: list[dict[str, Any]], target_id: str | None) -> dict[str, Any] | None:
    if not target_id:
        return None
    for t in targets:
        if t.get("target_id") == target_id:
            return t
    return None


def evaluate_job(job: dict[str, Any], pin: PolicyInput) -> Decision:
    """Validate a job the way the firmware does, deriving confirmation locally (operation type, and the
    enrolled profile's assertion for ble_check_access_policy), never from the job's flag."""
    operation = job.get("operation", "")
    # conservative until the target (and so the enrolled profile) is resolved
    requires_conf = operation_requires_local_confirmation(operation) or (
        operation == "ble_check_access_policy"
    )

    job_id = job.get("job_id", "")
    if not isinstance(job_id, str) or not _JOB_RE.match(job_id):
        return Decision(Verdict.reject_malformed, requires_conf, reason="invalid job_id")

    if operation not in DEVICE_OPERATIONS:
        return Decision(
            Verdict.reject_unknown_operation, requires_conf, reason=f"unknown operation {operation!r}"
        )

    session_id = job.get("session_id", "")
    if not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
        return Decision(Verdict.reject_malformed, requires_conf, reason="invalid session_id")

    device_id = job.get("device_id")
    if device_id not in (None, "") and device_id != pin.device_id:
        return Decision(Verdict.reject_wrong_device, requires_conf, reason="job bound to another device")

    if session_id != pin.active_session_id:
        return Decision(
            Verdict.reject_wrong_session, requires_conf, reason="job is not for the active session"
        )

    if job_id in pin.executed_job_ids:
        return Decision(Verdict.reject_duplicate, requires_conf, reason="job id already executed")

    # Arguments size bound (independent of schema).
    import json as _json

    if len(_json.dumps(job.get("arguments", {})).encode("utf-8")) > MAX_ARGS_BYTES:
        return Decision(Verdict.reject_args_too_large, requires_conf, reason="arguments exceed local bound")

    target_id = job.get("target_id")
    target = None
    if operation_requires_target(operation):
        if not target_id:
            return Decision(
                Verdict.reject_target_required, requires_conf, reason="operation requires a target"
            )
        if not _TARGET_RE.match(str(target_id)):
            return Decision(Verdict.reject_malformed, requires_conf, reason="invalid target_id")
        target = _find_target(pin.targets, target_id)
        if target is None:
            return Decision(Verdict.reject_unknown_target, requires_conf, reason="target not enrolled")
        allowed = target.get("allowed_operations", [])
        if operation not in allowed:
            return Decision(
                Verdict.reject_operation_not_allowed, requires_conf, reason="operation not allowed on target"
            )
        scope = job.get("scope_version")
        if scope != target.get("scope_version"):
            return Decision(
                Verdict.reject_stale_scope, requires_conf, target=target, reason="stale scope_version"
            )
        requires_conf = job_requires_local_confirmation(
            operation, job.get("arguments"), pin.profiles.get(str(target.get("profile_id")))
        )
    elif target_id:
        # A non-target operation that nonetheless names a target: resolve it when known (informational).
        target = _find_target(pin.targets, target_id)

    profile = pin.profiles.get(str(target.get("profile_id"))) if target is not None else None
    bad = validate_arguments(operation, job.get("arguments", {}), profile)
    if bad:
        return Decision(Verdict.reject_bad_arguments, requires_conf, target=target, reason=bad)

    if not limits_within_bounds(operation, job.get("limits", {})):
        return Decision(
            Verdict.reject_limits_exceeded, requires_conf, target=target, reason="limits exceed bounds"
        )

    # A confirmation-required operation must carry a nonce to bind the local approval.
    if requires_conf and not job.get("nonce"):
        return Decision(
            Verdict.reject_malformed, requires_conf, target=target, reason="confirmation op lacks nonce"
        )

    return Decision(Verdict.accept, requires_conf, target=target, reason="ok")


def confirmation_valid(job: dict[str, Any], confirmation: dict[str, Any] | None) -> bool:
    """True only for an approval whose job_id AND nonce match this exact job."""
    if not confirmation:
        return False
    return bool(
        confirmation.get("approved")
        and confirmation.get("job_id") == job.get("job_id")
        and confirmation.get("nonce")
        and confirmation.get("nonce") == job.get("nonce")
    )
