"""Fixtures for evaluator and report tests (fixtures only; see tests/auth_jobs/conftest.py for why)."""

from __future__ import annotations

import pytest
from oyster.contracts import Event
from oyster.db.sqlite_store import SqliteStore
from oyster.policy import catalogue

_AUTO = object()
_PROFILE = catalogue.load_profile("oyster-lab-v1")
# ATT code a correctly enforcing stack reports for a denial under each documented policy
_ATT_FOR_POLICY = {"link_encryption": 0x0F, "link_authentication": 0x05, "app_authorization": 0x08}


def _assertion(test_id: str) -> dict | None:
    return next((a for a in _PROFILE["assertions"] if a["test_id"] == test_id), None)


def _check_event(
    eid: str,
    test_id: str,
    *,
    session_id: str = "sess-r-01",
    access: str = "returned",
    established: bool | None = True,
    encrypted=False,
    authenticated=False,
    authorized=False,
    transition: bool = False,
    hash_verified=None,
    source_mode: str = "simulated",
    status: str | None = None,
    target_id: str = "target-lab-01",
    seq: int = 1,
    device_id: str = "sim-01",
    characteristic=_AUTO,
    precondition=_AUTO,
    raw_att=_AUTO,
) -> Event:
    """Check event carrying the real characteristic/precondition of the assertion (override to mismatch)."""
    a = _assertion(test_id) or {}
    if raw_att is _AUTO:
        policy = _PROFILE["characteristics"].get(a.get("characteristic"), {}).get("policy", {})
        op = "read" if a.get("operation") == "transfer" else a.get("operation")
        raw_att = _ATT_FOR_POLICY.get(policy.get(op)) if access == "denied_att" else None
    details = {
        "profile_id": "oyster-lab-v1",
        "test_id": test_id,
        "characteristic_id": a.get("characteristic", "x") if characteristic is _AUTO else characteristic,
        "precondition": a.get("precondition", "p") if precondition is _AUTO else precondition,
        "precondition_established": established,
        "access": access,
        "app_status": None,
        "raw_att_status": raw_att,
        "security_state": {
            "encrypted": encrypted,
            "authenticated_pairing": authenticated,
            "application_authorized": authorized,
        },
        "security_before": {
            "encrypted": False,
            "authenticated_pairing": False,
            "application_authorized": False,
        },
        "security_after": {
            "encrypted": encrypted,
            "authenticated_pairing": authenticated,
            "application_authorized": authorized,
        },
        "security_transition": transition,
        "value_length": 10 if access == "returned" else None,
        "value_sha256": None,
        "hash_verified": hash_verified,
    }
    if status is None:
        status = {
            "returned": "success",
            "denied_app": "denied",
            "denied_att": "denied",
            "timeout": "timeout",
            "error": "error",
            "not_attempted": "inconclusive",
        }[access]
    return Event.model_validate(
        {
            "schema_version": "1.0",
            "event_id": eid,
            "session_id": session_id,
            "device_id": device_id,
            "boot_id": "boot-r-01",
            "sequence": seq,
            "source_mode": source_mode,
            "transport": "ble",
            "target_id": target_id,
            "operation": "ble_check_access_policy",
            "timestamp_utc": None,
            "clock_quality": "unknown",
            "uptime_ms": 100,
            "status": status,
            "details": details,
        }
    )


def _read_event(
    eid: str,
    *,
    session_id: str = "sess-r-01",
    status: str = "success",
    source_mode="simulated",
    characteristic: str = "public_sensor",
    seq: int = 1,
) -> Event:
    return Event.model_validate(
        {
            "schema_version": "1.0",
            "event_id": eid,
            "session_id": session_id,
            "device_id": "sim-01",
            "boot_id": "boot-r-01",
            "sequence": seq,
            "source_mode": source_mode,
            "transport": "ble",
            "target_id": "target-lab-01",
            "operation": "ble_read_profile_value",
            "timestamp_utc": None,
            "clock_quality": "unknown",
            "uptime_ms": 100,
            "status": status,
            "details": {
                "profile_id": "oyster-lab-v1",
                "characteristic_id": characteristic,
                "security_state": {
                    "encrypted": False,
                    "authenticated_pairing": False,
                    "application_authorized": False,
                },
                "raw_att_status": None,
                "value_length": 2 if status == "success" else None,
            },
        }
    )


@pytest.fixture
def profile() -> dict:
    return catalogue.load_profile("oyster-lab-v1")


@pytest.fixture
def check_event():
    return _check_event


@pytest.fixture
def read_event():
    return _read_event


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "oyster.db")
    s.create_device("sim-01", "sim", True)
    s.create_device("sim-02", "sim", True)
    yield s
    s.close()
