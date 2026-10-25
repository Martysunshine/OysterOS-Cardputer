"""The allowlist of typed Control Deck actions and the executor seam.

Every action is a pydantic model (`extra="forbid"`, bounded fields) tagged by `type`. Whether an action is
impactful (needs an on-device confirmation, ADR-005) is fixed per action type in this file: it is a class
constant, not a field, so no request, profile or stored row can change it. Unknown action types are
rejected when a profile is defined and again when a slot runs and when the runner executes it.

`MockExecutor` (the default) records calls and executes nothing on any host; `executor_windows.py` is the
real executor for the owner's own Windows PC. Any executor must guarantee:
  - typed arguments only: it receives these models, never a command string or template;
  - no shell and no string interpolation: the host-side operation is chosen by action type from a fixed
    table and arguments are passed as discrete values (host ids and service names are validated names
    resolved through the executor's own registry, never addresses or paths from a request);
  - a per-action timeout (`timeout_s` below is the ceiling the runner enforces) and no output beyond a
    short redacted summary;
  - least privilege: the rights of the account that started the backend, never more (no self-elevation);
  - an honest result: raise `ActionOutcomeUnknown` when it cannot tell whether an impactful action ran.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, ClassVar, Literal, Protocol, get_args, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

ID_PATTERN = r"^[0-9a-zA-Z_-]{1,40}$"
SERVICE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$"
MAX_SUMMARY_CHARS = 120

HostId = Annotated[str, Field(pattern=ID_PATTERN)]
ServiceName = Annotated[str, Field(pattern=SERVICE_PATTERN)]


class ActionRejected(ValueError):
    """The action is not on the allowlist or is malformed (generic text, never echoes the input)."""


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    impactful: ClassVar[bool]  # fixed per type; never a field, so it cannot be supplied or overridden
    timeout_s: ClassVar[float]  # ceiling for one execution, enforced by the runner


class Noop(_Action):
    """Test action: does nothing."""

    type: Literal["noop"] = "noop"
    impactful = False
    timeout_s = 5.0


class HostHealthCheck(_Action):
    """Read-only: CPU, memory and disk state of a host."""

    type: Literal["host_health_check"] = "host_health_check"
    host_id: HostId
    impactful = False
    timeout_s = 15.0


class ServiceStatus(_Action):
    """Read-only: state of one named service on a host."""

    type: Literal["service_status"] = "service_status"
    host_id: HostId
    service: ServiceName
    impactful = False
    timeout_s = 15.0


class WakeOnLan(_Action):
    """Impactful: powers a host on."""

    type: Literal["wake_on_lan"] = "wake_on_lan"
    host_id: HostId
    impactful = True
    timeout_s = 15.0


class ServiceRestart(_Action):
    """Impactful: restarts one named service on a host."""

    type: Literal["service_restart"] = "service_restart"
    host_id: HostId
    service: ServiceName
    impactful = True
    timeout_s = 60.0


MediaKeyName = Literal["play_pause", "next", "prev", "vol_up", "vol_down", "mute"]
MEDIA_KEYS: tuple[str, ...] = get_args(MediaKeyName)


class MediaKey(_Action):
    """Presses one system media key on a host. Any player that listens to media keys reacts (a music player,
    a browser tab, ...); no player API or account is involved. Not impactful: it changes no session state."""

    type: Literal["media_key"] = "media_key"
    host_id: HostId
    key: MediaKeyName
    impactful = False
    timeout_s = 10.0


class LockWorkstation(_Action):
    """Impactful: locks the interactive session of a host (the user signs in again to continue)."""

    type: Literal["lock_workstation"] = "lock_workstation"
    host_id: HostId
    impactful = True
    timeout_s = 15.0


Action = Annotated[
    Noop | HostHealthCheck | ServiceStatus | WakeOnLan | ServiceRestart | MediaKey | LockWorkstation,
    Field(discriminator="type"),
]
ACTION_MODELS: tuple[type[_Action], ...] = (
    Noop,
    HostHealthCheck,
    ServiceStatus,
    WakeOnLan,
    ServiceRestart,
    MediaKey,
    LockWorkstation,
)
ALLOWLIST: Mapping[str, type[_Action]] = MappingProxyType(
    {m.model_fields["type"].default: m for m in ACTION_MODELS}
)
_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(raw: object) -> _Action:
    """Validate one action against the allowlist (unknown type, extra field, bad value -> ActionRejected)."""
    if not isinstance(raw, dict) or raw.get("type") not in ALLOWLIST:
        raise ActionRejected("action type is not on the allowlist")
    try:
        return _ADAPTER.validate_python(raw)
    except ValidationError:
        raise ActionRejected("action arguments are invalid") from None


def action_type(action: _Action) -> str:
    return action.type  # type: ignore[attr-defined]


def is_impactful(action: _Action) -> bool:
    return type(action).impactful


def timeout_for(action: _Action) -> float:
    return type(action).timeout_s


def action_json(action: _Action) -> dict:
    return action.model_dump(mode="json")


# --- executor seam ---------------------------------------------------------------------------------


class ActionFailed(Exception):
    """The executor knows the action did not take effect (safe to report `failed`)."""


class ActionOutcomeUnknown(Exception):
    """The action may have run but its result is lost (impactful jobs end `unknown_outcome`)."""


class ActionTimeout(ActionOutcomeUnknown):
    """The action exceeded its timeout; whether it took effect is unknown."""


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    code: str = "ok"  # short machine code, [a-z0-9_]
    summary: str = ""  # short, redacted text for the device screen


@runtime_checkable
class ActionExecutor(Protocol):
    """Runs one validated action. `kind` names the implementation on the wire ("mock" for the test double)."""

    kind: str

    def execute(self, action: _Action) -> ActionResult: ...


class MockExecutor:
    """Test double and the default executor: records calls, executes nothing on any host.

    `inject(type, kind)` makes the next `times` calls of an action type misbehave: "fail" (ActionFailed),
    "timeout" (ActionTimeout), "lost" (ActionOutcomeUnknown), "error" (unexpected exception) or "hang"
    (blocks until `release()`, to exercise the runner's own timeout)."""

    kind = "mock"
    HANG_LIMIT_S = 5.0

    def __init__(self) -> None:
        self.calls: list[_Action] = []
        self._lock = threading.Lock()
        self._injected: dict[str, list[str]] = {}
        self._hanging: list[threading.Event] = []

    def inject(self, type_: str, kind: str, times: int = 1) -> None:
        if kind not in ("fail", "timeout", "lost", "error", "hang"):
            raise ValueError(f"unknown injection {kind!r}")
        with self._lock:
            self._injected.setdefault(type_, []).extend([kind] * times)

    def release(self) -> None:
        """Let every call that is currently hanging return."""
        with self._lock:
            hanging, self._hanging = self._hanging, []
        for event in hanging:
            event.set()

    def execute(self, action: _Action) -> ActionResult:
        with self._lock:
            self.calls.append(action)
            queue = self._injected.get(action_type(action))
            kind = queue.pop(0) if queue else None
        if kind == "fail":
            raise ActionFailed("injected failure")
        if kind == "timeout":
            raise ActionTimeout("injected timeout")
        if kind == "lost":
            raise ActionOutcomeUnknown("injected lost result")
        if kind == "error":
            raise RuntimeError("injected executor error")
        if kind == "hang":
            event = threading.Event()
            with self._lock:
                self._hanging.append(event)
            event.wait(self.HANG_LIMIT_S)
        return ActionResult(ok=True, code="ok", summary="mock executor: nothing was executed on any host")
