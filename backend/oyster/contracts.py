"""Pydantic models mirroring schemas/*.json. One source of truth for request/response shapes.

A contract test (tests/contracts) proves every schemas/examples/*.json both validates under jsonschema
AND parses into the matching model here, so the two never drift. Wire values must match the JSON schemas
exactly. Absent measurements are None/unknown, never invented defaults.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- enumerations (string values match the schemas) ---
class SourceMode(StrEnum):
    live = "live"
    offline = "offline"
    simulated = "simulated"


class Transport(StrEnum):
    wifi = "wifi"
    ble = "ble"
    backend = "backend"
    local = "local"
    none = "none"


class ClockQuality(StrEnum):
    ntp = "ntp"
    serial = "serial"
    backend_hint = "backend_hint"
    unknown = "unknown"


class EventStatus(StrEnum):
    success = "success"
    denied = "denied"
    timeout = "timeout"
    unsupported = "unsupported"
    error = "error"
    cancelled = "cancelled"
    partial = "partial"
    unknown_outcome = "unknown_outcome"
    inconclusive = "inconclusive"


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


class Operation(StrEnum):
    wifi_survey = "wifi_survey"
    ble_discover = "ble_discover"
    ble_inspect_gatt = "ble_inspect_gatt"
    ble_read_profile_value = "ble_read_profile_value"
    ble_observe_notifications = "ble_observe_notifications"
    ble_check_access_policy = "ble_check_access_policy"
    ble_write_lab_value = "ble_write_lab_value"
    ble_collect_diagnostics = "ble_collect_diagnostics"


class JobOutcome(StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"
    unknown_outcome = "unknown_outcome"
    unsupported = "unsupported"


class Classification(StrEnum):
    observation = "observation"
    candidate = "candidate"
    confirmed = "confirmed"
    passed = "passed"
    inconclusive = "inconclusive"
    unsupported = "unsupported"


# --- event envelope (schemas/event.schema.json) ---
class SecurityState(Strict):
    encrypted: bool | None = None
    authenticated_pairing: bool | None = None
    application_authorized: bool | None = None


class EventDetails(BaseModel):
    model_config = ConfigDict(extra="allow")  # operation-specific members vary
    security_state: SecurityState | None = None
    raw_att_status: int | None = None
    value_length: int | None = None


class Event(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: str = Field(pattern=r"^evt-[0-9a-zA-Z_-]{1,40}$")
    session_id: str = Field(pattern=r"^sess-[0-9a-zA-Z_-]{1,40}$")
    device_id: str = Field(pattern=r"^[0-9a-zA-Z_-]{1,40}$")
    boot_id: str = Field(pattern=r"^boot-[0-9a-zA-Z_-]{1,40}$")
    sequence: int = Field(ge=0)
    source_mode: SourceMode
    transport: Transport
    target_id: str | None = None
    operation: str = Field(min_length=1, max_length=64)
    timestamp_utc: str | None = None
    clock_quality: ClockQuality
    uptime_ms: int = Field(ge=0)
    status: EventStatus
    details: EventDetails


# --- job (schemas/job.schema.json) ---
class JobLimits(Strict):
    max_results: int | None = Field(default=None, ge=1, le=256)
    max_duration_s: float | None = Field(default=None, gt=0, le=600)
    max_bytes: int | None = Field(default=None, ge=1, le=65536)


class Job(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    job_id: str = Field(pattern=r"^job-[0-9a-zA-Z_-]{1,40}$")
    session_id: str = Field(pattern=r"^sess-[0-9a-zA-Z_-]{1,40}$")
    device_id: str | None = None
    target_id: str | None = None
    operation: Operation
    arguments: dict = Field(default_factory=dict)
    scope_version: int = Field(ge=1)
    deadline_s: float = Field(gt=0, le=600)
    limits: JobLimits = Field(default_factory=JobLimits)
    requires_local_confirmation: bool
    nonce: str | None = None


class Confirmation(Strict):
    job_id: str
    nonce: str
    approved: bool


class JobResult(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    job_id: str = Field(pattern=r"^job-[0-9a-zA-Z_-]{1,40}$")
    idempotency_key: str = Field(min_length=8, max_length=80)
    outcome: JobOutcome
    event_ids: list[str] = Field(default_factory=list, max_length=256)
    artifact_ids: list[str] = Field(default_factory=list, max_length=32)
    error: str | None = Field(default=None, max_length=240)
    confirmation: Confirmation | None = None


# --- finding (schemas/finding.schema.json) ---
class Finding(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    finding_id: str = Field(pattern=r"^finding-[0-9a-zA-Z_-]{1,40}$")
    session_id: str = Field(pattern=r"^sess-[0-9a-zA-Z_-]{1,40}$")
    target_id: str
    profile_id: str
    test_id: str = Field(pattern=r"^[a-z0-9_]{1,80}$")
    classification: Classification
    title: str = Field(max_length=160)
    expected_policy: str = Field(max_length=400)
    observed_behavior: str = Field(max_length=400)
    evidence_ids: list[str] = Field(min_length=1)
    severity: Literal["informational", "low", "medium", "high", "not_assessed"]
    confidence: Literal["low", "medium", "high"]
    source_mode: SourceMode
    limitations: list[str] = Field(default_factory=list)
    retest_of: str | None = None


# --- enrolled target (schemas/target-profile.schema.json) ---
class TargetIdentity(Strict):
    advertised_name: str | None = None
    service_uuid: str | None = None
    address: str | None = None
    address_type: Literal["public", "random", "unknown"] | None = None


class TargetProfile(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    target_id: str = Field(pattern=r"^target-[0-9a-zA-Z_-]{1,40}$")
    display_name: str = Field(max_length=64)
    profile_id: str = Field(max_length=64)
    scope_version: int = Field(ge=1)
    allowed_operations: list[str] = Field(default_factory=list)
    identity: TargetIdentity = Field(default_factory=TargetIdentity)
    notes: str | None = Field(default=None, max_length=400)


# --- normalized AI action (schemas/ai-action.schema.json) ---
AI_TOOLS = (
    *DEVICE_OPERATIONS,
    "compare_sessions",
    "explain_evidence",
    "generate_report",
)


class FinalAnswer(Strict):
    answer_markdown: str = Field(max_length=6000)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    limitations: list[str] = Field(default_factory=list, max_length=16)
    suggestions: list[str] = Field(default_factory=list, max_length=16)


class AiAction(Strict):
    kind: Literal["tool_call", "final_answer"]
    tool: str | None = None
    arguments: dict | None = None
    provider_call_id: str | None = Field(default=None, max_length=120)
    final: FinalAnswer | None = None


def validate_ai_action(a: AiAction) -> str | None:
    """Return a human-readable reason the action is invalid, or None when valid."""
    if a.kind == "tool_call":
        if a.tool not in AI_TOOLS:
            return f"unknown tool {a.tool!r}"
        if a.arguments is None:
            return "tool_call requires arguments"
    elif a.kind == "final_answer" and a.final is None:
        return "final_answer requires a final block"
    return None


# --- AI run status (schemas/run.schema.json; GET /v1/runs/{id}) ---
class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    awaiting_device = "awaiting_device"
    awaiting_local_confirmation = "awaiting_local_confirmation"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    expired = "expired"
    budget_exhausted = "budget_exhausted"
    ai_unavailable = "ai_unavailable"


class RunProvider(Strict):
    name: str
    model: str | None
    is_fake: bool


class RunBudget(Strict):
    actions_used: int
    actions_max: int
    provider_requests_used: int
    provider_requests_max: int
    elapsed_s: float
    max_duration_s: float


class RunStep(Strict):
    index: int
    kind: Literal["provider_request", "tool_call", "job", "backend_tool", "validation_error", "final_answer"]
    tool: str | None = None
    job_id: str | None = None
    outcome: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    summary: str = Field(max_length=240)


class RunAnswerFinding(Strict):
    finding_id: str
    test_id: str
    classification: Classification
    severity: Literal["informational", "low", "medium", "high", "not_assessed"]


class RunAnswer(Strict):
    answer_markdown: str | None = None
    cited_evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    source_modes: list[SourceMode] = Field(default_factory=list)
    findings: list[RunAnswerFinding] = Field(default_factory=list)  # evaluator output, authoritative
    findings_truncated: int | None = Field(default=None, ge=0)


class PendingAction(Strict):
    job_id: str | None = None
    operation: str | None = None
    target_id: str | None = None
    expected_effect: str | None = Field(default=None, max_length=200)
    requires_local_confirmation: bool | None = None


class RunUsage(Strict):
    input_tokens: int
    output_tokens: int
    cost: float | str  # a number when pricing is configured, else the string "unavailable"


class Run(Strict):
    run_id: str = Field(pattern=r"^run-[0-9a-zA-Z_-]{1,40}$")
    session_id: str
    status: RunStatus
    question: str = Field(max_length=1000)
    provider: RunProvider
    budget: RunBudget
    steps: list[RunStep] = Field(max_length=32)
    answer: RunAnswer | None
    pending_action: PendingAction | None
    usage: RunUsage
    error: str | None = Field(max_length=500)
