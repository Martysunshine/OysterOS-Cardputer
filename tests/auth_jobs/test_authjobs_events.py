"""Evidence upload: schema validation, size limit, idempotency without duplicate side effects."""

from __future__ import annotations


def test_oversize_payload_rejected_with_413(lab, ev):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    big = ev("evt-000001", sid, "dev-a", 1, details={"profile_id": "oyster-lab-v1", "pad": "x" * 5000})
    r = lab.upload(a, sid, [big])
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"
    assert lab.store.count_events(sid) == 0
    r = lab.c.post(f"/v1/sessions/{sid}/assistant/messages", headers=a, json={"question": "q" * 5000})
    assert r.status_code == 413


def test_events_are_schema_validated_and_bound_to_url_and_token(lab, ev):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    bad = ev("evt-000001", sid, "dev-a", 1)
    bad["status"] = "pwned"
    r = lab.upload(a, sid, [bad])
    assert (
        r.status_code == 422 and r.json()["error"] == "validation_failed" and "events/0" in r.json()["detail"]
    )
    wrong_session = ev("evt-000001", "sess-other", "dev-a", 1)
    assert lab.upload(a, sid, [wrong_session]).status_code == 422
    wrong_device = ev("evt-000001", sid, "dev-zzz", 1)
    assert lab.upload(a, sid, [wrong_device]).status_code == 422
    live_label = ev("evt-000001", sid, "dev-a", 1, source_mode="live")
    r = lab.upload(a, sid, [live_label])
    assert r.status_code == 422, "a simulated session only accepts simulated events"
    bad_time = ev("evt-000001", sid, "dev-a", 1)
    bad_time["timestamp_utc"] = "yesterday"
    assert lab.upload(a, sid, [bad_time]).status_code == 422
    assert lab.store.count_events(sid) == 0


def test_live_session_rejects_simulated_events(lab, ev):
    live = lab.enroll("cardputer-01", simulated=False)
    sid = lab.session(live, mode="live")
    r = lab.upload(live, sid, [ev("evt-000001", sid, "cardputer-01", 1, source_mode="simulated")])
    assert r.status_code == 422
    r = lab.upload(live, sid, [ev("evt-000001", sid, "cardputer-01", 1, source_mode="offline")])
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_duplicate_events_are_idempotent_and_conflicts_atomic(lab, ev):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    batch = [ev("evt-000001", sid, "dev-a", 1), ev("evt-000002", sid, "dev-a", 2)]
    r = lab.upload(a, sid, batch)
    assert r.json() == {"accepted": 2, "duplicates": 0, "acked_through": 2}
    r = lab.upload(a, sid, batch)
    assert r.json() == {"accepted": 0, "duplicates": 2, "acked_through": 2}
    assert lab.store.count_events(sid) == 2
    # a conflicting duplicate rejects the whole batch: the new event in it is not stored either
    changed = ev("evt-000002", sid, "dev-a", 2, status="denied")
    r = lab.upload(a, sid, [ev("evt-000003", sid, "dev-a", 3), changed])
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert lab.store.count_events(sid) == 2
    assert lab.store.missing_event_ids(sid, ["evt-000003"]) == ["evt-000003"]
    # same event id in another session is a different event
    sid2 = lab.session(a)
    assert lab.upload(a, sid2, [ev("evt-000001", sid2, "dev-a", 1)]).json()["accepted"] == 1


def test_batch_size_is_bounded(lab, ev):
    a = lab.enroll("dev-a")
    sid = lab.session(a)
    tiny = [{"x": i} for i in range(65)]
    r = lab.upload(a, sid, tiny)
    assert r.status_code == 422
