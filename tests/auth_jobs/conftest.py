"""Fixtures for API/auth/job-lifecycle tests: an in-process app over a temporary SQLite store with a
controllable clock (no network, no background threads unless a test opens the lifespan)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oyster.db.sqlite_store import SqliteStore
from oyster.main import create_app
from oyster.settings import REPO_DIR, Settings

ADMIN = "test-admin-token-0123456789abcdef"
LAB_TARGET = json.loads(
    (REPO_DIR / "schemas" / "examples" / "target.lab-01.json").read_text(encoding="utf-8")
)
ALL_OPS = [
    "wifi_survey",
    "ble_discover",
    "ble_inspect_gatt",
    "ble_read_profile_value",
    "ble_observe_notifications",
    "ble_check_access_policy",
    "ble_write_lab_value",
    "ble_collect_diagnostics",
]


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_settings(data_dir: Path, **kw) -> Settings:
    base = dict(
        data_dir=data_dir,
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token=ADMIN,
        tls_cert=data_dir / "dev-ca" / "server.crt",
        tls_key=data_dir / "dev-ca" / "server.key",
        ai_provider="groq",
        ai_base_url="https://ai.invalid/v1",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
    )
    base.update(kw)
    return Settings(**base)


def event(
    eid: str,
    session_id: str,
    device_id: str,
    seq: int,
    *,
    operation: str = "ble_read_profile_value",
    status: str = "success",
    source_mode: str = "simulated",
    target_id: str | None = "target-lab-01",
    details: dict | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": eid,
        "session_id": session_id,
        "device_id": device_id,
        "boot_id": "boot-test-01",
        "sequence": seq,
        "source_mode": source_mode,
        "transport": "ble",
        "target_id": target_id,
        "operation": operation,
        "timestamp_utc": None,
        "clock_quality": "unknown",
        "uptime_ms": 1000 + seq,
        "status": status,
        "details": details
        if details is not None
        else {"profile_id": "oyster-lab-v1", "characteristic_id": "public_sensor", "value_length": 2},
    }


class Lab:
    """Small driver around the TestClient for readable tests."""

    def __init__(self, client: TestClient, store: SqliteStore, clock: Clock):
        self.c = client
        self.store = store
        self.clock = clock
        self.owner = {"Authorization": f"Bearer {ADMIN}"}

    def enroll(self, device_id: str, simulated: bool = True) -> dict:
        r = self.c.post(
            "/v1/admin/devices",
            headers=self.owner,
            json={"device_id": device_id, "display_name": device_id, "simulated": simulated},
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def enroll_target(self, **overrides) -> dict:
        t = {**LAB_TARGET, **overrides}
        r = self.c.post("/v1/admin/targets", headers=self.owner, json=t)
        assert r.status_code == 200, r.text
        return t

    def session(
        self,
        headers: dict,
        session_id: str | None = None,
        mode: str = "simulated",
        caps: list[str] | None = None,
    ) -> str:
        body = {"source_mode": mode, "capabilities": ALL_OPS if caps is None else caps}
        if session_id:
            body["session_id"] = session_id
        r = self.c.post("/v1/sessions", headers=headers, json=body)
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def upload(self, headers: dict, session_id: str, events: list[dict]):
        return self.c.post(f"/v1/sessions/{session_id}/events", headers=headers, json={"events": events})

    def job(
        self,
        session_id: str,
        operation: str = "ble_read_profile_value",
        arguments: dict | None = None,
        target_id: str | None = "target-lab-01",
        scope_version: int = 1,
        deadline_s: float = 60,
        limits: dict | None = None,
        confirm: bool = False,
        run_id: str | None = None,
    ):
        return self.store.create_job(
            run_id=run_id,
            session_id=session_id,
            operation=operation,
            arguments={"characteristic_id": "public_sensor"} if arguments is None else arguments,
            target_id=target_id,
            scope_version=scope_version,
            deadline_s=deadline_s,
            limits=limits or {},
            requires_local_confirmation=confirm,
        )

    def poll(self, headers: dict) -> list[dict]:
        r = self.c.get("/v1/device/jobs", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["jobs"]

    def status(self, headers: dict, job_id: str, status: str):
        return self.c.post(f"/v1/jobs/{job_id}/status", headers=headers, json={"status": status})

    def result(
        self,
        headers: dict,
        job_id: str,
        event_ids: list[str],
        *,
        outcome: str = "succeeded",
        key: str | None = None,
        confirmation: dict | None = None,
        error: str | None = None,
    ):
        body = {
            "schema_version": "1.0",
            "job_id": job_id,
            "idempotency_key": key or f"res-{job_id}",
            "outcome": outcome,
            "event_ids": event_ids,
            "artifact_ids": [],
            "error": error,
            "confirmation": confirmation,
        }
        return self.c.post(f"/v1/jobs/{job_id}/result", headers=headers, json=body)


# Test modules use fixtures only (several rootless conftest.py files share the module name "conftest").
@pytest.fixture
def ev():
    return event


@pytest.fixture
def settings_factory():
    return make_settings


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def store(settings: Settings, clock: Clock):
    s = SqliteStore(settings.db_path, clock=clock)
    yield s
    s.close()


@pytest.fixture
def app(settings: Settings, store: SqliteStore):
    return create_app(settings, store=store, worker=None)


@pytest.fixture
def lab(app, store: SqliteStore, clock: Clock) -> Lab:
    return Lab(TestClient(app), store, clock)
