"""AI run endpoints over HTTP: ai_unavailable without a worker, schema-valid bounded status."""

from __future__ import annotations

import json
import threading

from fastapi.testclient import TestClient
from oyster.evidence.validate import schema_errors
from oyster.main import create_app


def test_runs_end_ai_unavailable_without_worker(lab):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    r = lab.c.post(f"/v1/sessions/{sid}/assistant/messages", headers=a, json={"question": "Is my sensor ok?"})
    assert r.status_code == 200 and r.json()["status"] == "ai_unavailable"
    view = lab.c.get(f"/v1/runs/{r.json()['run_id']}", headers=a).json()
    assert not schema_errors("run.schema.json", view)
    assert view["status"] == "ai_unavailable" and view["answer"] is None and "not available" in view["error"]
    assert view["usage"]["cost"] == "unavailable"
    # the owner may ask too; questions are bounded
    assert (
        lab.c.post(
            f"/v1/sessions/{sid}/assistant/messages", headers=lab.owner, json={"question": "again"}
        ).status_code
        == 200
    )
    assert (
        lab.c.post(
            f"/v1/sessions/{sid}/assistant/messages", headers=a, json={"question": "x" * 1001}
        ).status_code
        == 422
    )
    assert (
        lab.c.post(f"/v1/sessions/{sid}/assistant/messages", headers=a, json={"question": ""}).status_code
        == 422
    )


class FakeWorker:
    """Stands in for oyster.ai.runner.RunWorker: records lifecycle calls only."""

    def __init__(self):
        self.submitted: list[str] = []
        self.started = self.stopped = 0
        self.provider = None

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def submit(self, run_id: str):
        self.submitted.append(run_id)


def test_worker_receives_runs_and_lifespan_starts_it_once(settings, store, settings_factory):
    worker = FakeWorker()
    app = create_app(settings, store=store, worker=worker)
    owner = {"Authorization": f"Bearer {settings.admin_token}"}
    with TestClient(app) as c:
        assert worker.started == 1
        tok = c.post("/v1/admin/devices", headers=owner, json={"device_id": "d1", "simulated": True}).json()[
            "token"
        ]
        dev = {"Authorization": f"Bearer {tok}"}
        sid = c.post("/v1/sessions", headers=dev, json={"source_mode": "simulated"}).json()["session_id"]
        r = c.post(f"/v1/sessions/{sid}/assistant/messages", headers=dev, json={"question": "hello"})
        assert r.json()["status"] == "queued" and worker.submitted == [r.json()["run_id"]]
        assert app.state.runtime.sweeper._thread is not None
    assert worker.stopped == 1 and app.state.runtime.sweeper._thread is None


def test_run_view_is_schema_valid_and_bounded_for_devices(lab, settings):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    run = lab.store.create_run(sid, "Explain everything", {"name": "fake", "model": None, "is_fake": True})
    run.status = "succeeded"
    run.steps = [
        {
            "index": i,
            "kind": "job",
            "tool": "ble_read_profile_value",
            "job_id": f"job-{i}",
            "outcome": "succeeded",
            "event_ids": [f"evt-{i:06d}"],
            "summary": "s" * 240,
        }
        for i in range(40)
    ]
    run.steps.append({"index": 99, "kind": "made_up_kind", "summary": "dropped"})
    run.answer = {
        "answer_markdown": "A" * 6000,
        "cited_evidence_ids": ["evt-000001"],
        "limitations": ["l"] * 16,
        "suggestions": ["s"] * 16,
        "source_modes": ["simulated"],
        "internal": "not in schema",
    }
    run.pending_action = None
    lab.store.save_run(run)
    owner_view = lab.c.get(f"/v1/runs/{run.run_id}", headers=lab.owner).json()
    assert not schema_errors("run.schema.json", owner_view)
    assert len(owner_view["steps"]) == 32 and "internal" not in owner_view["answer"]
    device_resp = lab.c.get(f"/v1/runs/{run.run_id}", headers=a)
    assert len(device_resp.content) <= 4096
    dv = device_resp.json()
    assert not schema_errors("run.schema.json", dv)
    assert dv["answer"]["answer_markdown"].endswith("[truncated; full text in the session report]")
    assert dv["status"] == "succeeded" and dv["answer"]["cited_evidence_ids"] == ["evt-000001"]


def test_cost_reported_only_when_pricing_configured(tmp_path, settings_factory, store):
    s = settings_factory(tmp_path, ai_price_input_per_mtok=0.5, ai_price_output_per_mtok=1.0)
    app = create_app(s, store=store)
    c = TestClient(app)
    owner = {"Authorization": f"Bearer {s.admin_token}"}
    store.create_device("d1", "d1", True)
    sess, _ = store.register_session("d1", None, "simulated", [], None)
    run = store.create_run(sess.session_id, "q", {"name": "groq", "model": "m", "is_fake": False})
    run.input_tokens, run.output_tokens = 2_000_000, 1_000_000
    store.save_run(run)
    assert c.get(f"/v1/runs/{run.run_id}", headers=owner).json()["usage"]["cost"] == 2.0


def test_store_is_thread_safe_for_concurrent_uploads(lab, ev):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    errors: list[str] = []

    def worker(base: int):
        for i in range(10):
            n = base + i
            r = lab.upload(a, sid, [ev(f"evt-{n:06d}", sid, "dev-a", n)])
            if r.status_code != 200:
                errors.append(json.dumps(r.json()))

    threads = [threading.Thread(target=worker, args=(k * 100,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and lab.store.count_events(sid) == 40
