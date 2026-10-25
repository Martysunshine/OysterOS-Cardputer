"""The Smart Home allowlist: rooms > entities, written by the owner as a JSON file.

    {
      "version": 1,
      "rooms": [
        {"id": "living", "name": "Living room", "entities": [
          {"entity_id": "light.living_ceiling", "label": "Ceiling light",
           "actions": ["turn_on", "turn_off", "toggle"]},
          {"entity_id": "lock.front_door", "label": "Front door", "actions": ["turn_on", "turn_off"]}
        ]}
      ]
    }

Only what is listed can be read or controlled. Rules (any violation makes the whole file invalid, so a typo
never silently widens or narrows access): at most 16 rooms of at most 16 entities; room ids are
a-z 0-9 _ - (1..24); names and labels are printable ASCII (1..24) because the device screen has one fixed
font; an entity id is `domain.object_id` (lowercase, at most 64) whose domain is one of DOMAIN_SERVICES; every
entity appears once in the whole file; `actions` is a non-empty subset of turn_on, turn_off, toggle that the
domain supports. The domains lock, cover and alarm_control_panel are always impactful (local confirmation on
the device) whatever the file says; any other entity can be marked `"impactful": true`.

The generic actions map to the Home Assistant service of the entity's domain (DOMAIN_SERVICES): for a lock
turn_on is lock and turn_off is unlock, for a cover turn_on is open and turn_off is close, for an alarm panel
only turn_on (arm away) exists: disarming needs a code, which this code never stores, so it is not offered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 64 * 1024
MAX_ROOMS = 16
MAX_ENTITIES_PER_ROOM = 16
MAX_PROBLEMS = 10
ACTIONS = ("turn_on", "turn_off", "toggle")

_GENERIC = {"turn_on": "turn_on", "turn_off": "turn_off", "toggle": "toggle"}
# domain -> {generic action: Home Assistant service}
DOMAIN_SERVICES: dict[str, dict[str, str]] = {
    "light": _GENERIC,
    "switch": _GENERIC,
    "fan": _GENERIC,
    "input_boolean": _GENERIC,
    "humidifier": _GENERIC,
    "media_player": _GENERIC,
    "climate": _GENERIC,
    "scene": {"turn_on": "turn_on"},
    "lock": {"turn_on": "lock", "turn_off": "unlock"},
    "cover": {"turn_on": "open_cover", "turn_off": "close_cover", "toggle": "toggle"},
    "alarm_control_panel": {"turn_on": "alarm_arm_away"},
}
ALWAYS_IMPACTFUL = frozenset({"lock", "cover", "alarm_control_panel"})

ROOM_ID = re.compile(r"^[a-z0-9_-]{1,24}$")
ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
TEXT = re.compile(r"^[ -~]{1,24}$")
MAX_ENTITY_ID_LEN = 64


class AllowlistError(Exception):
    """`reason` is the machine code the device screen maps (no_allowlist, bad_allowlist, no_rooms)."""

    def __init__(self, reason: str, problems: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.problems = problems or []


@dataclass(frozen=True)
class Entity:
    entity_id: str
    label: str
    actions: tuple[str, ...]
    impactful: bool
    room_id: str

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def service_for(self, action: str) -> tuple[str, str]:
        """(domain, service) of the Home Assistant call for a generic action of this entity."""
        return self.domain, DOMAIN_SERVICES[self.domain][action]


@dataclass(frozen=True)
class Room:
    room_id: str
    name: str
    entities: tuple[Entity, ...]


@dataclass(frozen=True)
class Allowlist:
    rooms: tuple[Room, ...]

    def room(self, room_id: str) -> Room | None:
        return next((r for r in self.rooms if r.room_id == room_id), None)

    def entity(self, entity_id: str) -> Entity | None:
        for r in self.rooms:
            for e in r.entities:
                if e.entity_id == entity_id:
                    return e
        return None

    @property
    def entity_count(self) -> int:
        return sum(len(r.entities) for r in self.rooms)

    @property
    def impactful_count(self) -> int:
        return sum(1 for r in self.rooms for e in r.entities if e.impactful)


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def parse_allowlist(data: object) -> Allowlist:
    """Validate decoded JSON. Raises AllowlistError("bad_allowlist" | "no_rooms") with the first problems."""
    problems: list[str] = []

    def bad(msg: str) -> None:
        if len(problems) < MAX_PROBLEMS:
            problems.append(msg)

    if not isinstance(data, dict):
        raise AllowlistError("bad_allowlist", ["the file must contain a JSON object"])
    for key in data:
        if key not in ("version", "rooms"):
            bad(f"unknown key {_short(key)}")
    if "version" in data and (type(data["version"]) is not int or data["version"] != 1):
        bad("version must be 1")
    raw_rooms = data.get("rooms")
    if not isinstance(raw_rooms, list):
        raise AllowlistError("bad_allowlist", [*problems, "rooms must be a list"][:MAX_PROBLEMS])
    if len(raw_rooms) > MAX_ROOMS:
        bad(f"at most {MAX_ROOMS} rooms")

    rooms: list[Room] = []
    seen_rooms: set[str] = set()
    seen_entities: set[str] = set()
    for ri, raw in enumerate(raw_rooms[:MAX_ROOMS]):
        where = f"rooms[{ri}]"
        if not isinstance(raw, dict):
            bad(f"{where}: must be an object")
            continue
        for key in raw:
            if key not in ("id", "name", "entities"):
                bad(f"{where}: unknown key {_short(key)}")
        room_id, name, raw_entities = raw.get("id"), raw.get("name"), raw.get("entities")
        if not isinstance(room_id, str) or not ROOM_ID.fullmatch(room_id):
            bad(f"{where}: id must be 1..24 of a-z 0-9 _ -")
            continue
        if room_id in seen_rooms:
            bad(f"{where}: duplicate room id {_short(room_id)}")
            continue
        seen_rooms.add(room_id)
        if not isinstance(name, str) or not TEXT.fullmatch(name):
            bad(f"{where}: name must be 1..24 printable ASCII characters")
            continue
        if not isinstance(raw_entities, list) or not raw_entities:
            bad(f"{where}: entities must be a non-empty list")
            continue
        if len(raw_entities) > MAX_ENTITIES_PER_ROOM:
            bad(f"{where}: at most {MAX_ENTITIES_PER_ROOM} entities")
        entities: list[Entity] = []
        for ei, rawe in enumerate(raw_entities[:MAX_ENTITIES_PER_ROOM]):
            ent = _entity(rawe, f"{where}.entities[{ei}]", room_id, seen_entities, bad)
            if ent is not None:
                entities.append(ent)
        rooms.append(Room(room_id, name, tuple(entities)))

    if problems:
        raise AllowlistError("bad_allowlist", problems)
    if not rooms:
        raise AllowlistError("no_rooms")
    return Allowlist(tuple(rooms))


def _entity(raw: object, where: str, room_id: str, seen: set[str], bad) -> Entity | None:
    if not isinstance(raw, dict):
        bad(f"{where}: must be an object")
        return None
    for key in raw:
        if key not in ("entity_id", "label", "actions", "impactful"):
            bad(f"{where}: unknown key {_short(key)}")
    eid, label, actions = raw.get("entity_id"), raw.get("label"), raw.get("actions")
    if not isinstance(eid, str) or len(eid) > MAX_ENTITY_ID_LEN or not ENTITY_ID.fullmatch(eid):
        bad(f"{where}: entity_id must look like light.kitchen (lowercase, at most {MAX_ENTITY_ID_LEN})")
        return None
    domain = eid.split(".", 1)[0]
    if domain not in DOMAIN_SERVICES:
        bad(f"{where}: domain {_short(domain)} is not supported")
        return None
    if eid in seen:
        bad(f"{where}: {_short(eid)} is listed twice")
        return None
    seen.add(eid)
    if not isinstance(label, str) or not TEXT.fullmatch(label):
        bad(f"{where}: label must be 1..24 printable ASCII characters")
        return None
    if not isinstance(actions, list) or not actions or len(actions) > len(ACTIONS):
        bad(f"{where}: actions must be a non-empty list of {', '.join(ACTIONS)}")
        return None
    supported = DOMAIN_SERVICES[domain]
    for a in actions:
        if a not in ACTIONS:
            bad(f"{where}: unknown action {_short(a)}")
            return None
        if a not in supported:
            bad(f"{where}: {_short(a)} is not available for {domain}")
            return None
    if len(set(actions)) != len(actions):
        bad(f"{where}: actions repeat")
        return None
    impactful = raw.get("impactful", False)
    if not isinstance(impactful, bool):
        bad(f"{where}: impactful must be true or false")
        return None
    ordered = tuple(a for a in ACTIONS if a in actions)
    return Entity(eid, label, ordered, impactful or domain in ALWAYS_IMPACTFUL, room_id)


def load_allowlist(path: Path) -> Allowlist:
    """Read and validate the file. Raises AllowlistError (no_allowlist, bad_allowlist, no_rooms)."""
    try:
        st = path.stat()
    except OSError:
        raise AllowlistError("no_allowlist") from None
    if not path.is_file():
        raise AllowlistError("no_allowlist")
    if st.st_size > MAX_FILE_BYTES:
        raise AllowlistError("bad_allowlist", [f"the file is larger than {MAX_FILE_BYTES // 1024} KiB"])
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise AllowlistError("bad_allowlist", ["the file is not valid UTF-8 JSON"]) from None
    return parse_allowlist(data)
