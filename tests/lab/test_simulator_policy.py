"""Local policy and result-submission behaviour: the confirmation gate cannot be bypassed by the job
flag, a job id is never re-executed, result submission is idempotent and retried on transport error,
and malformed/out-of-scope jobs are rejected locally."""

from __future__ import annotations

import pytest
from _simulator_backend import FakeBackend
from oyster_lab.simulator import events, policy
from oyster_lab.simulator.device import Simulator
from oyster_lab.simulator.scenarios import load_scenario

TARGET = "target-lab-01"


def _sim(scenario_name="corrected_target", **kw):
    scenario = load_scenario(scenario_name)
    backend = FakeBackend()
    client = backend.client()
    sim = Simulator(scenario, client, confirm_wait_s=0.0, **kw)
    session_id = sim.register_session()
    return scenario, backend, client, sim, session_id


def _write_job(job_id, session_id, *, requires_local_confirmation, nonce="n-write-1"):
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_write_lab_value",
        "arguments": {"characteristic_id": "virtual_led", "value": "green", "restore": True},
        "scope_version": 1,
        "deadline_s": 150,
        "limits": {"max_bytes": 1, "max_duration_s": 30},
        "requires_local_confirmation": requires_local_confirmation,
        "nonce": nonce,
    }


def test_confirmation_gate_not_bypassed_by_false_flag():
    # Job claims no confirmation is needed, but a write is always gated by the device.
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    backend.enqueue(_write_job("job-write-01", session_id, requires_local_confirmation=False))
    report = sim.run()
    client.close()
    assert ("job-write-01", "awaiting_local_confirmation") in backend.status_log
    assert report.jobs[0].confirmation_approved is True
    assert report.jobs[0].outcome == "succeeded"


def test_confirmation_denied_without_auto_confirm_is_cancelled():
    _s, backend, client, sim, session_id = _sim(auto_confirm=False)
    backend.enqueue(_write_job("job-write-02", session_id, requires_local_confirmation=False))
    report = sim.run()
    client.close()
    assert ("job-write-02", "awaiting_local_confirmation") in backend.status_log
    assert ("job-write-02", "running") not in backend.status_log
    assert report.jobs[0].outcome == "cancelled"
    assert report.jobs[0].confirmation_approved is False
    # A cancelled (unapproved) job uploads no evidence.
    assert backend.events == {}


def test_confirmation_result_carries_bound_approval():
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    backend.enqueue(_write_job("job-write-03", session_id, requires_local_confirmation=True, nonce="n-bound"))
    sim.run()
    client.close()
    submitted = backend.result_by_job["job-write-03"]
    assert submitted["confirmation"] == {"job_id": "job-write-03", "nonce": "n-bound", "approved": True}


def test_duplicate_job_id_not_re_executed():
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    job = {
        "schema_version": "1.0",
        "job_id": "job-dupe-01",
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_read_profile_value",
        "arguments": {"characteristic_id": "public_sensor"},
        "scope_version": 1,
        "deadline_s": 30,
        "limits": {"max_bytes": 512},
        "requires_local_confirmation": False,
        "nonce": None,
    }
    backend.enqueue(dict(job))
    backend.enqueue(dict(job))  # same id delivered twice
    report = sim.run()
    client.close()
    assert len(report.jobs) == 2
    assert report.jobs[0].skipped_duplicate is False
    assert report.jobs[1].skipped_duplicate is True
    # Exactly one execution's worth of evidence and one stored result.
    events_after_first = len(report.jobs[0].event_ids)
    assert len(backend.events) == events_after_first
    assert list(backend.result_by_job.keys()) == ["job-dupe-01"]


def test_result_submission_is_idempotent():
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    job = {
        "schema_version": "1.0",
        "job_id": "job-idem-01",
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_read_profile_value",
        "arguments": {"characteristic_id": "public_sensor"},
        "scope_version": 1,
        "deadline_s": 30,
        "limits": {"max_bytes": 512},
        "requires_local_confirmation": False,
        "nonce": None,
    }
    backend.enqueue(dict(job))
    report = sim.run()
    # Re-submit the same result directly (simulating a retried submission).
    first = backend.result_by_job["job-idem-01"]
    resp = client.post("/v1/jobs/job-idem-01/result", json=first)
    client.close()
    assert resp.json()["duplicate"] is True
    assert report.jobs[0].outcome == "succeeded"


