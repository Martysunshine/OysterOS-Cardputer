"""Live provider checks: real network calls that use API quota. They run only on explicit opt-in
(OYSTER_LIVE_TESTS=1 together with AI_API_KEY and AI_MODEL), so a normal test run never spends quota.

Run explicitly:  .venv\\Scripts\\python.exe -m pytest tests/ai -m live_provider
"""

from __future__ import annotations

import os
import time

import pytest
from fake_store import TARGET_ID, AutoDevice, FakeStore, make_settings, setup_lab
from oyster.ai.cli import run_ai_check
from oyster.ai.orchestrator import Orchestrator
from oyster.ai.runner import build_provider

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        not (os.getenv("OYSTER_LIVE_TESTS") == "1" and os.getenv("AI_API_KEY") and os.getenv("AI_MODEL")),
        reason="live provider tests are opt-in: set OYSTER_LIVE_TESTS=1 with AI_API_KEY and AI_MODEL",
    ),
]


def _live_settings(tmp_path):
    return make_settings(
        tmp_path,
        ai_provider=os.getenv("AI_PROVIDER", "groq"),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1"),
        ai_model=os.environ["AI_MODEL"],
        ai_api_key=os.environ["AI_API_KEY"],
        ai_timeout_s=60,
    )


def test_live_tool_call_round_trip(tmp_path):
    result = run_ai_check(_live_settings(tmp_path))
    assert result["ok"], result


def test_live_orchestrated_run_cites_only_session_evidence(tmp_path):
    settings = _live_settings(tmp_path)
    provider = build_provider(settings)
    assert provider is not None and provider.is_fake is False
    store = FakeStore(now=time.time)
    sess = setup_lab(store)
    store.responder = AutoDevice()
    run = store.create_run(
        sess.session_id,
        f"Read the public sensor of {TARGET_ID} and explain the result.",
        provider.capabilities(),
    )
    final = Orchestrator(store, settings, provider, poll_interval_s=0.05).execute(run.run_id)
    assert final.status != "ai_unavailable", final.error
    if final.status == "succeeded":
        assert not store.missing_event_ids(sess.session_id, final.answer["cited_evidence_ids"])
