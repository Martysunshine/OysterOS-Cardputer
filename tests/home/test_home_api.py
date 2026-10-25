"""Smart Home routes: configured and unconfigured, rooms and devices, actions, impactful gating, idempotency,
upstream failures. The Home Assistant behind them is the fake in _home_support."""

from __future__ import annotations

import json

import pytest
from _home_support import FAKE_TOKEN, HomeKit


def codes(r) -> tuple[int, str]:
    return r.status_code, r.json().get("error", "")


# ---- rooms ----------------------------------------------------------------------------------------------


def test_rooms_lists_the_allowlist_after_one_reachability_check(kit: HomeKit):
    r = kit.rooms()
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True and body["next_after"] is None
    assert body["rooms"] == [
        {"id": "living", "name": "Living room", "count": 3},
        {"id": "hall", "name": "Hall", "count": 2},
        {"id": "office", "name": "Office", "count": 2},
    ]
    kit.rooms()
    assert kit.ha.paths() == ["GET /api/"]  # remembered for a few seconds


def test_rooms_paging(kit: HomeKit):
    first = kit.rooms(limit=2).json()
    assert [r["id"] for r in first["rooms"]] == ["living", "hall"] and first["next_after"] == "2"
    second = kit.rooms(limit=2, after="2").json()
    assert [r["id"] for r in second["rooms"]] == ["office"] and second["next_after"] is None


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"url": ""}, "no_url"),
        ({"token": ""}, "no_token"),
        ({"allowlist": None}, "no_allowlist"),
        ({"allowlist": "{broken"}, "bad_allowlist"),
        ({"allowlist": {"rooms": []}}, "no_rooms"),
    ],
)
def test_unconfigured_is_a_clean_answer_and_touches_nothing(kit_factory, kw, reason):
    kit = kit_factory(**kw)
    r = kit.rooms()
    assert r.status_code == 200
    assert r.json() == {"configured": False, "reason": reason, "rooms": [], "next_after": None}
    assert kit.ha.requests == []
    assert kit.devices("living").status_code == 503
    assert codes(kit.devices("living")) == (503, "home_not_configured")
    assert codes(kit.act("light.living_ceiling", "toggle")) == (503, "home_not_configured")
    assert kit.ha.requests == []


def test_rooms_when_home_assistant_is_down_or_rejects_the_token(kit: HomeKit):
    kit.ha.fail = "down"
    assert codes(kit.rooms()) == (503, "home_unreachable")
    kit.ha.fail = "401"
    assert codes(kit.rooms()) == (502, "home_auth_failed")
    kit.ha.fail = None
    assert kit.rooms().json()["configured"] is True  # a failure is not remembered as a success


# ---- devices --------------------------------------------------------------------------------------------


def test_devices_show_label_state_flags_and_actions(kit: HomeKit):
    r = kit.devices("hall")
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == {"id": "hall", "name": "Hall"} and body["next_after"] is None
    assert body["devices"] == [
        {
            "entity_id": "lock.front_door",
            "label": "Front door",
            "state": "locked",
            "impactful": True,
            "actions": ["turn_on", "turn_off"],
        },
        {
            "entity_id": "light.hall_lamp",
            "label": "Hall lamp",
            "state": "off",
            "impactful": False,
            "actions": ["toggle"],
        },
    ]


def test_scene_state_is_not_read(kit: HomeKit):
    devices = kit.devices("office").json()["devices"]
    assert [d["state"] for d in devices] == ["off", "scene"]
    assert "GET /api/states/scene.office_focus" not in kit.ha.paths()


def test_entity_missing_in_home_assistant_is_shown_as_missing(kit: HomeKit):
    kit.ha.states.pop("light.hall_lamp")
    states = {d["entity_id"]: d["state"] for d in kit.devices("hall").json()["devices"]}
    assert states == {"lock.front_door": "locked", "light.hall_lamp": "missing"}


