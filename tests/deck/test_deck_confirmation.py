"""Local confirmation (ADR-005): bound to the exact job id, nonce and authenticated device."""

from __future__ import annotations

import json

import pytest


def run_impactful(kit, key, slot=3, headers=None):
    r = kit.run(headers or kit.dev_a, slot, key)
    assert r.status_code == 200, r.text
    return r.json()


def test_impactful_run_waits_for_confirmation_and_executes_nothing(kit):
    j = run_impactful(kit, "key-conf-0001")
    assert j["state"] == "awaiting_confirmation" and j["requires_confirmation"] is True
    assert len(j["nonce"]) >= 16 and j["confirm_deadline"] > j["created_at"]
    assert j["steps"] == [
        {"index": 0, "type": "service_restart", "host_id": "h1", "service": "nginx", "outcome": "pending"}
    ]
    assert kit.drain() == 0 and kit.executor.calls == []


def test_approval_with_the_right_device_job_and_nonce_runs_it_once(kit):
    j = run_impactful(kit, "key-conf-0002")
    c = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert c.status_code == 200 and c.json()["state"] == "running" and "nonce" not in c.json()
    assert kit.executor.calls == []  # approval hands the job to the runner; the runner executes it
    kit.drain()
    done = kit.job(kit.dev_a, j["job_id"]).json()
    assert done["state"] == "succeeded" and done["steps"][0]["outcome"] == "succeeded"
    assert len(kit.executor.calls) == 1 and kit.executor.calls[0].service == "nginx"


def test_declining_cancels_without_executing(kit):
    j = kit.run_confirmed(kit.dev_a, 3, "key-conf-0003", approved=False)
    assert j["state"] == "cancelled" and j["reason"] == "declined" and kit.executor.calls == []


def test_wrong_nonce_is_rejected_and_the_job_stays_confirmable(kit):
    j = run_impactful(kit, "key-conf-0004")
    bad = kit.confirm(kit.dev_a, j["job_id"], "nonce-wrong-value-000")
    assert bad.status_code == 403 and bad.json()["error"] == "forbidden"
    assert j["nonce"] not in bad.text and "nonce-wrong" not in bad.text
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "awaiting_confirmation"
    assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"]).status_code == 200


def test_a_nonce_is_bound_to_its_own_job(kit):
    a = run_impactful(kit, "key-conf-0005", slot=3)
    b = run_impactful(kit, "key-conf-0006", slot=4)
    assert a["nonce"] != b["nonce"]
    swapped = kit.confirm(kit.dev_a, b["job_id"], a["nonce"])
    assert swapped.status_code == 403
    mismatched_body = kit.confirm(kit.dev_a, b["job_id"], b["nonce"], body_job_id=a["job_id"])
    assert mismatched_body.status_code == 422
    assert kit.job(kit.dev_a, b["job_id"]).json()["state"] == "awaiting_confirmation"
    assert kit.job(kit.dev_a, a["job_id"]).json()["state"] == "awaiting_confirmation"
    assert kit.drain() == 0 and kit.executor.calls == []


def test_another_device_cannot_confirm_even_with_the_nonce(kit, make_profile):
    kit.put_profile(make_profile(device_ids=["dev-a", "dev-b"]))
    j = run_impactful(kit, "key-conf-0007")
    stolen = kit.confirm(kit.dev_b, j["job_id"], j["nonce"])
    assert stolen.status_code == 404 and stolen.json()["error"] == "not_found"
    assert kit.job(kit.dev_b, j["job_id"]).status_code == 404  # and cannot read the nonce either
    assert kit.cancel(kit.dev_b, j["job_id"]).status_code == 404
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "awaiting_confirmation"
    # the owner token is not a confirmation either
    assert kit.confirm(kit.owner, j["job_id"], j["nonce"]).status_code == 403
    assert kit.drain() == 0 and kit.executor.calls == []


