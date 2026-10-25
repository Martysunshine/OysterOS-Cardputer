"""Executor for the owner's own Windows PC (media keys, lock, health, services, Wake-on-LAN).

Every operating-system access goes through the small `OsLayer` protocol (the real one is `os_windows.py`),
so tests drive this class with a fake and never press a key or lock a screen. What this class decides:

- Exactly one host id is "this PC" (`local_host_id`, default `this-pc`). Every action naming another host
  is refused, except `wake_on_lan`, which by nature targets another machine and looks the host up in an
  allowlist (`host_id -> MAC`) that only configuration can fill. A MAC address never comes from a request,
  a profile or a job; the action model has no field for one.
- `service_restart` needs the service on an executor allowlist (configuration, in addition to the profile's
  own host/service list) and an already elevated process. Without administrator rights it fails with an
  honest `needs administrator rights`; nothing here ever asks for elevation (no UAC prompt).
- Results are honest: a refusal or a known OS failure is `ok=False` (the job ends `failed`); a result that
  cannot be told (the OS layer raises `ActionOutcomeUnknown`, or anything unexpected) propagates, so the
  runner ends an impactful job `unknown_outcome`. Summaries say what was done, never more: a key press is
  "sent", a magic packet is "sent", neither claims the player or the host reacted.
- No shell, no child process, no code evaluation, no path or command taken from a request: the operation is
  chosen from a fixed table by action type and arguments are discrete values.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from oyster.deck.actions import (
    ID_PATTERN,
    MEDIA_KEYS,
    SERVICE_PATTERN,
    ActionFailed,
    ActionResult,
    _Action,
    action_type,
)

DEFAULT_LOCAL_HOST_ID = "this-pc"
DEFAULT_WOL_BROADCAST = "255.255.255.255"
_ID = re.compile(ID_PATTERN)
_SERVICE = re.compile(SERVICE_PATTERN)
_MAC = re.compile(r"^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$")


class ExecutorConfigError(ValueError):
    """The executor configuration is invalid. The message never contains a configured value."""


class OsFailure(Exception):
    """The OS layer knows the operation did not take effect. `code` is a short machine code ([a-z0-9_]),
    `summary` a short text for the device screen (no secrets, no raw OS error text)."""

    def __init__(self, code: str, summary: str):
        super().__init__(code)
        self.code = code
        self.summary = summary


@dataclass(frozen=True)
class HostHealth:
    cpu_percent: float
    memory_percent: float
    disk_percent: float


@runtime_checkable
class OsLayer(Protocol):
    """All operating-system access of the executor. Methods raise `OsFailure` for a failure whose effect is
    known (nothing happened) and `ActionOutcomeUnknown` when the effect cannot be told."""

    def send_media_key(self, key: str) -> None:
        """Press and release one media key; `key` is one of `actions.MEDIA_KEYS`."""

    def lock_workstation(self) -> None: ...

    def host_health(self) -> HostHealth: ...

    def service_status(self, name: str) -> str | None:
        """The state of a Windows service ("running", "stopped", ...) or None when no such service exists."""

    def wake_on_lan(self, mac: str) -> None:
        """Send one magic packet for `mac` (normalised `aa:bb:cc:dd:ee:ff`) to the configured broadcast."""

    def is_elevated(self) -> bool: ...

    def restart_service(self, name: str) -> None:
        """Stop and start a service, waiting (bounded) for each state."""


# --- configuration -----------------------------------------------------------------------------------


def normalize_mac(text: str) -> str:
    """`AA-BB-CC-DD-EE-FF` or `aa:bb:...` -> lowercase colon form. Raises ValueError (text not echoed)."""
    if not _MAC.match(text):
        raise ValueError("not a MAC address")
    return text.replace("-", ":").lower()


def parse_wol_hosts(text: str) -> dict[str, str]:
    """`host_id=MAC;host_id=MAC` -> {host_id: normalised MAC}. Empty text is an empty allowlist."""
    out: dict[str, str] = {}
    for n, entry in enumerate((e.strip() for e in text.split(";")), 1):
        if not entry:
            continue
        host, sep, mac = entry.partition("=")
        host, mac = host.strip(), mac.strip()
        if not sep or not _ID.match(host):
            raise ExecutorConfigError(f"OYSTER_DECK_WOL_HOSTS entry {n}: expected host_id=MAC")
        try:
            out[host] = normalize_mac(mac)
        except ValueError:
            raise ExecutorConfigError(f"OYSTER_DECK_WOL_HOSTS entry {n}: invalid MAC address") from None
    return out


def parse_service_list(text: str) -> frozenset[str]:
    names = [n.strip() for n in text.split(",") if n.strip()]
    for n in names:
        if not _SERVICE.match(n):
            raise ExecutorConfigError("OYSTER_DECK_RESTART_SERVICES: invalid service name")
    return frozenset(n.lower() for n in names)  # Windows service names are case-insensitive


@dataclass(frozen=True)
class WindowsExecutorConfig:
    local_host_id: str = DEFAULT_LOCAL_HOST_ID
    wol_hosts: Mapping[str, str] = field(default_factory=dict)  # host_id -> MAC, from configuration only
    restart_services: frozenset[str] = frozenset()  # lowercase names the executor may restart
    wol_broadcast: str = DEFAULT_WOL_BROADCAST

    def __post_init__(self) -> None:
        if not _ID.match(self.local_host_id):
            raise ExecutorConfigError("OYSTER_DECK_LOCAL_HOST_ID: invalid host id")
        object.__setattr__(self, "wol_hosts", MappingProxyType(dict(self.wol_hosts)))
        try:
            ipaddress.IPv4Address(self.wol_broadcast)
        except ValueError:
            raise ExecutorConfigError("OYSTER_DECK_WOL_BROADCAST: not an IPv4 address") from None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WindowsExecutorConfig:
        return cls(
            local_host_id=(env.get("OYSTER_DECK_LOCAL_HOST_ID") or DEFAULT_LOCAL_HOST_ID).strip(),
            wol_hosts=parse_wol_hosts(env.get("OYSTER_DECK_WOL_HOSTS") or ""),
            restart_services=parse_service_list(env.get("OYSTER_DECK_RESTART_SERVICES") or ""),
            wol_broadcast=(env.get("OYSTER_DECK_WOL_BROADCAST") or DEFAULT_WOL_BROADCAST).strip(),
        )


# --- executor ----------------------------------------------------------------------------------------


def _refuse(code: str, summary: str) -> ActionResult:
    return ActionResult(ok=False, code=code, summary=summary)


class WindowsExecutor:
    """Runs validated actions on this PC through an `OsLayer`. Jobs carry `"executor": "windows"`."""

    kind = "windows"

    def __init__(self, os_layer: OsLayer, config: WindowsExecutorConfig | None = None):
        self.os = os_layer
        self.config = config or WindowsExecutorConfig()
        self._handlers = {
            "noop": self._noop,
            "host_health_check": self._health,
            "service_status": self._service_status,
            "wake_on_lan": self._wake,
            "service_restart": self._restart,
            "media_key": self._media_key,
            "lock_workstation": self._lock,
        }

    def execute(self, action: _Action) -> ActionResult:
        handler = self._handlers.get(action_type(action))
        if handler is None:
            raise ActionFailed("action type has no handler")
        try:
            return handler(action)
        except OsFailure as e:
            return ActionResult(ok=False, code=e.code, summary=e.summary)

    # --- helpers
    def _not_local(self, action) -> ActionResult | None:
        if getattr(action, "host_id", None) != self.config.local_host_id:
            return _refuse("host_not_local", "refused: this executor only controls this PC")
        return None

    # --- handlers
    def _noop(self, action) -> ActionResult:
        return ActionResult(ok=True, code="ok", summary="noop")

    def _media_key(self, action) -> ActionResult:
        refused = self._not_local(action)
        if refused:
            return refused
        key = action.key
        if key not in MEDIA_KEYS:
            return _refuse("bad_key", "refused: unknown media key")
        self.os.send_media_key(key)
        text = f"{key} key sent to this PC; player reaction not checked"
        return ActionResult(ok=True, code="ok", summary=text)

    def _lock(self, action) -> ActionResult:
        refused = self._not_local(action)
        if refused:
            return refused
        self.os.lock_workstation()
        return ActionResult(ok=True, code="ok", summary="lock request accepted by Windows")

    def _health(self, action) -> ActionResult:
        refused = self._not_local(action)
        if refused:
            return refused
        h = self.os.host_health()
        text = f"cpu {h.cpu_percent:.0f}% mem {h.memory_percent:.0f}% disk {h.disk_percent:.0f}%"
        return ActionResult(ok=True, code="ok", summary=text)

    def _service_status(self, action) -> ActionResult:
        refused = self._not_local(action)
        if refused:
            return refused
        state = self.os.service_status(action.service)
        if state is None:
            return _refuse("service_not_found", f"service {action.service} not found")
        return ActionResult(ok=True, code="ok", summary=f"{action.service}: {state}")

    def _wake(self, action) -> ActionResult:
        mac = self.config.wol_hosts.get(action.host_id)  # the only source of a MAC: configuration
        if mac is None:
            return _refuse("host_not_allowed", "refused: host is not on the wake-on-LAN allowlist")
        self.os.wake_on_lan(mac)
        return ActionResult(ok=True, code="ok", summary="magic packet sent; wake-up not verified")

    def _restart(self, action) -> ActionResult:
        refused = self._not_local(action)
        if refused:
            return refused
        if action.service.lower() not in self.config.restart_services:
            return _refuse("service_not_allowed", "refused: service is not on the restart allowlist")
        if not self.os.is_elevated():
            return _refuse("needs_admin", "needs administrator rights; nothing was restarted")
        self.os.restart_service(action.service)
        return ActionResult(ok=True, code="ok", summary=f"service {action.service} restarted, running again")