def test_devices_when_home_assistant_fails(kit: HomeKit):
    for mode, expected in (
        ("down", (503, "home_unreachable")),
        ("timeout", (503, "home_timeout")),
        ("401", (502, "home_auth_failed")),
        ("500", (502, "home_upstream_error")),
    ):
        kit.ha.fail = mode
        assert codes(kit.devices("living")) == expected
    assert kit.audit_rows() == []  # reads that fail upstream are not policy events


def test_devices_stop_asking_when_the_time_budget_is_spent(kit: HomeKit):
    kit.mono.step = 5.0  # every clock read costs 5 s: the 8 s budget is gone after the first entity
    devices = kit.devices("living").json()["devices"]
    assert [d["state"] for d in devices] == ["on", "unknown", "unknown"]


def test_unknown_room_is_404_and_audited(kit: HomeKit):
    assert codes(kit.devices("garage")) == (404, "not_found")
    assert codes(kit.devices("Living")) == (404, "not_found")  # not a valid room id at all
    assert codes(kit.devices("bad%0Aid")) == (404, "not_found")
    assert kit.ha.requests == []
    rows = kit.audit_rows()
    assert rows[0][:3] == ("denied", "denied", "unknown_room") and rows[0][4] == "garage"
    assert rows[1][4] is None and rows[2][4] is None  # an invalid id is never stored


def test_devices_paging_and_page_size(kit_factory):
    entities = [
        {"entity_id": f"light.lamp_{i:02d}", "label": f"Lamp {i}", "actions": ["toggle"]} for i in range(12)
    ]
    kit = kit_factory(allowlist={"rooms": [{"id": "big", "name": "Big", "entities": entities}]})
    kit.ha.states = {e["entity_id"]: "on" for e in entities}
    first = kit.devices("big").json()
    assert len(first["devices"]) == 8 and first["next_after"] == "8"
    second = kit.devices("big", after="8").json()
    assert [d["entity_id"] for d in second["devices"]] == [e["entity_id"] for e in entities[8:]]
    assert second["next_after"] is None
    assert len(kit.devices("big", limit=3).json()["devices"]) == 3
    assert kit.devices("big", limit=9).status_code == 422
    assert kit.devices("big", after="x").status_code == 422


def test_response_sizes_stay_below_the_device_bound(kit_factory):
    ids = [f"light.{'a' * 56}{i:02d}" for i in range(16)]  # the longest entity ids the allowlist accepts
    big = {
        "id": "x" * 24,
        "name": "N" * 24,
        "entities": [
            {
                "entity_id": e,
                "label": "L" * 24,
                "actions": ["turn_on", "turn_off", "toggle"],
                "impactful": True,
            }
            for e in ids
        ],
    }
    others = [
        {
            "id": f"{'r' * 22}{i:02d}",
            "name": "N" * 24,
            "entities": [{"entity_id": f"light.solo_{i}", "label": "S", "actions": ["toggle"]}],
        }
        for i in range(15)
    ]
    kit = kit_factory(allowlist={"rooms": [big, *others]})
    kit.ha.states = {e: "a" * 16 for e in ids}
    rooms = kit.rooms()
    assert len(rooms.json()["rooms"]) == 16 and len(rooms.content) < 4096
    first = kit.devices("x" * 24)
    assert len(first.json()["devices"]) == 8 and len(first.content) < 4096
    assert len(kit.devices("x" * 24, after="8").content) < 4096


# ---- actions --------------------------------------------------------------------------------------------


def test_a_plain_action_switches_and_reports_the_state(kit: HomeKit):
    r = kit.act("light.living_ceiling", "turn_off")
    assert r.status_code == 200
    assert r.json() == {
        "entity_id": "light.living_ceiling",
        "action": "turn_off",
        "result": "ok",
        "state": "off",
        "duplicate": False,
    }
    assert kit.ha.service_calls == [("light", "turn_off", "light.living_ceiling")]
    assert kit.act("light.living_ceiling", "toggle").json()["state"] == "on"


