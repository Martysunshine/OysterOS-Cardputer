"""Seed data for the Control Deck: the `desk` profile (a macro pad for the owner's own PC).

`ensure_desk_profile` is idempotent: it defines the profile through the normal service path (so validation,
audit and the "replacing cancels unstarted jobs" rule all apply) and reports `created`, `updated` or
`unchanged`. The slots are fixed here; which devices may run them is the caller's choice.
"""

from __future__ import annotations

from collections.abc import Iterable

from oyster.deck.executor_windows import DEFAULT_LOCAL_HOST_ID
from oyster.deck.jobs import DeckService
from oyster.deck.profiles import Profile, load_profile

DESK_PROFILE_ID = "desk"
DESK_PROFILE_NAME = "Desk"  # at most 24 characters, like every label

# (slot, label, action fields without host_id). Labels are at most 24 printable ASCII characters.
_SLOTS: tuple[tuple[int, str, dict], ...] = (
    (1, "PC health", {"type": "host_health_check"}),
    (2, "Play/Pause", {"type": "media_key", "key": "play_pause"}),
    (3, "Next", {"type": "media_key", "key": "next"}),
    (4, "Prev", {"type": "media_key", "key": "prev"}),
    (5, "Vol+", {"type": "media_key", "key": "vol_up"}),
    (6, "Vol-", {"type": "media_key", "key": "vol_down"}),
    (7, "Mute", {"type": "media_key", "key": "mute"}),
    (8, "Lock PC", {"type": "lock_workstation"}),
)


def desk_profile(device_ids: Iterable[str], local_host_id: str = DEFAULT_LOCAL_HOST_ID) -> Profile:
    """The `desk` profile bound to `device_ids`, acting on the host `local_host_id`."""
    ids = list(dict.fromkeys(device_ids))  # unique, order kept
    return load_profile(
        {
            "profile_id": DESK_PROFILE_ID,
            "name": DESK_PROFILE_NAME,
            "device_ids": ids,
            "hosts": {local_host_id: {"services": []}},
            "slots": {
                str(n): {"label": label, "action": {**action, "host_id": local_host_id}}
                for n, label, action in _SLOTS
            },
        }
    )


def ensure_desk_profile(
    runtime_or_service, device_ids: Iterable[str], local_host_id: str = DEFAULT_LOCAL_HOST_ID
) -> str:
    """Create or update the `desk` profile. Accepts a `DeckRuntime` or a `DeckService`.

    Returns "created", "updated" (the content differed: unstarted jobs of the old profile are cancelled) or
    "unchanged". Raises `ProfileInvalid` for an invalid device or host id.
    """
    service: DeckService = getattr(runtime_or_service, "service", runtime_or_service)
    return service.put_profile(desk_profile(device_ids, local_host_id))
