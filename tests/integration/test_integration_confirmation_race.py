"""End-to-end confirmation-timeout race, in process: the real FastAPI app over SQLite, the real AI
orchestrator (FakeProvider) and the real device simulator talking HTTP through the TestClient.

The owner approves a write on the (virtual) device only after the AI run stopped waiting for the approval:
the backend has expired the job, refuses the `running` report, and the device executes nothing."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oyster.ai import fake as F
from oyster.ai.fake import FakeProvider
from oyster.ai.orchestrator import Orchestrator
from oyster.db.sqlite_store import SqliteStore
from oyster.main import create_app
from oyster.settings import REPO_DIR, RunBudgets, Settings
from oyster_lab.simulator.device import Simulator
from oyster_lab.simulator.scenarios import load_scenario

ADMIN = "integration-admin-token-0123456789abcdef"
DEVICE_ID = "sim-cardputer-01"
WRITE_GREEN = {"target_id": "target-lab-01", "characteristic_id": "virtual_led", "value": "green"}


def _settings(tmp_path: Path, confirm_wait_s: int) -> Settings:
    return Settings(
        data_dir=tmp_path,
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token=ADMIN,
        tls_cert=tmp_path / "server.crt",
        tls_key=tmp_path / "server.key",
        ai_provider="fake",
        ai_base_url="",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
        budgets=RunBudgets(max_confirmation_wait_s=confirm_wait_s),
    )


@pytest.fixture
def lab(tmp_path):
    def build(confirm_wait_s: int, script: list, owner):
        settings = _settings(tmp_path, confirm_wait_s)
        store = SqliteStore(settings.db_path)
        app = create_app(settings, store=store, worker=None)
        owner_client = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
        r = owner_client.post(
            "/v1/admin/devices", json={"device_id": DEVICE_ID, "display_name": "sim", "simulated": True}
        )
        token = r.json()["token"]
        target = json.loads((REPO_DIR / "schemas/examples/target.lab-01.json").read_text(encoding="utf-8"))
        assert owner_client.post("/v1/admin/targets", json=target).status_code == 200
        device_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        sim = Simulator(load_scenario("success"), device_client, device_id=DEVICE_ID, confirm=owner)
        session_id = sim.register_session()
        provider = FakeProvider(script)
        orch = Orchestrator(store, settings, provider, poll_interval_s=0.02)
        run = store.create_run(session_id, "Turn the lab LED green", provider.capabilities())
        built.append(store)
        return store, sim, orch, run.run_id, session_id

    built: list[SqliteStore] = []
    yield build
    for s in built:
        s.close()


def _run_in_thread(orch: Orchestrator, run_id: str):
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("run", orch.execute(run_id)), daemon=True)
    t.start()
    return t, out


def test_late_device_approval_after_expiry_cannot_execute(lab):
    holder: dict = {}

    def owner_approves_too_late(job: dict) -> bool:
        holder["thread"].join(timeout=20)  # the AI run gives up first
        assert not holder["thread"].is_alive()
        return True

    script = [F.tool_call("ble_write_lab_value", WRITE_GREEN), F.final("never reached", [])]
    store, sim, orch, run_id, session_id = lab(1, script, owner_approves_too_late)
    holder["thread"], out = _run_in_thread(orch, run_id)
    report = sim.run(serve=True, max_jobs=1, poll_interval_s=0.02, idle_exit_s=20)

    run = out["run"]
    assert run.status == "expired" and "fresh confirmation" in run.error
    jr = report.jobs[0]
    assert jr.operation == "ble_write_lab_value" and jr.confirmation_approved is True  # approved, too late
    assert jr.statuses == ["awaiting_local_confirmation", "running:rejected"]
    assert jr.executed is False and jr.outcome is None
    job = store.get_job(jr.job_id)
    assert job.status == "expired" and job.result is None
    audit = store.get_job_audit(jr.job_id)
    assert audit["status_reason"] == "confirmation_timeout" and audit["late_results"] == []
    assert store.get_events(session_id) == []  # nothing ran, no evidence of an execution


def test_timely_approval_executes_with_bound_confirmation(lab):
    def cite_last(messages, tools):
        return F.final("The write was attempted.", F.evidence_ids_in(messages, last_only=True))

    store, sim, orch, run_id, session_id = lab(
        60, [F.tool_call("ble_write_lab_value", WRITE_GREEN), cite_last], lambda job: True
    )
    thread, out = _run_in_thread(orch, run_id)
    report = sim.run(serve=True, max_jobs=1, poll_interval_s=0.02, idle_exit_s=20)
    thread.join(timeout=20)

    run = out["run"]
    assert run.status == "succeeded", run.error
    jr = report.jobs[0]
    assert jr.statuses == ["awaiting_local_confirmation", "running"] and jr.executed is True
    job = store.get_job(jr.job_id)
    assert job.status == "succeeded"
    assert job.result.confirmation.approved is True and job.result.confirmation.nonce == job.job.nonce
    cited = run.answer["cited_evidence_ids"]
    assert cited and store.missing_event_ids(session_id, cited) == []
    assert run.answer["source_modes"] == ["simulated"]