def test_generic_actions_map_to_the_domain_services(kit: HomeKit):
    assert kit.act("lock.front_door", "turn_off", confirmed=True).json()["state"] == "unlocked"
    assert kit.act("lock.front_door", "turn_on", confirmed=True).json()["state"] == "locked"
    assert kit.act("cover.living_blinds", "turn_on", confirmed=True).json()["state"] == "open"
    assert kit.act("cover.living_blinds", "turn_off", confirmed=True).json()["state"] == "closed"
    assert kit.act("scene.office_focus", "turn_on").json()["state"] == "activated"
    assert kit.ha.service_calls == [
        ("lock", "unlock", "lock.front_door"),
        ("lock", "lock", "lock.front_door"),
        ("cover", "open_cover", "cover.living_blinds"),
        ("cover", "close_cover", "cover.living_blinds"),
        ("scene", "turn_on", "scene.office_focus"),
    ]


def test_state_is_read_back_when_the_answer_does_not_carry_it(kit: HomeKit):
    kit.ha.empty_service_answer = True
    r = kit.act("light.living_ceiling", "turn_off")
    assert r.status_code == 200 and r.json()["state"] == "on"  # what Home Assistant reports, not a guess
    assert kit.ha.paths()[-1] == "GET /api/states/light.living_ceiling"


def test_an_unreadable_state_after_a_good_call_is_unknown_not_an_error(kit: HomeKit):
    kit.ha.empty_service_answer = True
    kit.ha.fail, kit.ha.fail_on = "500", "/api/states"
    r = kit.act("light.living_ceiling", "turn_off")
    assert r.status_code == 200 and r.json()["state"] == "unknown"


def test_impactful_entities_need_the_confirmation(kit: HomeKit):
    for confirmed in (None, False):
        r = kit.act("lock.front_door", "turn_off", confirmed=confirmed)
        assert codes(r) == (409, "confirmation_required")
    assert codes(kit.act("switch.office_heater", "turn_on")) == (409, "confirmation_required")
    assert kit.ha.service_calls == []
    ok = kit.act("lock.front_door", "turn_off", confirmed=True)
    assert ok.status_code == 200
    assert kit.ha.service_calls == [("lock", "unlock", "lock.front_door")]
    # a confirmation flag on a harmless entity is ignored, not an error
    assert kit.act("light.living_ceiling", "toggle", confirmed=True).status_code == 200


def test_confirmed_must_be_a_real_boolean(kit: HomeKit):
    for value in ("true", 1, "yes", None, [True]):
        r = kit.act("", "", body={"entity_id": "lock.front_door", "action": "turn_off", "confirmed": value})
        assert r.status_code == 422, value
    assert kit.ha.service_calls == []


def test_the_allowlist_is_enforced(kit: HomeKit):
    assert codes(kit.act("light.bedroom", "turn_on")) == (404, "not_found")  # well-formed but not listed
    assert codes(kit.act("lock.garage", "turn_off", confirmed=True)) == (404, "not_found")
    assert codes(kit.act("light.hall_lamp", "turn_on")) == (
        403,
        "action_not_allowed",
    )  # only toggle is listed
    assert codes(kit.act("switch.living_fan", "toggle")) == (403, "action_not_allowed")
    assert kit.ha.requests == []
    reasons = [row[2] for row in kit.audit_rows()]
    assert reasons == ["not_found", "not_found", "action_not_allowed", "action_not_allowed"]


@pytest.mark.parametrize(
    "body",
    [
        {"entity_id": "light.living_ceiling", "action": "reboot"},
        {"entity_id": "light.living_ceiling", "action": "turn_on", "extra": 1},
        {"entity_id": "Light.Living", "action": "turn_on"},
        {"entity_id": "light.living_ceiling\n", "action": "turn_on"},
        {"entity_id": "light.living_ceiling/../config", "action": "turn_on"},
        {"entity_id": "x" * 65, "action": "turn_on"},
        {"entity_id": 5, "action": "turn_on"},
        {"action": "turn_on"},
        {"entity_id": "light.living_ceiling"},
        {"entity_id": "light.living_ceiling", "action": ["turn_on"]},
    ],
)
def test_malformed_action_bodies_are_422(kit: HomeKit, body):
    assert codes(kit.act("", "", body=body))[0] == 422
    assert kit.ha.requests == []


