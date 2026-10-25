"""Operation catalogue: the backend's single source of truth for device-operation metadata.

ADR-005: requires_local_confirmation is derived from the operation type (writes and diagnostic transfer)
and, for ble_check_access_policy, from the selected assertion's `operation` in the enrolled lab profile
(write/transfer assertions are gated). No caller flag can lower it. Argument schemas here are what the
backend accepts in a job;
tool_parameters() adds target_id for the AI tool definitions. Bounds mirror firmware/oyster_core/config.h.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from oyster.contracts import DEVICE_OPERATIONS
from oyster.settings import REPO_DIR

MAX_ARGS_BYTES = 512  # cfg::kMaxJobArgsBytes
MAX_DEADLINE_S = 600.0  # job.schema.json deadline_s maximum
LIMIT_FIELDS = ("max_results", "max_duration_s", "max_bytes")
PROFILE_FILES = (REPO_DIR / "schemas" / "lab-profile.json",)
# public-read characteristics that must not be read through ble_read_profile_value: every read of app_auth
# issues (and so consumes) the fixture's single outstanding challenge (firmware lab_identity.cpp, ai/tools.py)
CHALLENGE_CHARACTERISTICS = frozenset({"app_auth"})

_CHAR_ID = {"type": "string", "pattern": "^[a-z0-9_]{1,23}$"}


@dataclass(frozen=True)
class OperationSpec:
    name: str
    requires_target: bool
    requires_local_confirmation: bool
    changes_target_state: bool  # never retried automatically; unknown_outcome on lost results
    default_deadline_s: float
    default_limits: dict
    limit_bounds: dict  # upper bound per JobLimits field
    arguments_schema: dict
    description: str
    expected_effect: str = ""
    extra: dict = field(default_factory=dict)


def _obj(props: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(required)}


OPERATIONS: dict[str, OperationSpec] = {
    "wifi_survey": OperationSpec(
        name="wifi_survey",
        requires_target=False,
        requires_local_confirmation=False,
        changes_target_state=False,
        default_deadline_s=60,
        default_limits={"max_results": 32, "max_duration_s": 30},
        limit_bounds={"max_results": 256, "max_duration_s": 30, "max_bytes": 4096},
        arguments_schema=_obj(
            {
                "passive": {"type": "boolean"},
                "show_hidden": {"type": "boolean"},
                "ms_per_channel": {"type": "integer", "minimum": 50, "maximum": 2000},
                "channel": {"type": "integer", "minimum": 0, "maximum": 14},
                "room_label": {"type": "string", "maxLength": 32},
            }
        ),
        description="Bounded 2.4 GHz access-point survey with recorded scan settings.",
        expected_effect="Passive or active scan; no connection to any network.",
    ),
    "ble_discover": OperationSpec(
        name="ble_discover",
        requires_target=False,
        requires_local_confirmation=False,
        changes_target_state=False,
        default_deadline_s=60,
        default_limits={"max_results": 64, "max_duration_s": 10},
        limit_bounds={"max_results": 256, "max_duration_s": 30, "max_bytes": 4096},
        arguments_schema=_obj(
            {
                "duration_ms": {"type": "integer", "minimum": 1000, "maximum": 30000},
                "active": {"type": "boolean"},
                "interval_ms": {"type": "integer", "minimum": 10, "maximum": 10000},
                "window_ms": {"type": "integer", "minimum": 10, "maximum": 10000},
            }
        ),
        description="Bounded BLE advertising discovery. Never connects.",
        expected_effect="Listens for advertisements only.",
    ),
    "ble_inspect_gatt": OperationSpec(
        name="ble_inspect_gatt",
        requires_target=True,
        requires_local_confirmation=False,
        changes_target_state=False,
        default_deadline_s=90,
        default_limits={"max_results": 64, "max_duration_s": 30},
        limit_bounds={"max_results": 256, "max_duration_s": 60, "max_bytes": 4096},
        arguments_schema=_obj({"page_index": {"type": "integer", "minimum": 0, "maximum": 63}}),
        description="GATT hierarchy of one enrolled target; completeness recorded.",
        expected_effect="Connects to the enrolled target and lists services/characteristics.",
    ),
    "ble_read_profile_value": OperationSpec(
        name="ble_read_profile_value",
        requires_target=True,
        requires_local_confirmation=False,
        changes_target_state=False,
        default_deadline_s=60,
        default_limits={"max_bytes": 512, "max_duration_s": 20},
        limit_bounds={"max_results": 1, "max_duration_s": 30, "max_bytes": 512},
        arguments_schema=_obj({"characteristic_id": _CHAR_ID}, ("characteristic_id",)),
        description="Read one profile-approved characteristic of an enrolled target.",
        expected_effect="Reads one value; does not change the target.",
    ),
    "ble_observe_notifications": OperationSpec(
        name="ble_observe_notifications",
        requires_target=True,
        requires_local_confirmation=False,
        changes_target_state=True,
        default_deadline_s=90,
        default_limits={"max_results": 32, "max_duration_s": 30},
        limit_bounds={"max_results": 256, "max_duration_s": 60, "max_bytes": 4096},
        arguments_schema=_obj(
            {
                "characteristic_id": _CHAR_ID,
                "duration_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
                "max_events": {"type": "integer", "minimum": 1, "maximum": 256},
            },
            ("characteristic_id",),
        ),
        description="Bounded notification subscription (writes the CCCD).",
        expected_effect="Subscribes to notifications (CCCD write) for a bounded time.",
    ),
    "ble_check_access_policy": OperationSpec(
        name="ble_check_access_policy",
        requires_target=True,
        requires_local_confirmation=False,
        changes_target_state=True,
        default_deadline_s=90,
        default_limits={"max_duration_s": 60},
        # transfer assertions move the synthetic diagnostic object (up to the profile's 64 KiB)
        limit_bounds={"max_results": 16, "max_duration_s": 180, "max_bytes": 65536},
        arguments_schema=_obj({"test_id": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"}}, ("test_id",)),
        description="Run one named profile assertion; classified by the deterministic evaluator.",
        expected_effect="Attempts the documented access under the assertion's precondition.",
    ),
    "ble_write_lab_value": OperationSpec(
        name="ble_write_lab_value",
        requires_target=True,
        requires_local_confirmation=True,
        changes_target_state=True,
        default_deadline_s=150,
        default_limits={"max_bytes": 1, "max_duration_s": 30},
        limit_bounds={"max_results": 1, "max_duration_s": 30, "max_bytes": 16},
        arguments_schema=_obj(
            {
                "characteristic_id": _CHAR_ID,
                "value": {"type": "string", "pattern": "^[a-z0-9_]{1,16}$"},
                "restore": {"type": "boolean"},
            },
            ("characteristic_id", "value"),
        ),
        description="Reversible typed write to a profile write target; needs local confirmation.",
        expected_effect="Writes a documented reversible lab value on the enrolled target.",
    ),
    "ble_collect_diagnostics": OperationSpec(
        name="ble_collect_diagnostics",
        requires_target=True,
        requires_local_confirmation=True,
        changes_target_state=True,
        default_deadline_s=300,
        default_limits={"max_bytes": 65536, "max_duration_s": 120},
        limit_bounds={"max_results": 1, "max_duration_s": 300, "max_bytes": 65536},
        arguments_schema=_obj(
            {
                "object_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,16}$"},
                "resume": {"type": "boolean"},
            }
        ),
        description="Bounded, hashed transfer of the known synthetic diagnostic object.",
        expected_effect="Transfers the synthetic diagnostic object (chunk requests are writes).",
    ),
}

if tuple(OPERATIONS) != DEVICE_OPERATIONS:  # the catalogue must cover exactly the contract operations
    raise RuntimeError("operation catalogue out of sync with contracts.DEVICE_OPERATIONS")


class CatalogueError(ValueError):
    """Rejected operation request. code is an API error code (unsupported_operation, validation_failed,
    payload_too_large, limits_exceeded)."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def get(operation: str) -> OperationSpec:
    spec = OPERATIONS.get(operation)
    if spec is None:
        raise CatalogueError("unsupported_operation", f"unsupported operation {operation!r}")
    return spec


