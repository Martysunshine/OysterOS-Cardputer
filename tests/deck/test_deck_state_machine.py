"""Job state machine: the transition table, every legal and illegal transition, and the deadlines."""

from __future__ import annotations

import itertools
import queue
import sqlite3

import pytest
from oyster.deck import jobs as J
from oyster.deck.jobs import can_transition

LEGAL_ANY = {
    ("queued", "cancelled"),
    ("queued", "expired"),
    ("queued", "failed"),
    ("awaiting_confirmation", "running"),
    ("awaiting_confirmation", "cancelled"),
    ("awaiting_confirmation", "expired"),
    ("running", "succeeded"),
    ("running", "failed"),
}
LEGAL_IMPACTFUL_ONLY = {
    ("queued", "awaiting_confirmation"),
    ("running", "unknown_outcome"),
}
LEGAL_NON_IMPACTFUL_ONLY = {("queued", "running")}
STATES = (
    "queued",
    "awaiting_confirmation",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "unknown_outcome",
)


def test_states_are_exactly_the_documented_set():
    assert J.STATES == STATES
    assert set(J.TERMINAL) == {"succeeded", "failed", "cancelled", "expired", "unknown_outcome"}
    assert set(J.TRANSITIONS) == set(STATES)


@pytest.mark.parametrize("impactful", [True, False])
@pytest.mark.parametrize("old,new", list(itertools.product(STATES, STATES)))
def test_every_pair_of_states_is_either_legal_or_illegal_exactly_as_documented(old, new, impactful):
    legal = (old, new) in LEGAL_ANY or (old, new) in (
        LEGAL_IMPACTFUL_ONLY if impactful else LEGAL_NON_IMPACTFUL_ONLY
    )
    assert can_transition(old, new, impactful) is legal


def test_terminal_states_are_final():
    for state in J.TERMINAL:
        assert J.TRANSITIONS[state] == frozenset()
        for new in STATES:
            for impactful in (True, False):
                assert not can_transition(state, new, impactful)


def test_unknown_states_are_illegal():
    assert not can_transition("queued", "bogus", False)
    assert not can_transition("bogus", "queued", False)


def test_the_database_refuses_a_state_outside_the_table(kit):
    r = kit.run(kit.dev_a, 1, "key-state-001")
    with pytest.raises(sqlite3.IntegrityError), kit.runtime.store.tx() as conn:
        conn.execute("UPDATE jobs SET state='bogus' WHERE job_id=?", (r.json()["job_id"],))


def test_service_refuses_an_illegal_transition(kit):
    r = kit.run(kit.dev_a, 3, "key-state-002")
    with kit.runtime.store.tx() as conn:
        job = kit.runtime.store.get_job(conn, r.json()["job_id"])
        with pytest.raises(J.IllegalTransition):
            kit.service._move(conn, job, "succeeded", actor=("system", "t"))  # awaiting -> succeeded
        with pytest.raises(J.IllegalTransition):
            kit.service._move(conn, job, "unknown_outcome", actor=("system", "t"))


def test_an_impactful_job_cannot_reach_running_without_its_confirmation(kit):
    r = kit.run(kit.dev_a, 3, "key-state-003")
    job_id = r.json()["job_id"]
    assert kit.drain() == 0  # impactful jobs are never handed to the runner before approval
    assert kit.service.begin(job_id) is None
    assert kit.job(kit.dev_a, job_id).json()["state"] == "awaiting_confirmation"
    assert kit.executor.calls == []


def test_non_impactful_jobs_skip_confirmation(kit):
    r = kit.run(kit.dev_a, 1, "key-state-004").json()
    assert r["state"] == "queued" and r["requires_confirmation"] is False and "nonce" not in r
    kit.drain()
    assert kit.job(kit.dev_a, r["job_id"]).json()["state"] == "succeeded"
    assert kit.events(r["job_id"]) == ["job.queued", "job.running", "job.succeeded"]


def test_impactful_path_queued_awaiting_running_succeeded(kit):
    j = kit.run_confirmed(kit.dev_a, 3, "key-state-005")
    assert j["state"] == "succeeded"
    assert kit.events(j["job_id"]) == [
        "job.queued",
        "job.awaiting_confirmation",
        "job.running",
        "job.succeeded",
    ]


def test_queued_to_cancelled_and_never_started(kit):
    r = kit.run(kit.dev_a, 1, "key-state-006").json()
    assert kit.cancel(kit.dev_a, r["job_id"]).json()["state"] == "cancelled"
    assert kit.drain() == 1  # the queue entry is consumed...
    assert kit.executor.calls == []  # ...but a cancelled job is not started
    assert kit.events(r["job_id"]) == ["job.queued", "job.cancelled"]


def test_awaiting_to_cancelled_by_decline_and_by_cancel(kit):
    declined = kit.run_confirmed(kit.dev_a, 3, "key-state-007", approved=False)
    assert declined["state"] == "cancelled" and declined["reason"] == "declined"
    r = kit.run(kit.dev_a, 3, "key-state-008").json()
    assert kit.cancel(kit.dev_a, r["job_id"]).json()["reason"] == "cancelled_by_device"
    assert kit.executor.calls == []