def test_result_retried_on_transport_error():
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    backend.result_fail_remaining = 1  # first result POST raises, retry succeeds
    job = {
        "schema_version": "1.0",
        "job_id": "job-retry-01",
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_read_profile_value",
        "arguments": {"characteristic_id": "public_sensor"},
        "scope_version": 1,
        "deadline_s": 30,
        "limits": {"max_bytes": 512},
        "requires_local_confirmation": False,
        "nonce": None,
    }
    backend.enqueue(dict(job))
    report = sim.run()
    client.close()
    assert report.jobs[0].outcome == "succeeded"
    assert "job-retry-01" in backend.result_by_job


@pytest.mark.parametrize(
    "mutate,expected_verdict",
    [
        ({"operation": "not_a_tool"}, policy.Verdict.reject_unknown_operation),
        ({"session_id": "sess-other-999"}, policy.Verdict.reject_wrong_session),
        ({"scope_version": 2}, policy.Verdict.reject_stale_scope),
        ({"target_id": "target-unknown-99"}, policy.Verdict.reject_unknown_target),
        ({"limits": {"max_duration_s": 9000}}, policy.Verdict.reject_limits_exceeded),
        ({"device_id": "someone-else"}, policy.Verdict.reject_wrong_device),
    ],
)
def test_policy_rejects_bad_jobs(mutate, expected_verdict):
    _s, backend, client, sim, session_id = _sim(auto_confirm=True)
    job = {
        "schema_version": "1.0",
        "job_id": "job-bad-01",
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_read_profile_value",
        "arguments": {"characteristic_id": "public_sensor"},
        "scope_version": 1,
        "deadline_s": 30,
        "limits": {"max_bytes": 512},
        "requires_local_confirmation": False,
        "nonce": None,
    }
    job.update(mutate)
    backend.enqueue(job)
    report = sim.run()
    client.close()
    assert report.jobs[0].verdict == expected_verdict.value
    # A rejected job executes nothing.
    assert backend.events == {}


def test_write_requires_confirmation_derivation():
    assert policy.operation_requires_local_confirmation("ble_write_lab_value") is True
    assert policy.operation_requires_local_confirmation("ble_collect_diagnostics") is True
    assert policy.operation_requires_local_confirmation("ble_read_profile_value") is False
    assert policy.operation_requires_local_confirmation("wifi_survey") is False


# --- running report must be accepted before execution (confirmation-timeout race) ---
def _sim_with_confirm(decision):
    scenario = load_scenario("corrected_target")
    backend = FakeBackend()
    client = backend.client()
    asked = []

    def owner(job):
        asked.append(job["job_id"])
        return decision

    sim = Simulator(scenario, client, confirm=owner, confirm_wait_s=0.0)
    session_id = sim.register_session()
    return backend, client, sim, session_id, asked


def _assert_nothing_executed(backend, report, job_id):
    jr = report.jobs[0]
    assert jr.executed is False and jr.outcome is None and jr.event_ids == []
    assert backend.events == {}  # no evidence uploaded
    assert job_id not in backend.result_by_job and backend.result_submissions == []  # no result


def test_late_approval_after_backend_expiry_does_not_execute():
    backend, client, sim, session_id, asked = _sim_with_confirm(True)
    backend.enqueue(_write_job("job-late-01", session_id, requires_local_confirmation=True))
    backend.reject_status.add(("job-late-01", "running"))  # the backend expired the job meanwhile
    report = sim.run()
    client.close()
    assert asked == ["job-late-01"]  # the owner did approve, but too late
    assert backend.status_log == [
        ("job-late-01", "awaiting_local_confirmation"),
        ("job-late-01", "running"),
    ]
    _assert_nothing_executed(backend, report, "job-late-01")
    assert report.jobs[0].statuses == ["awaiting_local_confirmation", "running:rejected"]


def test_rejected_awaiting_report_never_prompts_the_owner():
    backend, client, sim, session_id, asked = _sim_with_confirm(True)
    backend.enqueue(_write_job("job-late-02", session_id, requires_local_confirmation=True))
    backend.reject_status.add(("job-late-02", "awaiting_local_confirmation"))
    report = sim.run()
    client.close()
    assert asked == []
    _assert_nothing_executed(backend, report, "job-late-02")


def test_gated_job_does_not_run_when_the_running_report_is_lost():
    backend, client, sim, session_id, _asked = _sim_with_confirm(True)
    backend.enqueue(_write_job("job-late-03", session_id, requires_local_confirmation=True))
    backend.status_transport_error.add(("job-late-03", "running"))
    report = sim.run()
    client.close()
    _assert_nothing_executed(backend, report, "job-late-03")