def is_supported(operation: str) -> bool:
    return operation in OPERATIONS


# Assertion operations (lab-profile.json `assertions[].operation`) whose access check writes to the target
# or moves the diagnostic object: running them needs local confirmation like the equivalent operation.
CONFIRMED_ASSERTION_OPERATIONS = frozenset({"write", "transfer"})


def requires_local_confirmation(operation: str) -> bool:
    """Unconditional requirement of the operation type (ADR-005)."""
    return get(operation).requires_local_confirmation


def assertion(profile: dict | None, test_id: str | None) -> dict | None:
    if not profile or not isinstance(test_id, str):
        return None
    return next((a for a in profile.get("assertions", []) if a.get("test_id") == test_id), None)


def job_requires_local_confirmation(operation: str, arguments: dict | None, profile: dict | None) -> bool:
    """Confirmation for one concrete job (ADR-005): the operation type, or for ble_check_access_policy
    the selected assertion's `operation` in the enrolled profile (write/transfer). An unknown profile or
    test_id is treated as requiring confirmation. Never taken from a caller flag or model output."""
    if requires_local_confirmation(operation):
        return True
    if operation == "ble_check_access_policy":
        a = assertion(profile, (arguments or {}).get("test_id"))
        return a is None or a.get("operation") in CONFIRMED_ASSERTION_OPERATIONS
    return False


def changes_target_state(operation: str) -> bool:
    return get(operation).changes_target_state


def requires_target(operation: str) -> bool:
    return get(operation).requires_target


def tool_parameters(operation: str) -> dict:
    """JSON-schema parameters for the AI tool: job arguments plus target_id for connected operations."""
    spec = get(operation)
    schema = json.loads(json.dumps(spec.arguments_schema))
    if spec.requires_target:
        schema["properties"] = {
            "target_id": {"type": "string", "pattern": "^target-[0-9a-zA-Z_-]{1,40}$"},
            **schema["properties"],
        }
        schema["required"] = ["target_id", *schema["required"]]
    return schema


