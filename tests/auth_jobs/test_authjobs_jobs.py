"""Job creation rules and the device job lifecycle (docs/API.md 'Job lifecycle')."""

from __future__ import annotations

import pytest
from oyster.evidence.validate import schema_errors
from oyster.store import Conflict, NotFound


@pytest.fixture
def dev(lab):
    lab.enroll_target()
    headers = lab.enroll("sim-01")
    sid = lab.session(headers)
    return headers, sid


# --- creation-time rejection ---
def test_unsupported_operation_rejected(lab, dev):
    _, sid = dev
    with pytest.raises(Conflict) as e:
        lab.job(sid, operation="ble_spray_and_pray")
    assert e.value.code == "unsupported_operation"
    # an operation the device did not declare for this session is unsupported too
    narrow = lab.session(lab.enroll("sim-02"), caps=["wifi_survey"])
    with pytest.raises(Conflict) as e:
        lab.job(narrow)
    assert e.value.code == "unsupported_operation"


def test_stale_scope_version_rejected_at_creation(lab, dev):
    _, sid = dev
    with pytest.raises(Conflict) as e:
        lab.job(sid, scope_version=2)
    assert e.value.code == "stale_scope"
    lab.enroll_target(scope_version=2)
    with pytest.raises(Conflict) as e:
        lab.job(sid, scope_version=1)
    assert e.value.code == "stale_scope"
    assert lab.job(sid, scope_version=2).status == "queued"
    # scope_version may not go backwards
    current = lab.c.get("/v1/admin/targets", headers=lab.owner).json()["targets"][0]
    r = lab.c.post("/v1/admin/targets", headers=lab.owner, json={**current, "scope_version": 1})
    assert r.status_code == 409 and r.json()["error"] == "stale_scope"


def test_raising_scope_after_queueing_makes_the_job_stale(lab, dev):
    headers, sid = dev
    rec = lab.job(sid)
    lab.enroll_target(scope_version=2)
    assert lab.poll(headers) == []
    assert lab.store.get_job(rec.job.job_id).status == "failed"
    assert lab.store.get_job_audit(rec.job.job_id)["status_reason"] == "stale_scope"


def test_target_rules(lab, dev):
    _, sid = dev
    with pytest.raises(NotFound):
        lab.job(sid, target_id="target-unknown")
    with pytest.raises(Conflict) as e:
        lab.job(sid, target_id=None)
    assert e.value.code == "validation_failed"
    with pytest.raises(Conflict):
        lab.job(sid, operation="wifi_survey", arguments={}, target_id="target-lab-01")
    lab.enroll_target(target_id="target-ro-01", allowed_operations=["ble_read_profile_value"])
    with pytest.raises(Conflict) as e:
        lab.job(
            sid,
            operation="ble_write_lab_value",
            target_id="target-ro-01",
            arguments={"characteristic_id": "virtual_led", "value": "green"},
        )
    assert e.value.code == "operation_not_allowed"


@pytest.mark.parametrize(
    "arguments, code",
    [
        ({"characteristic_id": "public_sensor", "pad": "x" * 600}, "payload_too_large"),
        ({"characteristic_id": "public_sensor", "extra": 1}, "validation_failed"),
        ({"characteristic_id": "not_in_profile"}, "validation_failed"),
        ({"characteristic_id": "chunk_ctrl"}, "validation_failed"),  # write-only characteristic
        ({}, "validation_failed"),
    ],
)
def test_arguments_over_limits_or_untyped_are_rejected(lab, dev, arguments, code):
    _, sid = dev
    with pytest.raises(Conflict) as e:
        lab.job(sid, arguments=arguments)
    assert e.value.code == code


def test_limits_and_deadline_bounds(lab, dev):
    _, sid = dev
    with pytest.raises(Conflict) as e:
        lab.job(sid, limits={"max_bytes": 100000})
    assert e.value.code == "limits_exceeded"
    with pytest.raises(Conflict) as e:
        lab.job(sid, deadline_s=601)
    assert e.value.code == "validation_failed"
    with pytest.raises(Conflict):
        lab.job(sid, limits={"max_tokens": 5})
    with pytest.raises(Conflict):
        lab.job(sid, operation="wifi_survey", target_id=None, arguments={"ms_per_channel": 5})