def _read_job(job_id, session_id):
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_read_profile_value",
        "arguments": {"characteristic_id": "public_sensor"},
        "scope_version": 1,
        "deadline_s": 30,
        "limits": {"max_bytes": 512},
        "requires_local_confirmation": False,
        "nonce": None,
    }


def test_ungated_job_rejected_running_is_not_executed_but_lost_report_is_advisory():
    backend, client, sim, session_id, _asked = _sim_with_confirm(True)
    backend.enqueue(_read_job("job-read-rej", session_id))
    backend.reject_status.add(("job-read-rej", "running"))
    report = sim.run()
    _assert_nothing_executed(backend, report, "job-read-rej")
    backend.enqueue(_read_job("job-read-lost", session_id))
    backend.status_transport_error.add(("job-read-lost", "running"))
    report = sim.run()
    client.close()
    assert report.jobs[0].executed is True and report.jobs[0].outcome == "succeeded"
    assert "job-read-lost" in backend.result_by_job


def test_gated_order_is_confirm_then_running_then_execute_then_result():
    backend, client, sim, session_id, _asked = _sim_with_confirm(True)
    backend.enqueue(_write_job("job-order-01", session_id, requires_local_confirmation=False, nonce="n-o1"))
    report = sim.run()
    client.close()
    assert backend.status_log == [
        ("job-order-01", "awaiting_local_confirmation"),
        ("job-order-01", "running"),
    ]
    assert report.jobs[0].executed is True
    result = backend.result_by_job["job-order-01"]
    assert result["confirmation"] == {"job_id": "job-order-01", "nonce": "n-o1", "approved": True}
    assert set(result["event_ids"]) <= set(backend.events)


# --- ble_check_access_policy confirmation derived from the enrolled profile's assertion ---
def _assertion_job(job_id, session_id, test_id, nonce="n-assert"):
    return {
        "schema_version": "1.0",
        "job_id": job_id,
        "session_id": session_id,
        "device_id": "sim-cardputer-01",
        "target_id": TARGET,
        "operation": "ble_check_access_policy",
        "arguments": {"test_id": test_id},
        "scope_version": 1,
        "deadline_s": 150,
        "limits": {"max_bytes": 65536, "max_duration_s": 120},
        "requires_local_confirmation": False,  # never trusted by the device
        "nonce": nonce,
    }


@pytest.mark.parametrize(
    "assertion", events.load_profile()["assertions"], ids=lambda a: a["test_id"]
)
def test_access_policy_gate_follows_the_assertion_operation(assertion):
    backend, client, sim, session_id, asked = _sim_with_confirm(True)
    test_id = assertion["test_id"]
    backend.enqueue(_assertion_job("job-assert-01", session_id, test_id))
    report = sim.run()
    client.close()
    gated = assertion["operation"] in ("write", "transfer")
    assert (("job-assert-01", "awaiting_local_confirmation") in backend.status_log) is gated
    assert (asked == ["job-assert-01"]) is gated
    assert report.jobs[0].executed is True
    confirmation = backend.result_by_job["job-assert-01"]["confirmation"]
    assert (confirmation is not None and confirmation["approved"]) is gated


def test_gated_assertion_without_nonce_is_rejected_locally():
    backend, client, sim, session_id, _asked = _sim_with_confirm(True)
    job = _assertion_job(
        "job-assert-02", session_id, "virtual_led_write_requires_app_authorization", nonce=None
    )
    backend.enqueue(job)
    report = sim.run()
    client.close()
    assert report.jobs[0].verdict == policy.Verdict.reject_malformed.value
    assert report.jobs[0].executed is False and backend.events == {}


def test_policy_derivation_for_assertions_and_unknown_profiles():
    profile = events.load_profile()
    for a in profile["assertions"]:
        expected = a["operation"] in ("write", "transfer")
        args = {"test_id": a["test_id"]}
        assert policy.job_requires_local_confirmation("ble_check_access_policy", args, profile) is expected
    args = {"test_id": "public_sensor_is_public"}
    assert policy.job_requires_local_confirmation("ble_check_access_policy", args, None) is True
    unknown = {"test_id": "nope"}
    assert policy.job_requires_local_confirmation("ble_check_access_policy", unknown, profile) is True
    assert policy.operation_changes_target_state("ble_check_access_policy") is True