def test_a_late_confirmation_cannot_start_an_expired_job(kit):
    j = run_impactful(kit, "key-conf-0008")
    kit.clock.advance(120)  # exactly at the deadline
    late = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert late.status_code == 409 and late.json()["error"] == "expired"
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "expired"
    assert kit.drain() == 0 and kit.executor.calls == []
    again = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert again.status_code == 409 and again.json()["error"] == "expired"
    assert kit.drain() == 0 and kit.executor.calls == []


def test_confirming_just_inside_the_window_works(kit):
    j = run_impactful(kit, "key-conf-0009")
    kit.clock.advance(119.9)
    assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"]).status_code == 200


def test_an_already_decided_job_cannot_be_confirmed_again(kit):
    j = run_impactful(kit, "key-conf-0010")
    assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"]).status_code == 200
    second = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert second.status_code == 409 and second.json()["error"] == "already_decided"
    kit.drain()
    third = kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    assert third.status_code == 409
    assert len(kit.executor.calls) == 1
    flipped = kit.confirm(kit.dev_a, j["job_id"], j["nonce"], approved=False)  # a decline cannot undo it
    assert flipped.status_code == 409


def test_a_cancelled_job_cannot_be_confirmed(kit):
    j = run_impactful(kit, "key-conf-0011")
    kit.cancel(kit.dev_a, j["job_id"])
    assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"]).status_code == 409
    assert kit.drain() == 0 and kit.executor.calls == []


def test_the_nonce_is_single_use_and_hidden_once_decided(kit):
    j = run_impactful(kit, "key-conf-0012")
    assert "nonce" in kit.job(kit.dev_a, j["job_id"]).json()
    kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    with kit.runtime.store.tx() as conn:
        assert conn.execute("SELECT nonce FROM jobs WHERE job_id=?", (j["job_id"],)).fetchone()[0] is None
    assert "nonce" not in kit.job(kit.dev_a, j["job_id"]).json()


def test_the_owner_never_sees_a_nonce(kit):
    j = run_impactful(kit, "key-conf-0013")
    assert j["nonce"] not in kit.job(kit.owner, j["job_id"]).text
    assert j["nonce"] not in kit.client.get("/v1/deck/jobs", headers=kit.owner).text
    assert j["nonce"] not in kit.client.get("/v1/deck/audit", headers=kit.owner).text


def test_a_confirmation_carried_in_the_run_request_is_ignored(kit):
    claims = {
        "confirmed": True,
        "approved": True,
        "nonce": "anything-at-all",
        "confirmation": {"job_id": "x", "nonce": "y", "approved": True},
        "requires_confirmation": False,
        "impactful": False,
    }
    r = kit.run(kit.dev_a, 3, "key-conf-0014", json=claims)
    assert r.status_code == 200 and r.json()["state"] == "awaiting_confirmation"
    assert kit.drain() == 0 and kit.executor.calls == []


def test_confirm_requests_reject_unknown_flags_and_non_boolean_approvals(kit):
    j = run_impactful(kit, "key-conf-0015")
    for extra in ({"force": True}, {"skip_confirmation": True}, {"requires_local_confirmation": False}):
        assert kit.confirm(kit.dev_a, j["job_id"], j["nonce"], **extra).status_code == 422
    for approved in ("yes", 1, "true", None):
        r = kit.client.post(
            f"/v1/deck/jobs/{j['job_id']}/confirm",
            headers=kit.dev_a,
            json={"job_id": j["job_id"], "nonce": j["nonce"], "approved": approved},
        )
        assert r.status_code == 422
    for body in ({}, {"job_id": j["job_id"], "nonce": j["nonce"]}, {"job_id": j["job_id"], "approved": True}):
        assert (
            kit.client.post(f"/v1/deck/jobs/{j['job_id']}/confirm", headers=kit.dev_a, json=body).status_code
            == 422
        )
    assert (
        kit.client.post(f"/v1/deck/jobs/{j['job_id']}/confirm", headers=kit.dev_a, content=b"x").status_code
        == 422
    )
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "awaiting_confirmation"
    assert kit.drain() == 0 and kit.executor.calls == []