def test_write_job_always_requires_local_confirmation(lab, dev):
    headers, sid = dev
    rec = lab.job(
        sid,
        operation="ble_write_lab_value",
        arguments={"characteristic_id": "virtual_led", "value": "green", "restore": True},
        confirm=False,
    )
    assert rec.job.requires_local_confirmation is True and rec.job.nonce
    diag = lab.job(
        sid, operation="ble_collect_diagnostics", arguments={"object_id": "diag-synth-0001"}, confirm=False
    )
    assert diag.job.requires_local_confirmation is True
    # a caller may raise the requirement for a read, never lower it for a write
    assert lab.job(sid, confirm=True).job.requires_local_confirmation is True
    assert lab.job(sid, confirm=False).job.requires_local_confirmation is False
    delivered = lab.poll(headers)[0]
    assert (
        delivered["operation"] == "ble_write_lab_value" and delivered["requires_local_confirmation"] is True
    )
    with pytest.raises(Conflict):
        lab.job(
            sid,
            operation="ble_write_lab_value",
            arguments={"characteristic_id": "virtual_led", "value": "pink"},
        )


# --- delivery and lifecycle ---
def test_at_most_one_job_per_poll_in_creation_order(lab, dev):
    headers, sid = dev
    first, second = lab.job(sid), lab.job(sid, arguments={"characteristic_id": "device_info"})
    got1 = lab.poll(headers)
    got2 = lab.poll(headers)
    assert [j["job_id"] for j in got1] == [first.job.job_id]
    assert [j["job_id"] for j in got2] == [second.job.job_id]
    assert lab.poll(headers) == []
    assert not schema_errors("job.schema.json", got1[0])
    assert lab.store.get_job(first.job.job_id).status == "delivered"


def test_status_transitions_are_forward_only(lab, dev):
    headers, sid = dev
    queued = lab.job(sid)
    r = lab.status(headers, queued.job.job_id, "running")
    assert r.status_code == 409, "a job must be delivered before it can run"
    job_id = lab.poll(headers)[0]["job_id"]
    assert lab.status(headers, job_id, "awaiting_local_confirmation").json() == {
        "status": "awaiting_local_confirmation"
    }
    assert lab.status(headers, job_id, "running").json() == {"status": "running"}
    assert lab.status(headers, job_id, "running").status_code == 200  # idempotent repeat
    r = lab.status(headers, job_id, "awaiting_local_confirmation")
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert lab.status(headers, job_id, "succeeded").status_code == 422  # terminal states come via results
    assert lab.status(headers, job_id, "queued").status_code == 422


def test_result_requires_uploaded_events_then_is_idempotent(lab, dev, ev):
    headers, sid = dev
    rec = lab.job(sid)
    job_id = lab.poll(headers)[0]["job_id"]
    assert job_id == rec.job.job_id
    r = lab.result(headers, job_id, ["evt-000018"])
    assert (
        r.status_code == 409 and r.json()["error"] == "events_missing" and "evt-000018" in r.json()["detail"]
    )
    assert lab.store.get_job(job_id).status == "delivered"
    lab.upload(headers, sid, [ev("evt-000018", sid, "sim-01", 18)])
    r = lab.result(headers, job_id, ["evt-000018"])
    assert r.json() == {"status": "succeeded", "duplicate": False}
    before = lab.store.get_job(job_id)
    r = lab.result(headers, job_id, ["evt-000018"])
    assert r.json() == {"status": "succeeded", "duplicate": True}
    after = lab.store.get_job(job_id)
    assert after.updated_at == before.updated_at and after.result == before.result
    # same key with different content, or a second different result, is a conflict
    assert lab.result(headers, job_id, ["evt-000018"], outcome="failed").status_code == 409
    assert lab.result(headers, job_id, ["evt-000018"], key="res-other-key").status_code == 409
    # body/URL mismatch
    other = lab.job(sid)
    r = lab.c.post(
        f"/v1/jobs/{job_id}/result",
        headers=headers,
        json={
            "schema_version": "1.0",
            "job_id": other.job.job_id,
            "idempotency_key": "res-12345678",
            "outcome": "succeeded",
            "event_ids": [],
        },
    )
    assert r.status_code == 422


def test_unsupported_outcome_is_a_failed_job(lab, dev, ev):
    headers, sid = dev
    lab.job(sid, operation="ble_observe_notifications", arguments={"characteristic_id": "public_sensor"})
    job_id = lab.poll(headers)[0]["job_id"]
    lab.upload(headers, sid, [ev("evt-000001", sid, "sim-01", 1, status="unsupported")])
    r = lab.result(
        headers, job_id, ["evt-000001"], outcome="unsupported", error="not implemented in this build"
    )
    assert r.json()["status"] == "failed"
    assert lab.store.get_job(job_id).result.outcome.value == "unsupported"


def test_confirmation_gate_on_results(lab, dev, ev):
    headers, sid = dev
    rec = lab.job(
        sid,
        operation="ble_write_lab_value",
        arguments={"characteristic_id": "virtual_led", "value": "green", "restore": True},
    )
    job = lab.poll(headers)[0]
    lab.status(headers, job["job_id"], "awaiting_local_confirmation")
    lab.status(headers, job["job_id"], "running")
    lab.upload(headers, sid, [ev("evt-000031", sid, "sim-01", 31, operation="ble_write_lab_value")])
    r = lab.result(headers, job["job_id"], ["evt-000031"])
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    wrong = {"job_id": job["job_id"], "nonce": "n-forged", "approved": True}
    assert lab.result(headers, job["job_id"], ["evt-000031"], confirmation=wrong).status_code == 409
    ok = {"job_id": job["job_id"], "nonce": rec.job.nonce, "approved": True}
    r = lab.result(headers, job["job_id"], ["evt-000031"], confirmation=ok)
    assert r.json() == {"status": "succeeded", "duplicate": False}