def test_body_must_be_json_object_and_small(kit: HomeKit):
    h = {**kit.dev_a, "Idempotency-Key": "key-aaaaaaaa"}
    assert kit.client.post("/v1/home/actions", headers=h, content=b"[1]").status_code == 422
    assert kit.client.post("/v1/home/actions", headers=h, content=b"not json").status_code == 422
    big = json.dumps({"entity_id": "light.living_ceiling", "action": "turn_on", "pad": "x" * 5000}).encode()
    assert codes(kit.client.post("/v1/home/actions", headers=h, content=big)) == (413, "payload_too_large")
    assert kit.ha.requests == []


@pytest.mark.parametrize("key", [None, "", "short", "k" * 81, "has space 1234", "semi;colon-1234"])
def test_idempotency_key_is_required_and_bounded(kit: HomeKit, key):
    r = kit.act("light.living_ceiling", "toggle", key=key)
    assert r.status_code == 422
    assert kit.ha.requests == []


# ---- idempotency ----------------------------------------------------------------------------------------


def test_a_repeated_key_returns_the_stored_answer_and_does_not_switch_twice(kit: HomeKit):
    first = kit.act("light.living_ceiling", "toggle", key="same-key-001")
    again = kit.act("light.living_ceiling", "toggle", key="same-key-001")
    assert first.json()["duplicate"] is False and again.json()["duplicate"] is True
    assert {k: v for k, v in again.json().items() if k != "duplicate"} == {
        k: v for k, v in first.json().items() if k != "duplicate"
    }
    assert kit.ha.service_calls == [("light", "toggle", "light.living_ceiling")]
    assert kit.ha.states["light.living_ceiling"] == "off"
    assert [r[1] for r in kit.audit_rows()] == ["pending", "ok", "replayed"]


def test_the_same_key_for_another_request_is_a_conflict(kit: HomeKit):
    kit.act("light.living_ceiling", "toggle", key="same-key-002")
    assert codes(kit.act("light.living_ceiling", "turn_on", key="same-key-002")) == (
        409,
        "idempotency_conflict",
    )
    assert codes(kit.act("light.hall_lamp", "toggle", key="same-key-002")) == (409, "idempotency_conflict")
    assert len(kit.ha.service_calls) == 1


def test_keys_are_scoped_to_the_device(kit: HomeKit):
    a = kit.act("light.living_ceiling", "toggle", key="shared-key-1", headers=kit.dev_a)
    b = kit.act("light.living_ceiling", "toggle", key="shared-key-1", headers=kit.dev_b)
    assert a.json()["duplicate"] is False and b.json()["duplicate"] is False
    assert len(kit.ha.service_calls) == 2


def test_a_failed_action_does_not_burn_its_key(kit: HomeKit):
    kit.ha.fail, kit.ha.fail_on = "down", "/api/services"
    assert codes(kit.act("light.living_ceiling", "toggle", key="retry-key-01")) == (503, "home_unreachable")
    kit.ha.fail = None
    r = kit.act("light.living_ceiling", "toggle", key="retry-key-01")
    assert r.status_code == 200 and r.json()["duplicate"] is False


def test_replays_survive_a_restart(kit: HomeKit):
    kit.act("light.living_ceiling", "toggle", key="restart-key-1")
    kit.restart()
    again = kit.act("light.living_ceiling", "toggle", key="restart-key-1")
    assert again.json()["duplicate"] is True
    assert len(kit.ha.service_calls) == 1


def test_replays_expire_after_a_day(kit: HomeKit):
    kit.act("light.living_ceiling", "toggle", key="old-key-0001")
    kit.clock.advance(24 * 3600 + 1)
    again = kit.act("light.living_ceiling", "toggle", key="old-key-0001")
    assert again.json()["duplicate"] is False and len(kit.ha.service_calls) == 2


