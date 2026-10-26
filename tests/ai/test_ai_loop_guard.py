"""Loop guard for backend tools.

A real model repeated `explain_evidence` with the same arguments five times in one run, waiting for the
provider's per-minute cap on every round. The same backend call is executed once; a repeat is refused and the
next request offers final_answer only, so the run ends with an answer within one more request. More than
MAX_BACKEND_CALLS backend calls in a run end the same way."""

from __future__ import annotations

from oyster.ai import fake as F
from oyster.ai.orchestrator import MAX_BACKEND_CALLS
from oyster.settings import RunBudgets
from test_ai_orchestrator import READ_PUBLIC, cite_last, make_env


def explain_last(messages, tools):
    return F.tool_call("explain_evidence", {"evidence_ids": F.evidence_ids_in(messages, last_only=True)})


def test_a_repeated_identical_backend_call_is_refused_and_the_answer_forced(tmp_path):
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), explain_last, explain_last, cite_last]
    env = make_env(tmp_path, script)
    run = env.execute()
    assert run.status == "succeeded", run.error
    executed = [s for s in run.steps if s["kind"] == "backend_tool" and s.get("outcome") == "succeeded"]
    assert len(executed) == 1
    assert any("already answered" in s["summary"] for s in run.steps)
    assert env.tool_names(3) == ["final_answer"]  # the request after the refusal offers only the answer


def test_too_many_backend_calls_force_the_answer(tmp_path):
    # different arguments each time, so the duplicate rule does not trigger; the count does
    def explain_nth(n):
        def call(messages, tools):
            return F.tool_call("explain_evidence", {"evidence_ids": [f"evt-{n:06d}"]})

        return call

    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC)]
    script += [explain_nth(i) for i in range(MAX_BACKEND_CALLS + 1)]
    script += [cite_last]
    # a budget wide enough that the request budget does not force the answer first
    wide = RunBudgets(
        max_actions=20, max_provider_requests=12, max_duration_s=180, max_confirmation_wait_s=120
    )
    env = make_env(tmp_path, script, budgets=wide)
    run = env.execute()
    assert run.status == "succeeded", run.error
    executed = [s for s in run.steps if s["kind"] == "backend_tool" and s.get("outcome") == "succeeded"]
    assert len(executed) == MAX_BACKEND_CALLS
    refused = [s for s in run.steps if "backend tool calls per run" in s["summary"]]
    assert refused, [s["summary"] for s in run.steps]
