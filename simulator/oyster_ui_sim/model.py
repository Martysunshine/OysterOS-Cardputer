"""Plain data shared by the engine and the view builders."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .widgets import Conn, Tile


class ConnState(enum.Enum):
    """Simulated connectivity: the status bar and every backend-bound view derive from it."""

    OFFLINE = "offline"
    CONNECTING = "connect"
    ONLINE = "online"
    NO_CLOCK = "no-clock"
    CERT_ERROR = "cert-err"
    AUTH_ERROR = "auth-err"  # "unauthorized"
    INIT_ERROR = "init-err"

    @property
    def short(self) -> str:
        """backend_short(): the label in the status bar."""
        return self.value

    @property
    def icon(self) -> Conn:
        if self is ConnState.ONLINE:
            return Conn.ONLINE
        if self is ConnState.CONNECTING:
            return Conn.CONNECTING
        if self is ConnState.OFFLINE:
            return Conn.OFFLINE
        return Conn.ERROR

    @property
    def explanation(self) -> str:
        return {
            ConnState.OFFLINE: "The companion backend is offline.",
            ConnState.CONNECTING: "Still connecting to the companion backend.",
            ConnState.ONLINE: "The companion backend is online.",
            ConnState.NO_CLOCK: "The device clock is not set, so the backend cannot be trusted yet.",
            ConnState.CERT_ERROR: "The backend certificate was not accepted.",
            ConnState.AUTH_ERROR: "The backend refused this device (unauthorized).",
            ConnState.INIT_ERROR: "The backend client did not start.",
        }[self]


@dataclass
class ListNav:
    sel: int = 0
    top: int = 0


@dataclass
class Row:
    """A focusable row or tile. `to` opens a route, `do` runs a named handler."""

    text: str
    to: str | None = None
    do: str | None = None
    arg: Any = None
    enabled: bool = True
    detail: str = ""
    icon: str | None = None
    shortcut: str = ""


@dataclass
class View:
    kind: str  # list | tiles | message | progress | confirm | entry
    title: str
    rows: list[Row] = field(default_factory=list)
    empty: str = "(empty)"
    columns: int = 1
    right: str = ""
    body: str = ""
    label: str = ""  # progress
    elapsed_s: int = 0
    items: int = 0
    cancellable: bool = True
    op: str = ""  # confirm
    target: str = ""
    effect: str = ""
    footer: tuple[str, str] | None = None
    shortcuts: str = ""
    state: str = "normal"
    selected: int = 0
    top: int = 0
    scroll: int = 0
    total_lines: int = 0
    entry: Any = None

    @property
    def alert(self) -> bool:
        return self.title.startswith("!")

    @property
    def tiles(self) -> list[Tile]:
        return [Tile(r.icon, r.text, r.detail, r.enabled) for r in self.rows]


@dataclass
class Modal:
    route: str  # "D20" or "confirm"
    operation: str
    target: str
    effect: str
    shown_ms: int
    ttl_ms: int = 30000
    on_approve: str = ""
    arg: Any = None
    guard_ms: int = 700  # firmware kConfirmGuardMs: keys typed just before the dialog appears never answer it

    @property
    def guard_until(self) -> int:
        return self.shown_ms + self.guard_ms


@dataclass
class Task:
    kind: str  # wifi | ble | gatt | ai | deck
    label: str
    route: str  # route that started it (shows its progress)
    started: int
    duration: int
    outcome: str  # success | failed | timed_out | unknown
    arg: Any = None
    cancelled: bool = False

    def phase(self, now: int) -> str:
        if self.kind == "deck":
            return "queued" if now - self.started < 600 else "running"
        return "running"


@dataclass
class Run:
    run_id: str
    question: str
    status: str  # queued | running | succeeded | failed | cancelled | ai_unavailable
    answer: str = ""
    pending: tuple[str, str, str] | None = None


@dataclass
class Job:
    job_id: str
    macro: str
    title: str
    host: str
    state: str  # queued | running | success | failed | timed_out | cancelled | unknown
    idem: str = ""
    final: str = ""  # what the mock backend really did (revealed by a status check)


@dataclass
class Entry:
    purpose: str  # room | question
    buffer: str = ""
    cursor: int = 0
    max_len: int = 32


@dataclass
class KeyTest:
    keys: str = ""
    action: str = ""
    before: str = ""
    presses: int = 0
    armed: str = ""
    armed_until: int = 0
