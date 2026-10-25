"""Evidence event builders for the simulator.

Events are shaped like the firmware's (docs/BLE_LAB_PROFILE.md): a summary event plus observation
pages for surveys and discovery, read events that carry `value_hex` only for public-policy
characteristics (otherwise `value_length` + `value_sha256`), and access-check events exactly per
the access-check event contract. Every event is labelled `source_mode: "simulated"` and is validated against
`schemas/event.schema.json` before upload. Absent measurements are null/unknown, never invented.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import Any

# Observation pages mirror firmware cfg::kObservationsPerEventPage.
OBSERVATIONS_PER_PAGE = 8


def _repo_root() -> Path:
    # events.py -> simulator -> oyster_lab -> lab -> ROOT
    return Path(__file__).resolve().parents[3]


def schemas_dir() -> Path:
    import os

    override = os.getenv("OYSTER_SCHEMAS_DIR")
    if override:
        return Path(override)
    return _repo_root() / "schemas"


@functools.lru_cache(maxsize=1)
def load_event_schema() -> dict[str, Any]:
    return json.loads((schemas_dir() / "event.schema.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def load_profile() -> dict[str, Any]:
    return json.loads((schemas_dir() / "lab-profile.json").read_text(encoding="utf-8"))


def char_def(characteristic_id: str) -> dict[str, Any] | None:
    return load_profile().get("characteristics", {}).get(characteristic_id)


def read_policy(characteristic_id: str) -> str | None:
    c = char_def(characteristic_id)
    if not c:
        return None
    return c.get("policy", {}).get("read")


def is_public_read(characteristic_id: str) -> bool:
    return read_policy(characteristic_id) == "public"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_event(
    *,
    event_id: str,
    session_id: str,
    device_id: str,
    boot_id: str,
    sequence: int,
    operation: str,
    transport: str,
    status: str,
    details: dict[str, Any],
    target_id: str | None = None,
    uptime_ms: int,
    source_mode: str = "simulated",
    clock_quality: str = "unknown",
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Assemble one event envelope. source_mode is always 'simulated' for this package."""
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "session_id": session_id,
        "device_id": device_id,
        "boot_id": boot_id,
        "sequence": sequence,
        "source_mode": source_mode,
        "transport": transport,
        "target_id": target_id,
        "operation": operation,
        "timestamp_utc": timestamp_utc,
        "clock_quality": clock_quality,
        "uptime_ms": uptime_ms,
        "status": status,
        "details": details,
    }


def paginate(items: list[Any], per_page: int = OBSERVATIONS_PER_PAGE) -> list[list[Any]]:
    if not items:
        return []
    return [items[i : i + per_page] for i in range(0, len(items), per_page)]


# --- details builders (the envelope is added by the caller via make_event) ---


def wifi_summary_details(
    *,
    room_label: str | None,
    scan: dict[str, Any],
    observations: list[dict],
    complete: bool,
    overflow_count: int,
) -> dict[str, Any]:
    return {
        "room_label": room_label,
        "scan": scan,
        "complete": complete,
        "overflow_count": overflow_count,
        "observation_count": len(observations),
        "page_count": len(paginate(observations)),
    }


def wifi_page_details(*, page_index: int, observations: list[dict], more_pages: bool) -> dict[str, Any]:
    return {"page_index": page_index, "more_pages": more_pages, "observations": observations}


def discovery_summary_details(
    *,
    observations: list[dict],
    complete: bool,
    overflow_count: int,
    malformed_count: int,
    address_changed: bool = False,
    prior_address: str | None = None,
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "complete": complete,
        "overflow_count": overflow_count,
        "malformed_count": malformed_count,
        "observation_count": len(observations),
        "page_count": len(paginate(observations)),
        "address_changed": address_changed,
    }
    if scan is not None:
        d["scan"] = scan
    if address_changed:
        d["prior_address"] = prior_address
    return d


def discovery_page_details(*, page_index: int, observations: list[dict], more_pages: bool) -> dict[str, Any]:
    return {"page_index": page_index, "more_pages": more_pages, "observations": observations}


