"""Failure injection: executor failure, timeout, lost result, hangs; never retried automatically."""

from __future__ import annotations

import threading
import time

import pytest
from oyster.deck.actions import ActionResult


def final(kit, slot, key, confirmed):
    return kit.run_confirmed(kit.dev_a, slot, key) if confirmed else kit.run_and_finish(kit.dev_a, slot, key)


@pytest.mark.parametrize("slot,action,confirmed", [(1, "noop", False), (3, "service_restart", True)])
def test_executor_failure_ends_failed_for_both_kinds(kit, slot, action, confirmed):
    kit.executor.inject(action, "fail")
    j = final(kit, slot, "key-fail-00001", confirmed)
    assert j["state"] == "failed" and j["reason"] == "failed"
    assert j["steps"][0]["outcome"] == "failed"
    assert len(kit.executor.calls) == 1


@pytest.mark.parametrize(
    "kind,code", [("timeout", "timeout"), ("lost", "lost_result"), ("error", "executor_error")]
)
def test_unknown_result_of_an_impactful_action_is_unknown_outcome(kit, kind, code):
    kit.executor.inject("service_restart", kind)
    j = final(kit, 3, f"key-unk-{kind}-1", True)
    assert j["state"] == "unknown_outcome" and j["reason"] == code
    assert j["outcome_known"] is False and "never retried" in j["note"]
    assert j["steps"][0]["outcome"] == "unknown"


@pytest.mark.parametrize(
    "kind,code", [("timeout", "timeout"), ("lost", "lost_result"), ("error", "executor_error")]
)
def test_unknown_result_of_a_read_only_action_is_a_plain_failure(kit, kind, code):
    kit.executor.inject("noop", kind)
    j = final(kit, 1, f"key-ro-{kind}-001", False)
    assert j["state"] == "failed" and j["reason"] == code and "outcome_known" not in j


def test_an_unknown_outcome_is_never_retried_automatically(kit):
    kit.executor.inject("service_restart", "lost")
    j = final(kit, 3, "key-noretry-001", True)
    assert j["state"] == "unknown_outcome" and len(kit.executor.calls) == 1
    kit.clock.advance(600)
    kit.service.sweep()
    assert kit.drain() == 0
    assert kit.run(kit.dev_a, 3, "key-noretry-001").json()["state"] == "unknown_outcome"  # replay: stored job
    assert kit.confirm(kit.dev_a, j["job_id"], "nonce-00000001-abcdef").status_code == 409
    assert kit.drain() == 0 and len(kit.executor.calls) == 1
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "unknown_outcome"
    kit.service.recover_after_restart()  # a restart does not re-run it either
    assert kit.drain() == 0 and len(kit.executor.calls) == 1


def test_executor_error_text_is_never_exposed(kit):
    kit.executor.inject("service_restart", "error")
    j = final(kit, 3, "key-leak-00001", True)
    everything = str(j) + str(kit.audit_rows()) + kit.client.get("/v1/deck/audit", headers=kit.owner).text
    assert "injected" not in everything


def test_a_hanging_executor_is_cut_off_by_the_runner_timeout(kit):
    kit.runtime.runner.timeout_of = lambda action: 0.05
    kit.executor.inject("service_restart", "hang")
    started = time.monotonic()
    j = final(kit, 3, "key-hang-00001", True)
    assert time.monotonic() - started < 4
    assert j["state"] == "unknown_outcome" and j["reason"] == "timeout"
    kit.executor.release()
    kit.executor.inject("noop", "hang")
    j2 = final(kit, 1, "key-hang-00002", False)
    assert j2["state"] == "failed" and j2["reason"] == "timeout"


def test_the_first_failed_step_stops_the_job_and_later_steps_are_skipped(kit):
    kit.executor.inject("host_health_check", "fail")
    j = kit.run_and_finish(kit.dev_a, 6, "key-steps-0001")
    assert j["state"] == "failed"
    assert [s["outcome"] for s in j["steps"]] == ["failed", "skipped"]
    assert [type(c).__name__ for c in kit.executor.calls] == ["HostHealthCheck"]


def test_an_impactful_step_with_a_lost_result_ends_unknown_and_stops(kit):
    kit.executor.inject("service_restart", "lost")
    j = kit.run_confirmed(kit.dev_a, 5, "key-steps-0002")
    assert j["state"] == "unknown_outcome"
    assert [s["outcome"] for s in j["steps"]] == ["succeeded", "unknown"]  # step 1 result is kept


def test_a_read_only_step_before_an_impactful_one_failing_prevents_the_impactful_step(kit):
    kit.executor.inject("service_status", "fail")
    j = kit.run_confirmed(kit.dev_a, 5, "key-steps-0003")
    assert j["state"] == "failed" and [s["outcome"] for s in j["steps"]] == ["failed", "skipped"]
    assert [type(c).__name__ for c in kit.executor.calls] == ["ServiceStatus"]


