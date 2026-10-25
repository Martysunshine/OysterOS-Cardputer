"""The `desk` seed profile: shape, idempotency and how it behaves through the API with the mock executor."""

from __future__ import annotations

import pytest
from oyster.deck.actions import MockExecutor, parse_action
from oyster.deck.profiles import ProfileInvalid
from oyster.deck.seed import DESK_PROFILE_ID, desk_profile, ensure_desk_profile

EXPECTED = {
    1: ("PC health", "host_health_check", False),
    2: ("Play/Pause", "media_key", False),
    3: ("Next", "media_key", False),
    4: ("Prev", "media_key", False),
    5: ("Vol+", "media_key", False),
    6: ("Vol-", "media_key", False),
    7: ("Mute", "media_key", False),
    8: ("Lock PC", "lock_workstation", True),
}
KEYS = {2: "play_pause", 3: "next", 4: "prev", 5: "vol_up", 6: "vol_down", 7: "mute"}


def test_the_desk_profile_has_the_documented_slots():
    p = desk_profile(["dev-a"])
    assert p.profile_id == DESK_PROFILE_ID == "desk" and p.name == "Desk" and len(p.name) <= 24
    assert p.device_ids == ["dev-a"] and list(p.hosts) == ["this-pc"]
    assert sorted(int(k) for k in p.slots) == list(EXPECTED)  # slot 9 stays free
    for n, (label, kind, impactful) in EXPECTED.items():
        slot = p.slot(n)
        assert slot.label == label and len(label) <= 24
        assert [a.type for a in slot.actions] == [kind] and slot.impactful is impactful
        assert slot.actions[0].host_id == "this-pc"
    for n, key in KEYS.items():
        assert p.slot(n).actions[0].key == key


def test_only_locking_needs_a_confirmation():
    p = desk_profile(["dev-a"])
    assert [n for n in EXPECTED if p.slot(n).impactful] == [8]
    summary = p.summary()
    assert [(s["slot"], s["impactful"]) for s in summary["slots"] if s["impactful"]] == [(8, True)]


def test_every_seeded_action_passes_the_allowlist():
    for slot in desk_profile(["dev-a"]).slots.values():
        for action in slot.actions:
            assert parse_action(action.model_dump(mode="json")) == action


def test_the_local_host_id_and_the_devices_are_parameters():
    p = desk_profile(["dev-a", "dev-b", "dev-a"], local_host_id="desk-pc")  # duplicates are folded
    assert p.device_ids == ["dev-a", "dev-b"] and list(p.hosts) == ["desk-pc"]
    assert {a.host_id for s in p.slots.values() for a in s.actions} == {"desk-pc"}


@pytest.mark.parametrize("devices", [[], [""], ["has space"], ["x" * 41]])
def test_invalid_devices_are_rejected(devices):
    with pytest.raises(ProfileInvalid):
        desk_profile(devices)


def test_seeding_is_idempotent(kit):
    service = kit.service
    assert ensure_desk_profile(service, ["dev-a"]) == "created"
    assert ensure_desk_profile(service, ["dev-a"]) == "unchanged"
    assert ensure_desk_profile(kit.runtime, ["dev-a"]) == "unchanged"  # a runtime works as well as a service
    with kit.runtime.store.tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM profiles WHERE profile_id='desk'").fetchone()[0] == 1
    desk_events = [row[5] for row in kit.audit_rows() if row[8] == "desk"]
    assert desk_events == ["profile.put"] * 3  # each call goes through the audited service path


def test_seeding_with_other_devices_updates_the_profile_and_cancels_unstarted_jobs(kit):
    ensure_desk_profile(kit.service, ["dev-a"])
    r = kit.run(kit.dev_a, 8, "key-seed-00001", profile="desk")
    assert r.json()["state"] == "awaiting_confirmation"
    assert ensure_desk_profile(kit.service, ["dev-a", "dev-b"]) == "updated"
    assert kit.job(kit.dev_a, r.json()["job_id"]).json()["state"] == "cancelled"
    assert (
        kit.client.get("/v1/deck/profiles", headers=kit.dev_b).json()["profiles"][0]["profile_id"] == "desk"
    )


def test_the_device_sees_the_seeded_profile_in_the_compact_form(kit):
    ensure_desk_profile(kit.service, ["dev-a"])
    body = kit.client.get("/v1/deck/profiles", headers=kit.dev_a).json()
    desk = next(p for p in body["profiles"] if p["profile_id"] == "desk")
    assert desk["name"] == "Desk"
    assert [s["label"] for s in desk["slots"]] == [v[0] for v in EXPECTED.values()]
    assert "host_id" not in str(desk) and "this-pc" not in str(desk)  # no host or action detail on the device
    assert len(kit.client.get("/v1/deck/profiles", headers=kit.dev_a).content) < 4096


def test_the_seeded_slots_run_through_the_api_with_the_mock_executor(kit):
    ensure_desk_profile(kit.service, ["dev-a"])
    assert isinstance(kit.executor, MockExecutor)
    r = kit.run(kit.dev_a, 2, "key-seed-media-1", profile="desk")
    assert r.status_code == 200 and r.json()["requires_confirmation"] is False
    kit.drain()
    j = kit.job(kit.dev_a, r.json()["job_id"]).json()
    assert j["state"] == "succeeded" and j["executor"] == "mock"
    assert [type(c).__name__ for c in kit.executor.calls] == ["MediaKey"]
    assert kit.executor.calls[0].key == "play_pause"
    locked = kit.run(kit.dev_a, 8, "key-seed-lock-01", profile="desk").json()
    assert locked["state"] == "awaiting_confirmation" and locked["requires_confirmation"] is True
    assert len(kit.executor.calls) == 1  # nothing ran for the lock before its confirmation
