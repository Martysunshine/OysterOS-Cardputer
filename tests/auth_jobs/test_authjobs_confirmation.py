"""Local-confirmation rules on the backend side (ADR-005, docs/API.md 'Job lifecycle'):
- ble_check_access_policy is gated when the selected assertion's operation is write/transfer;
- only executed outcomes of a gated job must carry the approved confirmation;
- expire_job/cancel_job end a job that has not started, and a late `running` report is then refused."""

from __future__ import annotations

import pytest
from oyster.policy import catalogue
from oyster.store import Conflict, NotFound

PROFILE = catalogue.load_profile("oyster-lab-v1")
WRITE = {"characteristic_id": "virtual_led", "value": "green", "restore": True}


@pytest.fixture
def dev(lab):
    lab.enroll_target()
    headers = lab.enroll("sim-01")
    sid = lab.session(headers)
    return headers, sid


def _gated(lab, sid):
    return lab.job(sid, operation="ble_write_lab_value", arguments=WRITE)


def _start(lab, headers, job_id):
    """Device-side start of a gated job: awaiting_local_confirmation, then running."""
    assert lab.status(headers, job_id, "awaiting_local_confirmation").status_code == 200
    assert lab.status(headers, job_id, "running").status_code == 200


# --- assertion-derived confirmation ---
@pytest.mark.parametrize("assertion", PROFILE["assertions"], ids=lambda a: a["test_id"])
def test_access_policy_job_confirmation_follows_the_assertion_operation(lab, dev, assertion):
    headers, sid = dev
    rec = lab.job(
        sid, operation="ble_check_access_policy", arguments={"test_id": assertion["test_id"]}, confirm=False
    )
    gated = assertion["operation"] in ("write", "transfer")
    assert rec.job.requires_local_confirmation is gated
    assert bool(rec.job.nonce) is gated
    delivered = lab.poll(headers)[0]
    assert delivered["requires_local_confirmation"] is gated


def test_catalogue_confirmation_for_unknown_assertion_or_profile_is_conservative():
    op = "ble_check_access_policy"
    public = {"test_id": "public_sensor_is_public"}
    assert catalogue.job_requires_local_confirmation(op, {"test_id": "no_such_test"}, PROFILE) is True
    assert catalogue.job_requires_local_confirmation(op, public, None) is True
    assert catalogue.job_requires_local_confirmation(op, public, PROFILE) is False
    assert catalogue.job_requires_local_confirmation("ble_write_lab_value", WRITE, None) is True
    assert catalogue.job_requires_local_confirmation("wifi_survey", {}, None) is False
    assert catalogue.changes_target_state(op) is True


# --- result confirmation rule (item: device rejections carry no confirmation) ---
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "expired", "unsupported"])
def test_not_executed_outcomes_of_a_gated_job_need_no_confirmation(lab, dev, outcome):
    headers, sid = dev
    rec = _gated(lab, sid)
    lab.poll(headers)
    r = lab.result(headers, rec.job.job_id, [], outcome=outcome, error="rejected by local policy")
    assert r.status_code == 200, r.text
    assert lab.store.get_job(rec.job.job_id).result.outcome.value == outcome


@pytest.mark.parametrize("outcome", ["succeeded", "unknown_outcome"])
def test_executed_outcomes_of_a_gated_job_need_the_approved_confirmation(lab, dev, ev, outcome):
    headers, sid = dev
    rec = _gated(lab, sid)
    job_id = lab.poll(headers)[0]["job_id"]
    _start(lab, headers, job_id)
    lab.upload(headers, sid, [ev("evt-000050", sid, "sim-01", 50, operation="ble_write_lab_value")])
    r = lab.result(headers, job_id, ["evt-000050"], outcome=outcome)
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    declined = {"job_id": job_id, "nonce": rec.job.nonce, "approved": False}
    r = lab.result(headers, job_id, ["evt-000050"], outcome=outcome, confirmation=declined)
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    forged = {"job_id": job_id, "nonce": "n-forged", "approved": True}
    r = lab.result(headers, job_id, ["evt-000050"], outcome=outcome, confirmation=forged)
    assert r.status_code == 409
    ok = {"job_id": job_id, "nonce": rec.job.nonce, "approved": True}
    r = lab.result(headers, job_id, ["evt-000050"], outcome=outcome, confirmation=ok)
    assert r.status_code == 200 and r.json()["status"] == outcome


# --- expire_job / cancel_job ---
def test_late_approval_after_expiry_is_refused(lab, dev):
    headers, sid = dev
    rec = _gated(lab, sid)
    job_id = lab.poll(headers)[0]["job_id"]
    assert lab.status(headers, job_id, "awaiting_local_confirmation").status_code == 200
    ended = lab.store.expire_job(job_id, "confirmation_timeout")
    assert ended.status == "expired"
    audit = lab.store.get_job_audit(job_id)
    assert audit["status_reason"] == "confirmation_timeout"
    r = lab.status(headers, job_id, "running")
    assert r.status_code == 409 and "must not be executed" in r.json()["detail"]
    assert lab.store.get_job(job_id).status == "expired"
    # a device that then reports it did not run the job (no confirmation needed) is kept for audit only
    r = lab.result(headers, job_id, [], outcome="cancelled", error="running report refused")
    assert r.status_code == 200 and r.json() == {"status": "expired", "duplicate": False}
    assert lab.store.get_job(job_id).result is None
    assert len(lab.store.get_job_audit(job_id)["late_results"]) == 1
    assert rec.job.requires_local_confirmation


def test_expire_and_cancel_only_end_jobs_that_cannot_have_started(lab, dev):
    headers, sid = dev
    queued = lab.job(sid)
    assert lab.store.expire_job(queued.job.job_id, "confirmation_timeout").status == "expired"
    gated = _gated(lab, sid)
    lab.poll(headers)  # delivered, gated: the device cannot run it before `running` is accepted
    assert lab.store.cancel_job(gated.job.job_id, "run_ended").status == "cancelled"
    assert lab.store.get_job_audit(gated.job.job_id)["status_reason"] == "run_ended"
    read = lab.job(sid)
    lab.poll(headers)  # delivered, not gated: its status report is advisory, it may be running
    assert lab.store.expire_job(read.job.job_id, "confirmation_timeout").status == "delivered"
    running = _gated(lab, sid)
    lab.poll(headers)
    _start(lab, headers, running.job.job_id)
    assert lab.store.expire_job(running.job.job_id, "confirmation_timeout").status == "running"
    assert lab.store.cancel_job(running.job.job_id, "run_ended").status == "running"
    # terminal jobs stay as they are
    assert lab.store.cancel_job(queued.job.job_id, "run_ended").status == "expired"
    with pytest.raises(NotFound):
        lab.store.expire_job("job-unknown", "confirmation_timeout")
    with pytest.raises(Conflict) as e:
        lab.store.expire_job(read.job.job_id, "Not A Reason!")
    assert e.value.code == "validation_failed"