def test_a_request_that_is_still_running_is_not_started_twice(kit: HomeKit):
    seen: list[int] = []

    def during_call(request):
        if request.method == "POST":
            seen.append(kit.act("light.living_ceiling", "toggle", key="inflight-key1").status_code)

    kit.ha.on_request = during_call
    first = kit.act("light.living_ceiling", "toggle", key="inflight-key1")
    assert first.status_code == 200
    assert seen == [409]  # the nested identical request was refused while the first was in flight
    assert len(kit.ha.service_calls) == 1


# ---- upstream failures on actions -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected", "outcome"),
    [
        ("down", (503, "home_unreachable"), "failed"),
        ("timeout", (503, "home_timeout"), "unknown_outcome"),
        ("401", (502, "home_auth_failed"), "failed"),
        ("400", (502, "home_refused"), "failed"),
        ("500", (502, "home_upstream_error"), "failed"),
        ("redirect", (502, "home_upstream_error"), "failed"),
    ],
)
def test_upstream_failures_on_actions(kit: HomeKit, mode, expected, outcome):
    kit.ha.fail, kit.ha.fail_on = mode, "/api/services"
    r = kit.act("light.living_ceiling", "toggle")
    assert codes(r) == expected
    assert FAKE_TOKEN not in r.text and "192.168.1.50" not in r.text
    assert [row[1] for row in kit.audit_rows()] == ["pending", outcome]
    assert kit.audit_rows()[-1][2] == expected[1]


def test_no_audit_row_no_action(kit: HomeKit, monkeypatch):
    import sqlite3

    def broken(entry):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(kit.runtime.store, "add_audit", broken)
    r = kit.act("light.living_ceiling", "toggle")
    assert codes(r) == (503, "audit_unavailable")
    assert kit.ha.requests == []


def test_a_bad_ca_file_is_a_clean_error(kit_factory):
    kit = kit_factory()
    kit.runtime.service._transport = None
    object.__setattr__(kit.config, "url", "https://ha.example.test")
    object.__setattr__(kit.config, "ca_file", "/nonexistent/ca.pem")
    assert codes(kit.devices("living")) == (503, "home_unreachable")


# ---- rate limit -----------------------------------------------------------------------------------------


def test_actions_are_rate_limited_per_device(kit_factory):
    kit = kit_factory(actions_per_minute=3)
    for _ in range(3):
        assert kit.act("light.living_ceiling", "toggle").status_code == 200
    r = kit.act("light.living_ceiling", "toggle")
    assert codes(r) == (429, "rate_limited") and int(r.headers["Retry-After"]) >= 1
    assert kit.act("light.living_ceiling", "toggle", headers=kit.dev_b).status_code == 200  # another device
    kit.clock.advance(61)
    assert kit.act("light.living_ceiling", "toggle").status_code == 200
    assert len(kit.ha.service_calls) == 5


def test_reads_are_rate_limited_per_device(kit_factory):
    kit = kit_factory(reads_per_minute=2)
    assert kit.rooms().status_code == 200 and kit.devices("living").status_code == 200
    assert codes(kit.rooms()) == (429, "rate_limited")
    assert kit.rooms(headers=kit.dev_b).status_code == 200


# ---- the allowlist file is re-read when it changes -------------------------------------------------------


def test_allowlist_changes_apply_without_a_restart(kit: HomeKit):
    assert kit.rooms().json()["configured"] is True
    kit.write_allowlist(
        {
            "rooms": [
                {
                    "id": "new",
                    "name": "New",
                    "entities": [{"entity_id": "light.only_one", "label": "One", "actions": ["toggle"]}],
                }
            ]
        }
    )
    assert [r["id"] for r in kit.rooms().json()["rooms"]] == ["new"]
    assert codes(kit.act("light.living_ceiling", "toggle")) == (404, "not_found")  # no longer listed
    kit.write_allowlist("{broken")
    assert kit.rooms().json() == {
        "configured": False,
        "reason": "bad_allowlist",
        "rooms": [],
        "next_after": None,
    }
    assert codes(kit.act("light.only_one", "toggle")) == (503, "home_not_configured")  # fails closed
    kit.write_allowlist("example")
    assert kit.rooms().json()["configured"] is True
