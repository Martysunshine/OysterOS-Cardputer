"""Profiles: owner-defined, validated, bound to devices, slots 1..9 only."""

from __future__ import annotations

import copy
import json

import pytest
from oyster.deck.jobs import DeckConfig


def put(kit, profile, expect=422):
    r = kit.client.put(f"/v1/deck/profiles/{profile['profile_id']}", headers=kit.owner, json=profile)
    assert r.status_code == expect, r.text
    return r


def test_define_get_list_and_delete_roundtrip(kit, make_profile):
    r = kit.client.get("/v1/deck/profiles/dev", headers=kit.owner)
    assert r.status_code == 200
    body = r.json()
    assert body["profile_id"] == "dev" and sorted(body["slots"]) == ["1", "2", "3", "4", "5", "6"]
    # a single `action` is stored as a one-element `actions` list
    assert body["slots"]["1"]["actions"] == [{"type": "noop"}]
    again = put(kit, make_profile(), 200)
    assert again.json()["result"] == "unchanged"
    listed = kit.client.get("/v1/deck/profiles", headers=kit.owner).json()
    assert [p["profile_id"] for p in listed["profiles"]] == ["dev"]
    assert kit.client.delete("/v1/deck/profiles/dev", headers=kit.owner).status_code == 200
    assert kit.client.get("/v1/deck/profiles/dev", headers=kit.owner).status_code == 404
    assert kit.client.delete("/v1/deck/profiles/dev", headers=kit.owner).status_code == 404


def test_device_sees_only_profiles_bound_to_it_in_compact_form(kit, make_profile):
    put(kit, make_profile(profile_id="other", name="Other", device_ids=["dev-b"]), 200)
    a = kit.client.get("/v1/deck/profiles", headers=kit.dev_a).json()
    b = kit.client.get("/v1/deck/profiles", headers=kit.dev_b).json()
    assert [p["profile_id"] for p in a["profiles"]] == ["dev"]
    assert [p["profile_id"] for p in b["profiles"]] == ["other"]
    slot3 = a["profiles"][0]["slots"][2]
    assert slot3 == {"slot": 3, "label": "Restart nginx", "impactful": True}
    assert "hosts" not in a["profiles"][0] and "device_ids" not in a["profiles"][0]
    assert "nginx" not in json.dumps(a["profiles"][0]["slots"][0])  # no action arguments on the device view


@pytest.mark.parametrize("slot_key", ["0", "10", "-1", "a", "", "01", "1.0", "99"])
def test_slot_numbers_outside_1_to_9_are_rejected_at_definition(kit, make_profile, slot_key):
    p = make_profile()
    p["slots"][slot_key] = {"label": "x", "action": {"type": "noop"}}
    put(kit, p)


def test_slots_1_and_9_are_accepted_and_runnable(kit, make_profile):
    p = make_profile()
    p["slots"] = {
        "1": {"label": "first", "action": {"type": "noop"}},
        "9": {"label": "last", "action": {"type": "noop"}},
    }
    put(kit, p, 200)
    assert kit.run(kit.dev_a, 1, "key-slot-01").status_code == 200
    assert kit.run(kit.dev_a, 9, "key-slot-09").status_code == 200
    assert kit.run(kit.dev_a, 2, "key-slot-02").status_code == 404  # defined profile, empty slot


@pytest.mark.parametrize("n", [0, 10, -1, 100])
def test_running_slot_0_or_10_is_rejected(kit, n):
    r = kit.run(kit.dev_a, n, "key-range-001")
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"
    assert kit.drain() == 0 and kit.executor.calls == []


def test_non_numeric_slot_in_the_path_is_rejected(kit):
    r = kit.client.post(
        "/v1/deck/profiles/dev/slots/x/run", headers={**kit.dev_a, "Idempotency-Key": "key-range-002"}
    )
    assert r.status_code == 422


@pytest.mark.parametrize("label", ["", "x" * 25, "tab\there", "new\nline", "café", "\x00"])
def test_slot_labels_are_bounded_printable_ascii(kit, make_profile, label):
    p = make_profile()
    p["slots"]["1"]["label"] = label
    put(kit, p)


