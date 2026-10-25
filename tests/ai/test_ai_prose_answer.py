"""A model that answers in plain text instead of calling final_answer.

Real models sometimes finish a run by speaking. The loop allows one repair attempt; if the model still
replies in prose and the run has real results, that prose becomes a clearly labelled answer. It goes
through the same validation as a structured answer, and a model that claimed something without running
anything is still refused."""

from __future__ import annotations

from oyster.ai import fake as F
from oyster.ai.orchestrator import MIN_PROSE_ANSWER_CHARS, PROSE_ANSWER_LIMITATION
from test_ai_orchestrator import READ_PUBLIC, cite_last, make_env

LONG = "The public sensor returned a two byte value on an unencrypted link, which matches its public policy."


def test_prose_after_real_results_becomes_a_labelled_answer(tmp_path):
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), F.prose(LONG), F.prose(LONG)]
    env = make_env(tmp_path, script)
    run = env.execute()
    assert run.status == "succeeded", run.error
    assert run.answer["answer_markdown"].startswith("The public sensor returned")
    assert run.answer["cited_evidence_ids"] == []
    assert PROSE_ANSWER_LIMITATION in run.answer["limitations"]
    assert run.provider_requests_used == 3  # one tool call, then the reply and the single repair attempt
    last = run.steps[-1]
    assert last["kind"] == "final_answer" and "plain-text" in last["summary"]


def test_a_structured_answer_after_the_repair_note_is_preferred(tmp_path):
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), F.prose(LONG), cite_last]
    run = make_env(tmp_path, script).execute()
    assert run.status == "succeeded"
    assert PROSE_ANSWER_LIMITATION not in run.answer["limitations"]
    assert run.answer["cited_evidence_ids"]


def test_prose_without_any_results_is_still_refused(tmp_path):
    run = make_env(tmp_path, [F.prose(LONG), F.prose(LONG)]).execute()
    assert run.status == "failed" and run.answer is None


def test_a_short_reply_is_not_accepted(tmp_path):
    short = "All good."
    assert len(short) < MIN_PROSE_ANSWER_CHARS
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), F.prose(short), F.prose(short)]
    assert make_env(tmp_path, script).execute().status == "failed"


def test_ids_named_in_prose_must_exist(tmp_path):
    invented = LONG + " See evt-999999 and finding-zzzz9 for details."
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), F.prose(invented), F.prose(invented)]
    run = make_env(tmp_path, script).execute()
    assert run.status == "failed" and run.answer is None
    assert any("Plain-text answer rejected" in s["summary"] for s in run.steps)


def test_links_and_images_in_prose_are_stripped(tmp_path):
    text = LONG + " More at [details](https://example.invalid/x) ![pic](https://example.invalid/y.png)."
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), F.prose(text), F.prose(text)]
    run = make_env(tmp_path, script).execute()
    assert run.status == "succeeded"
    assert "example.invalid" not in run.answer["answer_markdown"]
    assert "](" not in run.answer["answer_markdown"]