# --- profiles (schemas/lab-profile.json) ---
@lru_cache(maxsize=1)
def _profiles() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in PROFILE_FILES:
        p = Path(path)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            out[data["profile_id"]] = data
    return out


def load_profile(profile_id: str) -> dict | None:
    return _profiles().get(profile_id)


def known_profile_ids() -> list[str]:
    return sorted(_profiles())


def _profile_checks(op: str, args: dict, profile: dict) -> None:
    chars = profile.get("characteristics", {})
    if op in ("ble_read_profile_value", "ble_observe_notifications"):
        cid = args["characteristic_id"]
        c = chars.get(cid)
        prop = "read" if op == "ble_read_profile_value" else "notify"
        if c is None:
            raise CatalogueError(
                "validation_failed", f"characteristic {cid!r} is not in profile {profile.get('profile_id')}"
            )
        if prop not in c.get("properties", []) or c.get("policy", {}).get(prop, "none") == "none":
            raise CatalogueError("validation_failed", f"characteristic {cid!r} does not support {prop}")
        if op == "ble_read_profile_value":
            if c.get("policy", {}).get("read") != "public":
                raise CatalogueError(
                    "validation_failed",
                    f"characteristic {cid!r} is not publicly readable; protected reads are access-policy "
                    "assertions (ble_check_access_policy)",
                )
            if cid in CHALLENGE_CHARACTERISTICS:
                raise CatalogueError(
                    "validation_failed",
                    f"characteristic {cid!r} cannot be read directly: a read consumes the fixture challenge",
                )
    elif op == "ble_check_access_policy":
        tests = {a["test_id"] for a in profile.get("assertions", [])}
        if args["test_id"] not in tests:
            raise CatalogueError(
                "validation_failed", f"test_id {args['test_id']!r} is not a profile assertion"
            )
    elif op == "ble_write_lab_value":
        wv = profile.get("write_values", {}).get(args["characteristic_id"])
        if wv is None:
            raise CatalogueError(
                "validation_failed",
                f"characteristic {args['characteristic_id']!r} is not a documented write target",
            )
        allowed = wv.get("allowed", {})
        if args["value"] not in allowed:
            raise CatalogueError(
                "validation_failed", f"value {args['value']!r} not allowed; expected one of {sorted(allowed)}"
            )
    elif op == "ble_collect_diagnostics":
        expected = profile.get("transfer", {}).get("object_id")
        if "object_id" in args and args["object_id"] != expected:
            raise CatalogueError("validation_failed", f"object_id must be the profile object {expected!r}")


def validate_arguments(operation: str, arguments: dict, profile: dict | None = None) -> dict:
    """Typed, bounded argument check. Returns a normalized copy; raises CatalogueError."""
    spec = get(operation)
    if not isinstance(arguments, dict):
        raise CatalogueError("validation_failed", "arguments must be an object")
    size = len(json.dumps(arguments, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_ARGS_BYTES:
        raise CatalogueError("payload_too_large", f"arguments are {size} bytes; limit {MAX_ARGS_BYTES}")
    errors = sorted(Draft202012Validator(spec.arguments_schema).iter_errors(arguments), key=lambda e: e.path)
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "arguments"
        raise CatalogueError("validation_failed", f"{where}: {e.message}"[:240])
    if spec.requires_target:
        if profile is None:
            raise CatalogueError(
                "validation_failed", "target profile is unknown; arguments cannot be checked"
            )
        _profile_checks(operation, arguments, profile)
    return dict(arguments)


def resolve_limits(operation: str, limits: dict | None) -> dict:
    """Defaults merged with caller limits, each checked against the per-operation upper bound."""
    spec = get(operation)
    limits = dict(limits or {})
    unknown = set(limits) - set(LIMIT_FIELDS)
    if unknown:
        raise CatalogueError("validation_failed", f"unknown limit fields {sorted(unknown)}")
    merged = {**spec.default_limits, **{k: v for k, v in limits.items() if v is not None}}
    for k, v in merged.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise CatalogueError("validation_failed", f"limit {k} must be a positive number")
        if k in ("max_results", "max_bytes") and int(v) != v:
            raise CatalogueError("validation_failed", f"limit {k} must be an integer")
        if v > spec.limit_bounds[k]:
            raise CatalogueError(
                "limits_exceeded", f"limit {k}={v} exceeds {spec.limit_bounds[k]} for {operation}"
            )
    return merged


def resolve_deadline(operation: str, deadline_s: float | None) -> float:
    spec = get(operation)
    d = spec.default_deadline_s if deadline_s is None else deadline_s
    if isinstance(d, bool) or not isinstance(d, (int, float)) or not (0 < d <= MAX_DEADLINE_S):
        raise CatalogueError("validation_failed", f"deadline_s must be in (0, {MAX_DEADLINE_S:g}]")
    return float(d)