def test_label_limits_are_inclusive(kit, make_profile):
    p = make_profile()
    p["slots"]["1"]["label"] = "x" * 24
    p["name"] = "n" * 24
    put(kit, p, 200)


def test_a_slot_holds_one_action_or_an_ordered_list_of_at_most_three(kit, make_profile):
    p = make_profile()
    step = {"type": "noop"}
    p["slots"]["1"] = {"label": "three", "actions": [step, step, step]}
    put(kit, p, 200)
    p["slots"]["1"] = {"label": "four", "actions": [step, step, step, step]}
    put(kit, p)
    p["slots"]["1"] = {"label": "none", "actions": []}
    put(kit, p)
    p["slots"]["1"] = {"label": "both", "action": step, "actions": [step]}
    put(kit, p)
    p["slots"]["1"] = {"label": "neither"}
    put(kit, p)


def test_actions_keep_their_order(kit):
    r = kit.client.get("/v1/deck/profiles/dev", headers=kit.owner).json()
    assert [a["type"] for a in r["slots"]["5"]["actions"]] == ["service_status", "service_restart"]
    j = kit.run_confirmed(kit.dev_a, 5, "key-order-001")
    assert [s["type"] for s in j["steps"]] == ["service_status", "service_restart"]
    assert [type(c).__name__ for c in kit.executor.calls] == ["ServiceStatus", "ServiceRestart"]


def test_actions_may_only_reference_listed_hosts_and_services(kit, make_profile):
    p = make_profile()
    p["slots"]["3"]["action"] = {"type": "service_restart", "host_id": "unlisted", "service": "nginx"}
    put(kit, p)
    p = make_profile()
    p["slots"]["3"]["action"] = {"type": "service_restart", "host_id": "h1", "service": "postgres"}
    put(kit, p)
    p = make_profile()
    p["slots"]["4"]["action"] = {"type": "wake_on_lan", "host_id": "h3"}
    put(kit, p)
    p = make_profile()
    p["slots"]["3"]["action"] = {
        "type": "service_restart",
        "host_id": "h2",
        "service": "nginx",
    }  # h2: no services
    put(kit, p)


def test_structural_limits(kit, make_profile):
    base = make_profile()
    cases = []
    p = copy.deepcopy(base)
    p["device_ids"] = []
    cases.append(p)
    p = copy.deepcopy(base)
    p["device_ids"] = ["dev-a", "dev-a"]
    cases.append(p)
    p = copy.deepcopy(base)
    p["device_ids"] = [f"d{i}" for i in range(17)]
    cases.append(p)
    p = copy.deepcopy(base)
    p["device_ids"] = ["bad id"]
    cases.append(p)
    p = copy.deepcopy(base)
    p["hosts"] = {f"h{i}": {"services": []} for i in range(9)}
    cases.append(p)
    p = copy.deepcopy(base)
    p["hosts"]["h1"]["services"] = [f"s{i}" for i in range(17)]
    cases.append(p)
    p = copy.deepcopy(base)
    p["slots"] = {}
    cases.append(p)
    p = copy.deepcopy(base)
    p["extra"] = 1
    cases.append(p)
    p = copy.deepcopy(base)
    p["profile_id"] = "bad id"
    for case in cases:
        put(kit, case)
    r = kit.client.put("/v1/deck/profiles/bad%20id", headers=kit.owner, json=p)
    assert r.status_code == 422


def test_profile_id_in_body_must_match_the_url(kit, make_profile):
    r = kit.client.put("/v1/deck/profiles/other", headers=kit.owner, json=make_profile())
    assert r.status_code == 422


def test_body_must_be_a_json_object_and_bounded(kit):
    assert kit.client.put("/v1/deck/profiles/dev", headers=kit.owner, json=[1]).status_code == 422
    assert kit.client.put("/v1/deck/profiles/dev", headers=kit.owner, content=b"{nope").status_code == 422
    big = kit.client.put("/v1/deck/profiles/dev", headers=kit.owner, content=b'{"a":"' + b"x" * 17000 + b'"}')
    assert big.status_code == 413


