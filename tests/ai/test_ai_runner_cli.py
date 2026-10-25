"""RunWorker, build_provider and the ai-check / ask CLI commands (no network)."""

from __future__ import annotations

import argparse
import json
import time

import httpx
import pytest
from fake_store import TARGET_ID, AutoDevice, FakeStore, make_settings, setup_lab
from oyster.ai import fake as F
from oyster.ai.cli import register, run_ai_check, run_ask, write_provider_check
from oyster.ai.fake import FakeProvider
from oyster.ai.openai_compat import OpenAICompatProvider
from oyster.ai.runner import RunWorker, build_provider

KEY = "gsk" "_cli_test_secret_0123456789abcdef"


# ------------------------------------------------------------------ build_provider
def test_build_provider_selection(tmp_path):
    assert build_provider(make_settings(tmp_path, ai_provider="fake")).is_fake is True
    assert build_provider(make_settings(tmp_path, ai_provider="groq", ai_model="", ai_api_key=KEY)) is None
    assert build_provider(make_settings(tmp_path, ai_provider="groq", ai_model="m", ai_api_key="")) is None
    assert (
        build_provider(make_settings(tmp_path, ai_provider="no-such-provider", ai_model="m", ai_api_key=KEY))
        is None
    )
    assert (
        build_provider(
            make_settings(
                tmp_path,
                ai_provider="groq",
                ai_model="m",
                ai_api_key=KEY,
                ai_base_url="http://api.groq.com/openai/v1",
            )
        )
        is None
    )
    p = build_provider(
        make_settings(tmp_path, ai_provider="groq", ai_model="openai/gpt-oss-120b", ai_api_key=KEY)
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p.capabilities() == {"name": "groq", "model": "openai/gpt-oss-120b", "is_fake": False}
    p.close()


# ------------------------------------------------------------------ RunWorker
def _wait_terminal(store, run_id, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        r = store.get_run(run_id)
        if r.status in ("succeeded", "failed", "cancelled", "expired", "budget_exhausted", "ai_unavailable"):
            return r
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_run_worker_executes_runs_in_background(tmp_path):
    store = FakeStore(now=time.time)
    sess = setup_lab(store)
    store.responder = AutoDevice()
    provider = FakeProvider(
        [
            F.tool_call(
                "ble_read_profile_value", {"target_id": TARGET_ID, "characteristic_id": "public_sensor"}
            ),
            lambda m, t: F.final("Done.", F.evidence_ids_in(m, last_only=True)),
        ]
    )
    worker = RunWorker(store, make_settings(tmp_path), provider, poll_interval_s=0.01)
    worker.start()
    worker.start()  # idempotent
    try:
        run = store.create_run(sess.session_id, "Read the public sensor", provider.capabilities())
        worker.submit(run.run_id)
        worker.submit(run.run_id)  # duplicate submission ignored
        final = _wait_terminal(store, run.run_id)
    finally:
        worker.stop()
    assert final.status == "succeeded", final.error
    assert len(store.create_job_calls) == 1
    assert final.provider["is_fake"] is True


def test_run_worker_stop_ends_active_run_visibly(tmp_path):
    store = FakeStore(now=time.time)
    sess = setup_lab(store)
    store.responder = AutoDevice(mode="never")
    provider = FakeProvider([F.tool_call("ble_inspect_gatt", {"target_id": TARGET_ID})])
    worker = RunWorker(store, make_settings(tmp_path), provider, poll_interval_s=0.01)
    worker.start()
    run = store.create_run(sess.session_id, "Inspect", provider.capabilities())
    worker.submit(run.run_id)
    end = time.monotonic() + 5
    while store.get_run(run.run_id).status != "awaiting_device" and time.monotonic() < end:
        time.sleep(0.01)
    worker.stop()
    final = store.get_run(run.run_id)
    assert final.status == "failed" and "backend stopped" in final.error


# ------------------------------------------------------------------ ai-check
def _groq_mock(models, *, tool_ok=True, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": m, "active": True} for m in models]}
            )
        body = json.loads(request.content)
        assert body["tool_choice"] == "auto" and all("name" not in m for m in body["messages"])
        if body["messages"][-1]["role"] == "tool":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 3},
                },
            )
        call = {
            "id": "call_p",
            "type": "function",
            "function": {
                "name": "record_probe",
                "arguments": json.dumps({"value": "oyster-check" if tool_ok else "nope"}),
            },
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9},
            },
        )

    return httpx.MockTransport(handler)


def test_ai_check_auto_picks_model_and_writes_record_without_key(tmp_path):
    settings = make_settings(tmp_path, ai_provider="groq", ai_api_key=KEY, ai_model="")
    seen = []
    result = run_ai_check(
        settings,
        transport=_groq_mock(["whisper-large-v3", "llama-3.1-8b-instant", "openai/gpt-oss-120b"], seen=seen),
    )
    assert result["ok"] is True
    assert result["model"] == "openai/gpt-oss-120b" and result["model_source"] == "auto-pick"
    assert result["tested"][0]["tool_call_ok"] and result["tested"][0]["tool_result_ok"]
    assert "Set AI_MODEL=openai/gpt-oss-120b" in result["note"]
    tool_msg = json.loads(seen[-1].content)["messages"][-1]
    assert set(tool_msg) == {"role", "tool_call_id", "content"}
    path = write_provider_check(settings, result)
    text = path.read_text(encoding="utf-8")
    assert path.name == "provider_check.json" and KEY not in text
    assert json.loads(text)["model"] == "openai/gpt-oss-120b"


