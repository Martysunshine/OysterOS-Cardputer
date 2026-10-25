"""Allowlist file: format, limits, always-impactful domains, fail-closed validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from _home_support import EXAMPLE_ALLOWLIST
from oyster.home.allowlist import (
    ALWAYS_IMPACTFUL,
    DOMAIN_SERVICES,
    MAX_FILE_BYTES,
    MAX_PROBLEMS,
    AllowlistError,
    load_allowlist,
    parse_allowlist,
)


def one(entity: dict, room_id: str = "r1") -> dict:
    return {"rooms": [{"id": room_id, "name": "Room", "entities": [entity]}]}


def ent(**kw) -> dict:
    return {"entity_id": "light.kitchen", "label": "Kitchen", "actions": ["turn_on", "turn_off"], **kw}


def reason_of(data: object) -> str:
    with pytest.raises(AllowlistError) as e:
        parse_allowlist(data)
    return e.value.reason


def test_example_file_is_valid_and_uses_fake_ids():
    al = load_allowlist(EXAMPLE_ALLOWLIST)
    assert [r.room_id for r in al.rooms] == ["living", "hall", "office"]
    assert al.entity_count == 7
    text = EXAMPLE_ALLOWLIST.read_text(encoding="utf-8")
    assert "192." not in text and "token" not in text.lower()
    lock = al.entity("lock.front_door")
    assert lock is not None and lock.impactful and lock.room_id == "hall"
    heater = al.entity("switch.office_heater")
    assert heater is not None and heater.impactful  # declared in the file
    assert not al.entity("light.living_ceiling").impactful


def test_actions_are_ordered_and_mapped_to_services():
    al = parse_allowlist(one(ent(actions=["toggle", "turn_on"])))
    e = al.entity("light.kitchen")
    assert e.actions == ("turn_on", "toggle")
    assert e.service_for("toggle") == ("light", "toggle")
    lock = parse_allowlist(
        one({"entity_id": "lock.door", "label": "Door", "actions": ["turn_on", "turn_off"]})
    )
    assert lock.entity("lock.door").service_for("turn_on") == ("lock", "lock")
    assert lock.entity("lock.door").service_for("turn_off") == ("lock", "unlock")


@pytest.mark.parametrize("domain", sorted(ALWAYS_IMPACTFUL))
def test_always_impactful_domains_cannot_be_unmarked(domain):
    action = next(iter(DOMAIN_SERVICES[domain]))
    al = parse_allowlist(
        one({"entity_id": f"{domain}.x", "label": "X", "actions": [action], "impactful": False})
    )
    assert al.entity(f"{domain}.x").impactful is True


def test_other_domains_are_impactful_only_when_marked():
    assert not parse_allowlist(one(ent())).entity("light.kitchen").impactful
    assert parse_allowlist(one(ent(impactful=True))).entity("light.kitchen").impactful


def test_alarm_panel_only_offers_arming_and_scene_only_activation():
    assert reason_of(
        one({"entity_id": "alarm_control_panel.home", "label": "A", "actions": ["turn_off"]})
    ) == ("bad_allowlist")
    assert reason_of(one({"entity_id": "scene.x", "label": "S", "actions": ["toggle"]})) == "bad_allowlist"
    assert reason_of(one({"entity_id": "lock.x", "label": "L", "actions": ["toggle"]})) == "bad_allowlist"
    parse_allowlist(one({"entity_id": "alarm_control_panel.home", "label": "A", "actions": ["turn_on"]}))


BAD_ENTITIES = {
    "unknown domain": ent(entity_id="script.reboot"),
    "shell-like domain": ent(entity_id="shell_command.x"),
    "uppercase": ent(entity_id="light.Kitchen"),
    "trailing newline": ent(entity_id="light.kitchen\n"),
    "no dot": ent(entity_id="lightkitchen"),
    "two dots": ent(entity_id="light.a.b"),
    "path": ent(entity_id="light.../states"),
    "too long": ent(entity_id="light." + "a" * 60),
    "non-string id": ent(entity_id=5),
    "label empty": ent(label=""),
    "label too long": ent(label="x" * 25),
    "label non-ascii": ent(label="Küche"),
    "label newline": ent(label="a\nb"),
    "label trailing newline": ent(label="abc\n"),
    "label not a string": ent(label=["x"]),
    "no actions": ent(actions=[]),
    "actions not a list": ent(actions="turn_on"),
    "unknown action": ent(actions=["turn_on", "reboot"]),
    "repeated action": ent(actions=["turn_on", "turn_on"]),
    "too many actions": ent(actions=["turn_on", "turn_off", "toggle", "toggle"]),
    "impactful string": ent(impactful="true"),
    "impactful number": ent(impactful=1),
    "extra key": ent(token="x"),
    "not an object": "light.kitchen",
}


@pytest.mark.parametrize("entity", list(BAD_ENTITIES.values()), ids=list(BAD_ENTITIES))
def test_invalid_entity_makes_the_whole_file_invalid(entity):
    good = {"rooms": [{"id": "ok", "name": "Ok", "entities": [ent(entity_id="light.fine")]}]}
    good["rooms"].append({"id": "r1", "name": "Room", "entities": [entity]})
    with pytest.raises(AllowlistError) as e:
        parse_allowlist(good)
    assert e.value.reason == "bad_allowlist" and e.value.problems


BAD_FILES = {
    "not an object": [],
    "rooms missing": {},
    "rooms not a list": {"rooms": {}},
    "unknown top key": {"rooms": [], "extra": 1},
    "version 2": {"version": 2, "rooms": []},
    "version true": {"version": True, "rooms": []},
    "room not an object": {"rooms": ["x"]},
    "room id upper": one(ent(), room_id="Living"),
    "room id space": one(ent(), room_id="a b"),
    "room id too long": one(ent(), room_id="a" * 25),
    "room id newline": one(ent(), room_id="abc\n"),
    "room name empty": {"rooms": [{"id": "a", "name": "", "entities": [ent()]}]},
    "room name non-ascii": {"rooms": [{"id": "a", "name": "Zimmerä", "entities": [ent()]}]},
    "room extra key": {"rooms": [{"id": "a", "name": "A", "entities": [ent()], "x": 1}]},
    "entities empty": {"rooms": [{"id": "a", "name": "A", "entities": []}]},
    "entities missing": {"rooms": [{"id": "a", "name": "A"}]},
}


@pytest.mark.parametrize("data", list(BAD_FILES.values()), ids=list(BAD_FILES))
def test_invalid_structure_is_rejected(data):
    assert reason_of(data) == "bad_allowlist"


def test_duplicates_are_rejected():
    dup_room = {
        "rooms": [
            {"id": "a", "name": "A", "entities": [ent()]},
            {"id": "a", "name": "B", "entities": [ent(entity_id="light.b")]},
        ]
    }
    assert reason_of(dup_room) == "bad_allowlist"
    dup_entity = {
        "rooms": [
            {"id": "a", "name": "A", "entities": [ent()]},
            {"id": "b", "name": "B", "entities": [ent(label="Again")]},
        ]
    }
    assert reason_of(dup_entity) == "bad_allowlist"


def test_size_limits():
    rooms = [{"id": f"r{i}", "name": f"R{i}", "entities": [ent(entity_id=f"light.l{i}")]} for i in range(16)]
    assert len(parse_allowlist({"rooms": rooms}).rooms) == 16
    rooms.append({"id": "r16", "name": "R16", "entities": [ent(entity_id="light.l16")]})
    assert reason_of({"rooms": rooms}) == "bad_allowlist"
    sixteen = [ent(entity_id=f"light.e{i}") for i in range(16)]
    assert parse_allowlist({"rooms": [{"id": "a", "name": "A", "entities": sixteen}]}).entity_count == 16
    sixteen.append(ent(entity_id="light.e16"))
    assert reason_of({"rooms": [{"id": "a", "name": "A", "entities": sixteen}]}) == "bad_allowlist"


def test_no_rooms_is_its_own_reason():
    assert reason_of({"rooms": []}) == "no_rooms"


def test_problem_list_is_bounded():
    bad = {
        "rooms": [{"id": f"r{i}", "name": "", "entities": [ent(entity_id=f"light.l{i}")]} for i in range(16)]
    }
    with pytest.raises(AllowlistError) as e:
        parse_allowlist(bad)
    assert len(e.value.problems) == MAX_PROBLEMS


def test_problems_do_not_echo_long_values():
    data = copy.deepcopy(one(ent()))
    data["rooms"][0]["entities"][0]["entity_id"] = "x" * 5000
    with pytest.raises(AllowlistError) as e:
        parse_allowlist(data)
    assert all(len(p) < 200 for p in e.value.problems)


def test_load_errors(tmp_path: Path):
    assert _load_reason(tmp_path / "missing.json") == "no_allowlist"
    d = tmp_path / "dir.json"
    d.mkdir()
    assert _load_reason(d) == "no_allowlist"
    f = tmp_path / "a.json"
    f.write_bytes(b"{not json")
    assert _load_reason(f) == "bad_allowlist"
    f.write_bytes(b"\xff\xfe{}")
    assert _load_reason(f) == "bad_allowlist"
    f.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    assert _load_reason(f) == "bad_allowlist"
    f.write_text(json.dumps({"rooms": []}), encoding="utf-8")
    assert _load_reason(f) == "no_rooms"


def _load_reason(path: Path) -> str:
    with pytest.raises(AllowlistError) as e:
        load_allowlist(path)
    return e.value.reason