def test_cancel_is_idempotent_and_refused_once_running_or_finished(kit):
    r = kit.run(kit.dev_a, 3, "key-state-009").json()
    assert kit.cancel(kit.dev_a, r["job_id"]).status_code == 200
    assert kit.cancel(kit.dev_a, r["job_id"]).status_code == 200  # already cancelled: same answer
    approved = kit.run(kit.dev_a, 4, "key-state-010").json()
    kit.confirm(kit.dev_a, approved["job_id"], approved["nonce"])
    refused = kit.cancel(kit.dev_a, approved["job_id"])  # running
    assert refused.status_code == 409 and refused.json()["error"] == "not_cancellable"
    kit.drain()
    assert kit.cancel(kit.dev_a, approved["job_id"]).status_code == 409  # succeeded
    assert kit.job(kit.dev_a, approved["job_id"]).json()["state"] == "succeeded"


def test_confirmation_window_is_120_seconds(kit):
    r = kit.run(kit.dev_a, 3, "key-state-011").json()
    assert r["confirm_deadline"] - r["created_at"] == 120
    kit.clock.advance(119)
    assert kit.job(kit.dev_a, r["job_id"]).json()["state"] == "awaiting_confirmation"
    kit.clock.advance(1)
    j = kit.job(kit.dev_a, r["job_id"]).json()
    assert j["state"] == "expired" and j["reason"] == "confirmation_timeout"
    assert "nonce" not in j
    assert kit.events(r["job_id"])[-1] == "job.expired"


def test_queued_job_expires_when_the_runner_never_takes_it(kit):
    r = kit.run(kit.dev_a, 1, "key-state-012").json()
    kit.clock.advance(120)
    assert kit.drain() == 1  # the runner finally gets to it: too late
    j = kit.job(kit.dev_a, r["job_id"]).json()
    assert j["state"] == "expired" and j["reason"] == "queue_timeout"
    assert kit.executor.calls == []


def test_running_non_impactful_job_past_its_budget_fails_and_impactful_becomes_unknown(kit):
    quick = kit.run(kit.dev_a, 1, "key-state-013").json()
    risky = kit.run(kit.dev_a, 4, "key-state-014").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])
    assert kit.service.begin(quick["job_id"]) is not None  # runner claims both, then stalls
    assert kit.service.begin(risky["job_id"]) is not None
    kit.clock.advance(5 + 10)  # noop budget is 5 s + 10 s grace
    assert kit.job(kit.dev_a, quick["job_id"]).json()["state"] == "failed"
    assert kit.job(kit.dev_a, risky["job_id"]).json()["state"] == "running"  # wake_on_lan: 15 + 10 s
    kit.clock.advance(10)
    j = kit.job(kit.dev_a, risky["job_id"]).json()
    assert j["state"] == "unknown_outcome" and j["reason"] == "run_timeout" and j["outcome_known"] is False


def test_the_first_recorded_outcome_stands(kit):
    risky = kit.run(kit.dev_a, 4, "key-state-015").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])
    kit.service.begin(risky["job_id"])
    kit.clock.advance(1000)
    assert kit.job(kit.dev_a, risky["job_id"]).json()["state"] == "unknown_outcome"  # swept
    assert kit.service.finish(risky["job_id"], "succeeded", [], None) is None  # late runner result ignored
    assert kit.job(kit.dev_a, risky["job_id"]).json()["state"] == "unknown_outcome"


def test_a_job_is_claimed_for_execution_once(kit):
    r = kit.run(kit.dev_a, 1, "key-state-016").json()
    assert kit.service.begin(r["job_id"]) is not None
    assert kit.service.begin(r["job_id"]) is None  # a second claim (duplicate queue entry) does nothing
    risky = kit.run(kit.dev_a, 4, "key-state-017").json()
    kit.confirm(kit.dev_a, risky["job_id"], risky["nonce"])
    assert kit.service.begin(risky["job_id"]) is not None
    assert kit.service.begin(risky["job_id"]) is None


def test_queue_full_race_fails_the_job_instead_of_leaving_it_queued(kit, monkeypatch):
    def full(job_id):
        raise queue.Full

    monkeypatch.setattr(kit.runtime.runner, "submit", full)
    kit.service.attach_runner(full, lambda: True)
    r = kit.run(kit.dev_a, 1, "key-state-018")
    assert r.status_code == 503 and r.json()["error"] == "busy"
    jobs = kit.client.get("/v1/deck/jobs", headers=kit.owner).json()["jobs"]
    assert [(j["state"], j["reason"]) for j in jobs] == [("failed", "queue_full")]
    again = kit.run(kit.dev_a, 1, "key-state-018")  # same key: the stored (failed) job, never re-run
    assert (
        again.status_code == 200 and again.json()["state"] == "failed" and again.json()["duplicate"] is True
    )
