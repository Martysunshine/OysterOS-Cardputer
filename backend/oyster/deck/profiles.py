"""Owner-defined macro profiles.

A profile has an id, a name, the device ids allowed to run it, the hosts (and their service names) its
actions may reference, and slots 1..9. A slot holds a label and one typed action, or an ordered list of at
most three. Validation runs when the owner defines a profile and again whenever a stored profile is loaded
to run a slot, so a stored row that no longer passes (an unknown action type, an unlisted host) is refused.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from oyster.deck.actions import ID_PATTERN, Action, HostId, ServiceName, is_impactful

MAX_SLOTS = 9
MAX_ACTIONS_PER_SLOT = 3
MAX_HOSTS = 8
MAX_SERVICES_PER_HOST = 16
MAX_DEVICES = 16
LABEL_PATTERN = r"^[ -~]{1,24}$"  # printable ASCII only: the device screen is 240x135 with a fixed font
SlotKey = Annotated[str, StringConstraints(pattern=r"^[1-9]$")]


class ProfileInvalid(ValueError):
    """A stored or submitted profile fails validation. `reason` is a short machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class HostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    services: list[ServiceName] = Field(default_factory=list, max_length=MAX_SERVICES_PER_HOST)


class Slot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str = Field(pattern=LABEL_PATTERN)
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS_PER_SLOT)

    @model_validator(mode="before")
    @classmethod
    def _single_action(cls, data: Any) -> Any:
        """Accept `action` (one) or `actions` (ordered list); stored form is always `actions`."""
        if isinstance(data, dict) and "action" in data:
            if "actions" in data:
                raise ValueError("give either action or actions, not both")
            data = {**data, "actions": [data["action"]]}
            del data["action"]
        return data

    @property
    def impactful(self) -> bool:
        return any(is_impactful(a) for a in self.actions)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(pattern=LABEL_PATTERN)
    device_ids: list[Annotated[str, Field(pattern=ID_PATTERN)]] = Field(min_length=1, max_length=MAX_DEVICES)
    hosts: dict[HostId, HostSpec] = Field(default_factory=dict, max_length=MAX_HOSTS)
    slots: dict[SlotKey, Slot] = Field(min_length=1, max_length=MAX_SLOTS)

    @field_validator("device_ids")
    @classmethod
    def _unique_devices(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("device_ids must be unique")
        return v

    @model_validator(mode="after")
    def _references_listed(self) -> Profile:
        for key, slot in self.slots.items():
            for a in slot.actions:
                host = getattr(a, "host_id", None)
                if host is not None and host not in self.hosts:
                    raise ValueError(f"slot {key}: host_id is not listed in hosts")
                service = getattr(a, "service", None)
                if service is not None and service not in self.hosts[host].services:
                    raise ValueError(f"slot {key}: service is not listed for its host")
        return self

    def slot(self, n: int) -> Slot | None:
        return self.slots.get(str(n))

    def summary(self) -> dict:
        """Compact device view (no hosts, services or action arguments)."""
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "slots": [
                {"slot": int(k), "label": s.label, "impactful": s.impactful}
                for k, s in sorted(self.slots.items(), key=lambda kv: int(kv[0]))
            ],
        }


def _unknown_action(e: ValidationError) -> bool:
    return any(
        err["type"] in ("union_tag_invalid", "union_tag_not_found") and "actions" in err["loc"]
        for err in e.errors()
    )


def load_profile(raw: object) -> Profile:
    """Validate a profile given as a dict or a JSON string. Raises ProfileInvalid (never echoes input)."""
    try:
        if isinstance(raw, str | bytes):
            return Profile.model_validate_json(raw)
        return Profile.model_validate(raw)
    except ValidationError as e:
        if _unknown_action(e):
            raise ProfileInvalid("action_not_allowed", "an action type is not on the allowlist") from None
        first = e.errors()[0]
        where = "/".join(str(p) for p in first.get("loc", ())) or "(body)"
        raise ProfileInvalid("profile_invalid", f"{where}: {first.get('msg')}"[:300]) from None


def dump_profile(profile: Profile) -> str:
    """Canonical JSON used for storage and change detection."""
    return profile.model_dump_json()