def test_profile_count_is_bounded(kit_factory, make_profile):
    kit = kit_factory(DeckConfig(max_profiles=2))
    for i in range(2):
        kit.put_profile(make_profile(profile_id=f"p{i}"))
    r = kit.put_profile(make_profile(profile_id="p9"), expect=409)
    assert r.json()["error"] == "limit_reached"
    kit.put_profile(make_profile(profile_id="p0"))  # replacing an existing one is fine


def test_device_profile_list_is_paginated_and_stays_under_4_kib(kit, make_profile):
    full = make_profile()
    full["slots"] = {str(i): {"label": "L" * 24, "action": {"type": "noop"}} for i in range(1, 10)}
    for i in range(10):
        put(kit, {**full, "profile_id": "p" * 30 + f"{i:02d}", "name": "N" * 24}, 200)
    seen, after = [], None
    for _ in range(10):
        params = {"limit": 5} if after is None else {"limit": 5, "after": after}
        r = kit.client.get("/v1/deck/profiles", headers=kit.dev_a, params=params)
        assert r.status_code == 200 and len(r.content) < 4096
        seen += [p["profile_id"] for p in r.json()["profiles"]]
        after = r.json()["next_after"]
        if after is None:
            break
    assert len(seen) == 11 and seen == sorted(seen) and len(set(seen)) == 11
    capped = kit.client.get("/v1/deck/profiles", headers=kit.dev_a, params={"limit": 20}).json()
    assert len(capped["profiles"]) == 5 and capped["next_after"] is not None  # the page size is capped


def test_replacing_a_profile_cancels_unstarted_jobs_but_not_running_ones(kit, make_profile):
    waiting = kit.run(kit.dev_a, 3, "key-repl-0001").json()  # awaiting confirmation
    queued = kit.run(kit.dev_a, 1, "key-repl-0002").json()  # queued for the runner
    approved = kit.run(kit.dev_a, 4, "key-repl-0003").json()
    assert kit.confirm(kit.dev_a, approved["job_id"], approved["nonce"]).json()["state"] == "running"
    changed = make_profile()
    changed["slots"]["3"]["label"] = "Renamed"
    assert put(kit, changed, 200).json()["result"] == "updated"
    assert kit.job(kit.dev_a, waiting["job_id"]).json()["state"] == "cancelled"
    assert kit.job(kit.dev_a, waiting["job_id"]).json()["reason"] == "profile_changed"
    assert kit.job(kit.dev_a, queued["job_id"]).json()["state"] == "cancelled"
    assert kit.job(kit.dev_a, approved["job_id"]).json()["state"] == "running"
    kit.drain()
    assert [type(c).__name__ for c in kit.executor.calls] == ["WakeOnLan"]  # only the approved job ran
    # the old nonce is useless now
    assert kit.confirm(kit.dev_a, waiting["job_id"], waiting["nonce"]).status_code == 409


def test_deleting_a_profile_cancels_its_unstarted_jobs(kit):
    waiting = kit.run(kit.dev_a, 3, "key-del-00001").json()
    assert kit.client.delete("/v1/deck/profiles/dev", headers=kit.owner).status_code == 200
    j = kit.job(kit.dev_a, waiting["job_id"]).json()
    assert j["state"] == "cancelled" and j["reason"] == "profile_deleted"
    assert kit.run(kit.dev_a, 1, "key-del-00002").status_code == 404
    assert kit.drain() == 0 and kit.executor.calls == []


def test_removing_a_device_binding_stops_it_running_the_profile(kit, make_profile):
    assert kit.run(kit.dev_a, 1, "key-bind-0001").status_code == 200
    put(kit, make_profile(device_ids=["dev-b"]), 200)
    r = kit.run(kit.dev_a, 1, "key-bind-0002")
    assert r.status_code == 404
    assert kit.run(kit.dev_b, 1, "key-bind-0003").status_code == 200