def test_results_are_sanitised_and_labelled_as_mock(kit):
    j = kit.run_and_finish(kit.dev_a, 1, "key-sanit-0001")
    assert j["executor"] == "mock" and "mock executor" in j["steps"][0]["summary"]

    class Noisy:
        kind = "mock"

        def execute(self, action):
            return ActionResult(ok=True, code="Not A Code!", summary="\x1b[31mred\x00" + "x" * 500)

    kit.runtime.runner.executor = Noisy()
    j = kit.run_and_finish(kit.dev_a, 2, "key-sanit-0002")
    step = j["steps"][0]
    assert step["code"] == "ok" and step["summary"].startswith("[31mred") and len(step["summary"]) == 120
    assert "\x1b" not in step["summary"] and "\x00" not in step["summary"]

    class Bad:
        kind = "mock"

        def execute(self, action):
            return "not a result"

    kit.runtime.runner.executor = Bad()
    assert kit.run_and_finish(kit.dev_a, 1, "key-sanit-0003")["reason"] == "executor_error"


def test_not_ok_result_is_a_failure(kit):
    class Refuses:
        kind = "mock"

        def execute(self, action):
            return ActionResult(ok=False, code="host_down", summary="unreachable")

    kit.runtime.runner.executor = Refuses()
    j = kit.run_and_finish(kit.dev_a, 2, "key-notok-0001")
    assert j["state"] == "failed" and j["reason"] == "host_down" and j["steps"][0]["summary"] == "unreachable"


def test_the_worker_thread_runs_jobs_and_stops_cleanly(kit):
    runner = kit.runtime.runner
    runner.sweep_interval_s = 0.05
    runner.start()
    runner.start()  # idempotent
    try:
        job_id = kit.run(kit.dev_a, 1, "key-thread-001").json()["job_id"]
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            state = kit.job(kit.dev_a, job_id).json()["state"]
            if state == "succeeded":
                break
            time.sleep(0.02)
        assert state == "succeeded" and len(kit.executor.calls) == 1
        assert sum(t.name == "oyster-deck-runner" for t in threading.enumerate()) == 1
    finally:
        runner.stop()
        runner.stop()
    assert not any(t.name == "oyster-deck-runner" for t in threading.enumerate())


def test_the_worker_thread_sweeps_deadlines_while_idle(kit):
    j = kit.run(kit.dev_a, 3, "key-thread-002").json()
    kit.clock.advance(121)
    runner = kit.runtime.runner
    runner.sweep_interval_s = 0.05
    runner.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with kit.runtime.store.tx() as conn:
                state = conn.execute("SELECT state FROM jobs WHERE job_id=?", (j["job_id"],)).fetchone()[0]
            if state == "expired":
                break
            time.sleep(0.02)
        assert state == "expired"
    finally:
        runner.stop()


def test_a_crashing_executor_does_not_kill_the_worker(kit):
    runner = kit.runtime.runner
    runner.sweep_interval_s = 0.05
    kit.executor.inject("noop", "error")
    runner.start()
    try:
        first = kit.run(kit.dev_a, 1, "key-thread-003").json()["job_id"]
        second = kit.run(kit.dev_a, 1, "key-thread-004").json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and kit.job(kit.dev_a, second).json()["state"] != "succeeded":
            time.sleep(0.02)
        assert kit.job(kit.dev_a, first).json()["state"] == "failed"
        assert kit.job(kit.dev_a, second).json()["state"] == "succeeded"
    finally:
        runner.stop()


def test_the_largest_possible_job_response_stays_under_4_kib(kit, make_profile):
    host, service = "h" * 40, "s" * 64
    step = {"type": "service_status", "host_id": host, "service": service}
    kit.put_profile(
        make_profile(
            hosts={host: {"services": [service]}},
            slots={"1": {"label": "L" * 24, "actions": [step, step, step]}},
        )
    )

    class Verbose:
        kind = "mock"

        def execute(self, action):
            return ActionResult(ok=True, code="c" * 40, summary="x" * 500)

    kit.runtime.runner.executor = Verbose()
    r = kit.run(kit.dev_a, 1, "key-size-00001")
    assert len(r.content) < 4096
    kit.drain()
    done = kit.job(kit.dev_a, r.json()["job_id"])
    assert done.json()["state"] == "succeeded" and len(done.content) < 4096


def test_the_worker_starts_itself_on_the_first_job_when_no_lifespan_runs(kit_factory):
    """`oyster serve` runs its apps with the lifespan off: the runner must not depend on it."""
    kit = kit_factory(autostart=True)
    dev = kit.enroll("dev-a")
    kit.put_profile()
    assert kit.runtime.runner._thread is None
    job_id = kit.run(dev, 1, "key-auto-00001").json()["job_id"]
    deadline = time.monotonic() + 5
    state = None
    while time.monotonic() < deadline:
        state = kit.job(dev, job_id).json()["state"]
        if state == "succeeded":
            break
        time.sleep(0.02)
    assert state == "succeeded" and len(kit.executor.calls) == 1
    kit.runtime.stop()  # nothing started it through a lifespan; stop still ends the worker
    assert kit.runtime.runner._thread is None