def test_declined_confirmation_is_a_cancelled_job(lab, dev, ev):
    headers, sid = dev
    rec = lab.job(
        sid, operation="ble_write_lab_value", arguments={"characteristic_id": "virtual_led", "value": "red"}
    )
    job_id = lab.poll(headers)[0]["job_id"]
    lab.status(headers, job_id, "awaiting_local_confirmation")
    declined = {"job_id": job_id, "nonce": rec.job.nonce, "approved": False}
    r = lab.result(headers, job_id, [], outcome="cancelled", confirmation=declined)
    assert r.json()["status"] == "cancelled"


# --- expiry and cancellation ---
def test_expired_jobs_are_not_delivered(lab, dev):
    headers, sid = dev
    rec = lab.job(sid, deadline_s=5)
    lab.clock.advance(6)
    assert lab.poll(headers) == []
    assert lab.store.get_job(rec.job.job_id).status == "expired"
    assert lab.status(headers, rec.job.job_id, "running").status_code == 409


def test_expiry_sweep_of_in_flight_jobs(lab, dev, ev):
    headers, sid = dev
    read = lab.job(sid, deadline_s=30)
    lab.poll(headers)
    write = lab.job(
        sid,
        operation="ble_write_lab_value",
        deadline_s=30,
        arguments={"characteristic_id": "virtual_led", "value": "blue"},
    )
    lab.poll(headers)
    lab.status(headers, write.job.job_id, "awaiting_local_confirmation")
    lab.status(headers, write.job.job_id, "running")
    lab.clock.advance(31)
    assert lab.store.expire_jobs(lab.clock()) == 2
    assert lab.store.get_job(read.job.job_id).status == "expired"
    # a target-changing job that may have executed is never reported as merely expired
    assert lab.store.get_job(write.job.job_id).status == "unknown_outcome"
    # a late result is kept for audit, does not change the job, and is idempotent
    lab.upload(headers, sid, [ev("evt-000040", sid, "sim-01", 40)])
    r = lab.result(headers, read.job.job_id, ["evt-000040"])
    assert r.json() == {"status": "expired", "duplicate": False}
    r = lab.result(headers, read.job.job_id, ["evt-000040"])
    assert r.json() == {"status": "expired", "duplicate": True}
    audit = lab.store.get_job_audit(read.job.job_id)
    assert len(audit["late_results"]) == 1 and lab.store.get_job(read.job.job_id).result is None
    assert lab.store.expire_jobs(lab.clock()) == 0


def test_run_cancel_cancels_queued_jobs(lab, dev):
    headers, sid = dev
    run = lab.store.create_run(sid, "check things", {"name": "fake", "model": None, "is_fake": True})
    run.status = "running"
    lab.store.save_run(run)
    on_device = lab.job(sid, run_id=run.run_id)
    still_queued = lab.job(sid, run_id=run.run_id)
    lab.poll(headers)  # delivers the oldest one
    r = lab.c.post(f"/v1/runs/{run.run_id}/cancel", headers=headers)
    assert r.status_code == 200 and r.json() == {"status": "running"}  # active run: the worker finalises it
    assert lab.store.get_run(run.run_id).cancel_requested is True
    assert lab.store.get_job(on_device.job.job_id).status == "delivered"  # finishes per its own contract
    assert lab.store.get_job(still_queued.job.job_id).status == "cancelled"
    assert lab.poll(headers) == []
    with pytest.raises(Conflict) as e:
        lab.job(sid, run_id=run.run_id)
    assert e.value.code == "cancelled"
    # a stale save from the worker cannot clear the cancel request
    stale = lab.store.get_run(run.run_id)
    stale.cancel_requested = False
    lab.store.save_run(stale)
    assert lab.store.get_run(run.run_id).cancel_requested is True


def test_cancel_before_start_and_terminal_runs_stay_terminal(lab, dev):
    _, sid = dev
    run = lab.store.create_run(sid, "q", {"name": "fake", "model": None, "is_fake": True})
    r = lab.c.post(f"/v1/runs/{run.run_id}/cancel", headers=lab.owner)
    assert r.json() == {"status": "cancelled"}
    late = lab.store.get_run(run.run_id)
    late.status = "running"
    lab.store.save_run(late)
    assert lab.store.get_run(run.run_id).status == "cancelled"
