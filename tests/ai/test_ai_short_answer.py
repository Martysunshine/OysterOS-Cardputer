"""A final_answer that is not an answer: one word, or the question repeated.

Seen with a real model on the device: "what is malware?" was answered "Malware". With strict answers on
(the default for real providers; the scripted fake has it off because tests stub their answers), a final
answer that cites no evidence and is shorter than the minimum, or that only repeats the question, is sent
back for the single repair round; a proper answer then succeeds and a second stub fails the run. A short
answer that cites evidence is still accepted (the evaluator's findings carry the substance)."""

from __future__ import annotations

from oyster.ai import fake as F
from oyster.ai.orchestrator import MIN_FINAL_ANSWER_CHARS, _echoes_question
from test_ai_orchestrator import READ_PUBLIC, cite_last, make_env

FULL = (
    "Malware is software written to harm or misuse a computer, a phone or a network: viruses, worms, "
    "ransomware, spyware and trojans are the common families. No measurement was taken for this question."
)


def test_a_one_word_answer_is_repaired_once(tmp_path):
    run = make_env(tmp_path, [F.final("Malware"), F.final(FULL)], strict_answers=True).execute()
    assert run.status == "succeeded", run.error
    assert run.answer["answer_markdown"] == FULL
    assert run.provider_requests_used == 2
    assert any("too short" in s["summary"] for s in run.steps if s["kind"] == "validation_error")


def test_a_second_stub_fails_the_run(tmp_path):
    run = make_env(tmp_path, [F.final("Malware"), F.final("Bad.")], strict_answers=True).execute()
    assert run.status == "failed" and run.answer is None


def test_repeating_the_question_is_not_an_answer(tmp_path):
    env = make_env(
        tmp_path, [F.final("How are you?"), F.final(FULL)], strict_answers=True, question="how are you?"
    )
    run = env.execute()
    assert run.status == "succeeded" and run.answer["answer_markdown"] == FULL


def test_a_short_answer_that_cites_evidence_is_accepted(tmp_path):
    script = [F.tool_call("ble_read_profile_value", READ_PUBLIC), cite_last]
    run = make_env(tmp_path, script, strict_answers=True).execute()
    assert run.status == "succeeded", run.error
    assert len(run.answer["answer_markdown"]) < MIN_FINAL_ANSWER_CHARS + 10
    assert run.answer["cited_evidence_ids"]


def test_the_fake_provider_has_the_check_off_by_default(tmp_path):
    run = make_env(tmp_path, [F.final("ok")]).execute()
    assert run.status == "succeeded"


def test_echo_detection_ignores_case_and_punctuation():
    assert _echoes_question("How are you?", "how are you")
    assert _echoes_question("  what is malware ", "What is malware?")
    assert not _echoes_question("Malware is harmful software.", "what is malware?")
    assert not _echoes_question("anything", None)