def test_ai_check_reports_unlisted_or_failing_model(tmp_path):
    s = make_settings(tmp_path, ai_provider="groq", ai_api_key=KEY, ai_model="retired-model")
    r = run_ai_check(s, transport=_groq_mock(["openai/gpt-oss-20b"]))
    assert r["ok"] is False and "not listed" in r["error"]
    s2 = make_settings(tmp_path, ai_provider="groq", ai_api_key=KEY, ai_model="openai/gpt-oss-20b")
    r2 = run_ai_check(s2, transport=_groq_mock(["openai/gpt-oss-20b"], tool_ok=False))
    assert r2["ok"] is False and r2["tested"][0]["tool_call_ok"] is False
    r3 = run_ai_check(make_settings(tmp_path, ai_provider="groq", ai_api_key=""))
    assert r3["ok"] is False and "AI_API_KEY" in r3["error"]

    def down(request):
        raise httpx.ConnectError("no route")

    r4 = run_ai_check(s2, transport=httpx.MockTransport(down))
    assert r4["ok"] is False and "transport error" in r4["error"]


# ------------------------------------------------------------------ ask
def test_ask_prints_progress_confirmation_prompt_and_answer():
    statuses = iter(
        [
            {
                "status": "running",
                "steps": [
                    {
                        "index": 0,
                        "kind": "provider_request",
                        "tool": None,
                        "outcome": "tool_call",
                        "summary": "Model request 1/6",
                    }
                ],
            },
            {
                "status": "awaiting_local_confirmation",
                "steps": [
                    {
                        "index": 1,
                        "kind": "job",
                        "tool": "ble_write_lab_value",
                        "outcome": "queued",
                        "summary": "Requested Write virtual_led=green",
                    }
                ],
                "pending_action": {
                    "job_id": "job-000001",
                    "expected_effect": "Write virtual_led=green on target-lab-01",
                },
            },
            {
                "status": "succeeded",
                "steps": [],
                "answer": {
                    "answer_markdown": "The LED write was confirmed (evt-000001).",
                    "cited_evidence_ids": ["evt-000001"],
                    "limitations": ["Simulated"],
                    "suggestions": [],
                    "source_modes": ["simulated"],
                },
                "usage": {"input_tokens": 10, "output_tokens": 5, "cost": "unavailable"},
                "budget": {
                    "actions_used": 1,
                    "actions_max": 12,
                    "provider_requests_used": 2,
                    "provider_requests_max": 6,
                },
                "provider": {"name": "fake", "model": None, "is_fake": True},
            },
        ]
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer owner-token"
        if request.method == "POST":
            assert request.url.path == "/v1/sessions/sess-test-01/assistant/messages"
            assert json.loads(request.content) == {"question": "Is the LED write protected?"}
            return httpx.Response(202, json={"run_id": "run-000001", "status": "queued"})
        assert request.url.path == "/v1/runs/run-000001"
        return httpx.Response(
            200, json=dict({"run_id": "run-000001", "pending_action": None}, **next(statuses))
        )

    out: list[str] = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    code = run_ask(
        "http://127.0.0.1:8000",
        "owner-token",
        "sess-test-01",
        "Is the LED write protected?",
        client=client,
        out=out.append,
        sleep=lambda s: None,
    )
    text = "\n".join(out)
    assert code == 0
    assert "run run-000001: queued" in text
    assert "confirm on the Cardputer: Write virtual_led=green on target-lab-01" in text
    assert "The LED write was confirmed (evt-000001)." in text
    assert "FAKE PROVIDER" in text and "cost unavailable" in text
    assert "owner-token" not in text


def test_ask_reports_backend_errors():
    def handler(request):
        return httpx.Response(404, json={"error": "not_found", "detail": "unknown session"})

    out: list[str] = []
    client = httpx.Client(base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler))
    assert run_ask("http://127.0.0.1:8000", "t", "sess-nope-01", "q", client=client, out=out.append) == 2
    assert "not_found" in out[0]


def test_register_adds_subcommands():
    parser = argparse.ArgumentParser(prog="oyster")
    sub = parser.add_subparsers(dest="command")
    register(sub)
    a = parser.parse_args(["ai-check"])
    assert a.command == "ai-check" and callable(a.func)
    b = parser.parse_args(["ask", "--session", "sess-test-01", "What did the survey find?"])
    assert b.session == "sess-test-01" and b.question == "What did the survey find?" and callable(b.func)
    with pytest.raises(SystemExit):
        parser.parse_args(["ask", "no session flag"])
