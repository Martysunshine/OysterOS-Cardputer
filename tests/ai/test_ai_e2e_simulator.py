"""End to end, in process: real FastAPI app over SQLite, real AI orchestrator (FakeProvider), the real
deterministic evaluator and the real device simulator (vulnerable fixture). Checks that the simulator's
events are what the evaluator needs and that the stored answer carries the evaluator's findings."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from oyster.ai import fake as F
from oyster.ai.fake import FakeProvider
from oyster.ai.orchestrator import SIMULATED_LIMITATION, Orchestrator
from oyster.db.sqlite_store import SqliteStore
from oyster.main import create_app
from oyster.settings import REPO_DIR, RunBudgets, Settings
from oyster_lab.simulator.device import Simulator
from oyster_lab.simulator.scenarios import load_scenario

ADMIN = "e2e-admin-token-0123456789abcdef-0123"
DEVICE_ID = "sim-cardputer-01"
TARGET = "target-lab-01"


def _settings(tmp_path: Path) -> Settings:
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
        ai_max_context_events=40,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
        budgets=RunBudgets(max_confirmation_wait_s=30),
    )


def test_vulnerable_fixture_checks_are_classified_by_the_evaluator_and_kept_in_the_answer(tmp_path):
    settings = _settings(tmp_path)
    store = SqliteStore(settings.db_path)
    try:
        app = create_app(settings, store=store, worker=None)
        owner = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
        token = owner.post(
            "/v1/admin/devices", json={"device_id": DEVICE_ID, "display_name": "sim", "simulated": True}
        ).json()["token"]
        target = json.loads((REPO_DIR / "schemas/examples/target.lab-01.json").read_text(encoding="utf-8"))
        assert owner.post("/v1/admin/targets", json=target).status_code == 200
        device = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        sim = Simulator(
            load_scenario("vulnerable_target"), device, device_id=DEVICE_ID, confirm=lambda job: True
        )
        session_id = sim.register_session()

        script = [
            F.tool_call(
                "ble_check_access_policy",
                {"target_id": TARGET, "test_id": "restricted_diagnostics_requires_app_authorization"},
            ),
            F.tool_call(
                "ble_check_access_policy",
                {"target_id": TARGET, "test_id": "virtual_led_write_requires_app_authorization"},
            ),
            lambda m, t: F.final(
                "Both checks ran. The diagnostics check was confirmed as a violation.",
                [i for i in F.evidence_ids_in([x for x in m if '"kind":"job_result"' in x.content])],
            ),
        ]
        provider = FakeProvider(script)
        orch = Orchestrator(store, settings, provider, poll_interval_s=0.02)
        run_id = store.create_run(session_id, "Check access policy", provider.capabilities()).run_id
        out: dict = {}
        t = threading.Thread(target=lambda: out.setdefault("run", orch.execute(run_id)), daemon=True)
        t.start()
        report = sim.run(serve=True, max_jobs=2, poll_interval_s=0.02, idle_exit_s=30)
        t.join(timeout=30)
        assert not t.is_alive()

        assert [jr.outcome for jr in report.jobs] == ["succeeded", "succeeded"]
        run = out["run"]
        assert run.status == "succeeded", run.error
        by_test = {f.test_id: f for f in store.list_findings(session_id)}
        diag = by_test["restricted_diagnostics_requires_app_authorization"]
        led = by_test["virtual_led_write_requires_app_authorization"]
        assert diag.classification.value == "confirmed" and diag.severity == "medium"
        assert led.classification.value == "passed"  # denied with ATT 0x08 in every mode
        answer_findings = {f["test_id"]: f for f in run.answer["findings"]}
        assert answer_findings[diag.test_id]["classification"] == "confirmed"
        assert answer_findings[led.test_id]["classification"] == "passed"
        assert run.answer["source_modes"] == ["simulated"]
        assert SIMULATED_LIMITATION in run.answer["limitations"]
    finally:
        store.close()