def gatt_summary_details(
    *, service_uuid: str, total_entries: int, complete: bool, pages: int
) -> dict[str, Any]:
    return {
        "service_uuid": service_uuid,
        "total_entries": total_entries,
        "complete": complete,
        "page_count": pages,
    }


def gatt_page_details(*, page_index: int, entries: list[dict], more_pages: bool) -> dict[str, Any]:
    return {"page_index": page_index, "more_pages": more_pages, "entries": entries}


def read_value_details(
    *,
    characteristic_id: str,
    value: bytes | None,
    security_state: dict[str, Any] | None,
    raw_att_status: int | None,
    app_status: str | None = None,
) -> dict[str, Any]:
    """Read event details. Public-policy characteristics carry value_hex; all others only length+sha256."""
    profile = load_profile()
    d: dict[str, Any] = {
        "profile_id": profile.get("profile_id"),
        "characteristic_id": characteristic_id,
        "security_state": security_state,
        "raw_att_status": raw_att_status,
    }
    if app_status is not None:
        d["app_status"] = app_status
    if value is None:
        d["value_length"] = None
    else:
        d["value_length"] = len(value)
        if is_public_read(characteristic_id):
            d["value_hex"] = value.hex()
        else:
            d["value_sha256"] = sha256_hex(value)
    return d


def access_check_details(
    *,
    test_id: str,
    characteristic_id: str,
    precondition: str,
    precondition_established: bool,
    access: str,
    app_status: str | None,
    raw_att_status: int | None,
    security_before: dict[str, Any],
    security_after: dict[str, Any],
    security_transition: bool,
    value_length: int | None,
    value_sha256: str | None,
    hash_verified: bool | None = None,
) -> dict[str, Any]:
    """Exactly the ble_check_access_policy details contract (docs/BLE_LAB_PROFILE.md)."""
    profile = load_profile()
    return {
        "profile_id": profile.get("profile_id"),
        "test_id": test_id,
        "characteristic_id": characteristic_id,
        "precondition": precondition,
        "precondition_established": precondition_established,
        "access": access,
        "app_status": app_status,
        "raw_att_status": raw_att_status,
        "security_state": security_after,
        "security_before": security_before,
        "security_after": security_after,
        "security_transition": security_transition,
        "value_length": value_length,
        "value_sha256": value_sha256,
        "hash_verified": hash_verified,
    }


def write_value_details(
    *,
    characteristic_id: str,
    requested_value: Any,
    starting_value: Any,
    observed_result: str,
    restored: bool | None,
    security_state: dict[str, Any] | None,
    app_status: str | None,
    raw_att_status: int | None = None,
) -> dict[str, Any]:
    profile = load_profile()
    return {
        "profile_id": profile.get("profile_id"),
        "characteristic_id": characteristic_id,
        "requested_value": requested_value,
        "starting_value": starting_value,
        "observed_result": observed_result,
        "restored": restored,
        "security_state": security_state,
        "app_status": app_status,
        "raw_att_status": raw_att_status,
    }


def notifications_details(
    *,
    characteristic_id: str,
    subscribed: bool,
    notifications_observed: int,
    duration_ms: int,
    value_lengths: list[int],
    security_state: dict[str, Any] | None,
) -> dict[str, Any]:
    profile = load_profile()
    return {
        "profile_id": profile.get("profile_id"),
        "characteristic_id": characteristic_id,
        "subscribed": subscribed,
        "notifications_observed": notifications_observed,
        "duration_ms": duration_ms,
        "value_lengths": value_lengths,
        "security_state": security_state,
    }


def diagnostics_details(
    *,
    object_id: str,
    declared_length: int,
    received_length: int | None,
    value_sha256: str | None,
    hash_verified: bool | None,
    app_status: str | None,
    security_state: dict[str, Any] | None,
    chunks_received: int,
    raw_att_status: int | None = None,
) -> dict[str, Any]:
    profile = load_profile()
    return {
        "profile_id": profile.get("profile_id"),
        "object_id": object_id,
        "declared_length": declared_length,
        "value_length": received_length,
        "value_sha256": value_sha256,
        "hash_verified": hash_verified,
        "app_status": app_status,
        "security_state": security_state,
        "chunks_received": chunks_received,
        "raw_att_status": raw_att_status,
    }