def test_a_stored_flag_cannot_lower_the_requirement(kit):
    """Neither a profile nor a stored row can carry a flag that skips confirmation: the requirement is a
    class constant of the action type."""
    p = json.loads(kit.client.get("/v1/deck/profiles/dev", headers=kit.owner).text)
    p["slots"]["3"]["actions"][0]["impactful"] = False
    r = kit.client.put("/v1/deck/profiles/dev", headers=kit.owner, json=p)
    assert r.status_code == 422
    with kit.runtime.store.tx() as conn:  # the same flag planted directly in the stored profile
        body = json.loads(conn.execute("SELECT body FROM profiles WHERE profile_id='dev'").fetchone()[0])
        body["slots"]["3"]["actions"][0]["impactful"] = False
        conn.execute("UPDATE profiles SET body=?", (json.dumps(body),))
    refused = kit.run(kit.dev_a, 3, "key-conf-0016")
    assert refused.status_code == 409 and refused.json()["error"] == "profile_invalid"
    assert kit.drain() == 0 and kit.executor.calls == []


@pytest.mark.parametrize(
    "slot,impactful", [(1, False), (2, False), (3, True), (4, True), (5, True), (6, False)]
)
def test_requirement_comes_from_the_action_types_in_the_slot(kit, slot, impactful):
    j = kit.run(kit.dev_a, slot, f"key-conf-slot{slot}").json()
    assert j["requires_confirmation"] is impactful
    assert (j["state"] == "awaiting_confirmation") is impactful
    assert ("nonce" in j) is impactful


def test_a_slot_with_any_impactful_step_needs_one_confirmation_for_the_whole_job(kit):
    j = run_impactful(kit, "key-conf-0017", slot=5)
    assert kit.drain() == 0 and kit.executor.calls == []  # even the read-only first step waits
    kit.confirm(kit.dev_a, j["job_id"], j["nonce"])
    kit.drain()
    assert [type(c).__name__ for c in kit.executor.calls] == ["ServiceStatus", "ServiceRestart"]


def test_confirming_a_non_impactful_job_is_refused(kit):
    j = kit.run(kit.dev_a, 1, "key-conf-0018").json()
    r = kit.confirm(kit.dev_a, j["job_id"], "nonce-not-applicable")
    assert r.status_code == 409 and r.json()["error"] == "already_decided"
    kit.drain()
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "succeeded"


def test_confirming_an_unknown_job_is_a_404(kit):
    assert kit.confirm(kit.dev_a, "dj-doesnotexist", "nonce-whatever-000").status_code == 404


def test_confirm_attempts_are_rate_limited(kit):
    j = run_impactful(kit, "key-conf-0019")
    codes = [kit.confirm(kit.dev_a, j["job_id"], f"wrong-nonce-{i:04d}").status_code for i in range(62)]
    assert codes[:60] == [403] * 60 and codes[60:] == [429, 429]
    assert kit.job(kit.dev_a, j["job_id"]).json()["state"] == "awaiting_confirmation"


def test_confirm_is_refused_with_a_busy_queue_and_the_job_stays_confirmable(kit_factory):
    from oyster.deck.jobs import DeckConfig

    kit = kit_factory(DeckConfig(queue_size=1))
    dev = kit.enroll("dev-a")
    kit.put_profile()
    j = run_impactful(kit, "key-conf-0020", headers=dev)
    assert kit.run(dev, 1, "key-conf-0021").status_code == 200  # fills the one-slot queue
    busy = kit.confirm(dev, j["job_id"], j["nonce"])
    assert busy.status_code == 503 and busy.json()["error"] == "busy"
    assert kit.job(dev, j["job_id"]).json()["state"] == "awaiting_confirmation"
    kit.drain()
    assert kit.confirm(dev, j["job_id"], j["nonce"]).status_code == 200
