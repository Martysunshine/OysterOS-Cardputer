"""The approval flow end to end through the HTTP API: hook long poll, device list, device decision."""

from __future__ import annotations


def test_a_device_allow_ends_the_long_poll_with_allow(kit):
    w = kit.hook(summary="npm test")
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    assert item["tool"] == "Bash" and item["summary"] == "npm test"
    assert item["expires_in_s"] == 90 and item["age_s"] == 0
    r = kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "allow")
    assert r.status_code == 200, r.text
    assert r.json() == {"approval_id": item["approval_id"], "state": "allowed", "decision": "allow"}
    out = w.result()
    assert out.status_code == 200
    assert out.json() == {"decision": "allow", "approval_id": item["approval_id"]}


def test_a_device_deny_ends_the_long_poll_with_deny(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    assert kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "deny").status_code == 200
    assert w.result().json()["decision"] == "deny"


def test_nobody_answering_ends_with_timeout_and_never_allow(kit):
    w = kit.hook(wait_s=30)
    kit.wait_for_pending()
    kit.clock.advance(31)
    out = w.result()
    assert out.status_code == 200 and out.json()["decision"] == "timeout"
    assert kit.listing(kit.dev_a) == []


def test_the_event_trail_of_an_allowed_approval(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "allow")
    w.result()
    assert kit.events(item["approval_id"]) == ["approval.created", "approval.allowed"]
