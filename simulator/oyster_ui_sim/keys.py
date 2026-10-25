"""Keyboard emulation: a port of firmware/lib/oyster_core/src/input_map.cpp (physical keys -> logical events)
and the logical action set of docs/OysterOS_Project_Plan.md section 4.

The bindings are the firmware's PROPOSED bindings and are UNVERIFIED on hardware (the Key tester, route P11,
exists to confirm them). KEY_TABLE is the documented table; tests parse input_map.cpp and compare."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class NavEvent(enum.Enum):
    NONE = "None"
    UP = "Up"
    DOWN = "Down"
    LEFT = "Left"
    RIGHT = "Right"
    SELECT = "Select"
    BACK = "Back"
    CANCEL = "Cancel"
    CHAR = "Char"
    ERASE = "Erase"
    TAB = "Tab"
    HOME = "Home"
    HELP = "Help"


class Action(enum.Enum):
    """Logical actions (plan section 4). TEXT_INPUT carries a character; SHORTCUT a digit."""

    MOVE_UP = "MOVE_UP"
    MOVE_DOWN = "MOVE_DOWN"
    MOVE_LEFT = "MOVE_LEFT"
    MOVE_RIGHT = "MOVE_RIGHT"
    SELECT = "SELECT"
    BACK = "BACK"
    HOME = "HOME"
    HELP = "HELP"
    TEXT_INPUT = "TEXT_INPUT"
    ERASE = "ERASE"
    SHORTCUT = "SHORTCUT"
    CANCEL = "CANCEL"
    CONFIRM = "CONFIRM"
    TAB = "TAB"


EVENT_TO_ACTION = {
    NavEvent.UP: Action.MOVE_UP,
    NavEvent.DOWN: Action.MOVE_DOWN,
    NavEvent.LEFT: Action.MOVE_LEFT,
    NavEvent.RIGHT: Action.MOVE_RIGHT,
    NavEvent.SELECT: Action.SELECT,
    NavEvent.BACK: Action.BACK,
    NavEvent.CANCEL: Action.CANCEL,
    NavEvent.CHAR: Action.TEXT_INPUT,
    NavEvent.ERASE: Action.ERASE,
    NavEvent.TAB: Action.TAB,
    NavEvent.HOME: Action.HOME,
    NavEvent.HELP: Action.HELP,
}


@dataclass
class KeySnapshot:
    """What BoardSupport reports per key change (oyster::KeySnapshot)."""

    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    enter: bool = False
    esc: bool = False
    backspace: bool = False
    del_: bool = False
    tab: bool = False
    space: bool = False
    fn: bool = False
    shift: bool = False
    ctrl: bool = False
    opt: bool = False
    alt: bool = False
    chars: str = field(default="")


MAX_EVENTS = 8
_ARROWS = {";": NavEvent.UP, ".": NavEvent.DOWN, ",": NavEvent.LEFT, "/": NavEvent.RIGHT}


def _printable(c: str) -> bool:
    return " " <= c <= "~"


def map_keys(k: KeySnapshot, text_mode: bool = False) -> list[tuple[NavEvent, str]]:
    """InputMapper::map. Cancel, when present, is the only event; Home is the only event otherwise."""
    if (
        (k.fn and k.esc)
        or (k.fn and ("`" in k.chars or "~" in k.chars))
        or (k.ctrl and ("c" in k.chars or "C" in k.chars))
    ):
        return [(NavEvent.CANCEL, "")]
    if k.fn and (k.backspace or k.del_):
        return [(NavEvent.HOME, "")]
    out: list[tuple[NavEvent, str]] = []

    def push(e: NavEvent, c: str = "") -> None:
        if len(out) >= MAX_EVENTS or (e is not NavEvent.CHAR and any(o[0] is e for o in out)):
            return
        out.append((e, c if e is NavEvent.CHAR else ""))

    for flag, ev in (
        (k.up, NavEvent.UP),
        (k.down, NavEvent.DOWN),
        (k.left, NavEvent.LEFT),
        (k.right, NavEvent.RIGHT),
        (k.enter, NavEvent.SELECT),
        (k.esc, NavEvent.BACK),
        (k.tab, NavEvent.TAB),
    ):
        if flag:
            push(ev)
    if k.backspace or k.del_:
        push(NavEvent.ERASE if text_mode else NavEvent.BACK)
    for c in k.chars[:MAX_EVENTS]:
        arrow = _ARROWS.get(c, NavEvent.NONE)
        if k.fn:  # Fn layer: arrows in both modes; other Fn chords are reserved and ignored
            if arrow is not NavEvent.NONE:
                push(arrow)
            continue
        if k.ctrl or k.opt or k.alt:  # modifier chords are never text or navigation
            continue
        if not text_mode:
            if arrow is not NavEvent.NONE:
                push(arrow)
                continue
            if c == "`":
                push(NavEvent.BACK)
                continue
            if c == "?":
                push(NavEvent.HELP)
                continue
        if _printable(c):
            push(NavEvent.CHAR, c)
    return out


def describe_snapshot(k: KeySnapshot, events: list[tuple[NavEvent, str]]) -> tuple[str, str]:
    """The two strings of the Key tester (keytest_record): physical keys and the resolved actions."""
    flags = (
        (k.fn, "Fn"),
        (k.ctrl, "Ctrl"),
        (k.shift, "Shift"),
        (k.opt, "Opt"),
        (k.alt, "Alt"),
        (k.esc, "Esc"),
        (k.enter, "Enter"),
        (k.backspace, "Del"),
        (k.del_, "DelFwd"),
        (k.tab, "Tab"),
        (k.space, "Space"),
        (k.up, "Fn-Up"),
        (k.down, "Fn-Down"),
        (k.left, "Fn-Left"),
        (k.right, "Fn-Right"),
    )
    keys = "".join(f"{name} " for on, name in flags if on)
    for c in k.chars[:MAX_EVENTS]:
        keys += f"'{c}' " if "!" <= c <= "~" else f"0x{ord(c):02x} "
    action = "".join("Char " if e is NavEvent.CHAR else f"{e.value} " for e, _ in events[:MAX_EVENTS])
    return (keys.strip() or "(released)"), (action.strip() or "none")


def is_key_edge(k: KeySnapshot, events: list[tuple[NavEvent, str]]) -> bool:
    """Modifier-only changes keep the last Key tester result on screen."""
    edge = (
        k.esc
        or k.enter
        or k.backspace
        or k.del_
        or k.tab
        or k.space
        or k.up
        or k.down
        or k.left
        or k.right
        or bool(k.chars)
    )
    return edge or bool(events)


# ---- documented binding table ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Binding:
    action: str  # Action name, or SHORTCUT_1-8
    keys: tuple[str, ...]  # physical chords (script syntax), the proposed bindings
    shows: str  # text for the legend
    scope: str
    pc: str  # extra desktop shortcut of the emulator window


KEY_TABLE: tuple[Binding, ...] = (
    Binding("MOVE_UP", (";", "fn+;"), "; (Fn+; anywhere)", "bare in menus, Fn layer anywhere", "Up arrow"),
    Binding(
        "MOVE_DOWN", (".", "fn+."), ". (Fn+. anywhere)", "bare in menus, Fn layer anywhere", "Down arrow"
    ),
    Binding("MOVE_LEFT", (",", "fn+,"), ", (Fn+, anywhere)", "page / switch column / adjust", "Left arrow"),
    Binding("MOVE_RIGHT", ("/", "fn+/"), "/ (Fn+/ anywhere)", "page / switch column / adjust", "Right arrow"),
    Binding("SELECT", ("enter",), "Enter", "open / run / approve", "Return"),
    Binding("BACK", ("`", "del"), "` or Del", "navigation mode; a modal is answered first", "Esc, Backspace"),
    Binding("HOME", ("fn+del",), "Fn+Del (Fn first)", "any mode; a modal is rejected first", "Home key"),
    Binding("HELP", ("?",), "? (Shift+/)", "navigation mode; not in text entry or over a modal", "F1"),
    Binding(
        "CANCEL",
        ("fn+`", "ctrl+c"),
        "Fn+` or Ctrl+C",
        "any screen: cancels the task, rejects a modal",
        "Ctrl+C",
    ),
    Binding("ERASE", ("del",), "Del", "text entry; on an empty buffer it leaves", "Backspace"),
    Binding("TEXT_INPUT", ("a",), "printable keys", "text entry (hotkeys in menus)", "keyboard"),
    Binding("SHORTCUT_1-8", ("1", "8"), "1-8", "Home only, advertised in the footer", "digits"),
)

# ---- chord parsing (scripts, legend self-test, emulator) ------------------------------------------------

_NAMED = {
    "enter": "enter",
    "return": "enter",
    "esc": "esc",
    "escape": "esc",
    "tab": "tab",
    "space": "space",
    "del": "backspace",
    "backspace": "backspace",
    "delfwd": "del",
    "delete": "del",
}
_MODS = {"fn": "fn", "shift": "shift", "ctrl": "ctrl", "opt": "opt", "alt": "alt"}
# Logical aliases for scripts: the simplest snapshot that resolves to the action in navigation mode.
_LOGICAL = {
    "up": {"up": True},
    "down": {"down": True},
    "left": {"left": True},
    "right": {"right": True},
    "select": {"enter": True},
    "back": {"esc": True},
    "home": {"fn": True, "backspace": True},
    "help": {"chars": "?"},
    "cancel": {"fn": True, "chars": "`"},
}


class ChordError(ValueError):
    """A key chord in a script could not be understood."""


def parse_chord(text: str) -> KeySnapshot:
    """'fn+del', 'ctrl+c', 'shift+/', ';', 'enter', 'home' -> KeySnapshot."""
    raw = text.strip()
    if not raw:
        raise ChordError("empty key chord")
    low = raw.lower()
    if low in _LOGICAL:
        return KeySnapshot(**_LOGICAL[low])  # type: ignore[arg-type]
    if raw == "+":
        return KeySnapshot(chars="+")
    if raw.endswith("++"):  # the '+' key itself: "fn++"
        parts = [*raw[:-2].split("+"), "+"]
    else:
        parts = raw.split("+") if len(raw) > 1 else [raw]
    if "" in parts:
        raise ChordError(f"incomplete key chord {text!r}")
    k = KeySnapshot()
    for i, part in enumerate(parts):
        p = part.lower()
        last = i == len(parts) - 1
        if not last:
            if p not in _MODS:
                raise ChordError(f"unknown modifier {part!r} in {text!r}")
            setattr(k, _MODS[p], True)
        elif p in _NAMED:
            setattr(k, _NAMED[p], True)
        elif len(part) == 1:
            ch = part
            if k.shift and ch == "/":
                ch = "?"
            k.chars = ch
        else:
            raise ChordError(f"unknown key {part!r} in {text!r}")
    return k


def snapshot_from_pc_key(keysym: str, char: str, ctrl: bool, shift: bool, fn: bool) -> KeySnapshot | None:
    """Desktop window: a tkinter key press -> the snapshot the Cardputer would report (None = ignore)."""
    pc = {
        "Up": {"up": True},
        "Down": {"down": True},
        "Left": {"left": True},
        "Right": {"right": True},
        "Return": {"enter": True},
        "KP_Enter": {"enter": True},
        "Escape": {"esc": True},
        "BackSpace": {"backspace": True},
        "Delete": {"backspace": True},
        "Tab": {"tab": True},
        "Home": {"fn": True, "backspace": True},
        "F1": {"chars": "?"},
    }
    if ctrl and keysym.lower() == "c":
        return KeySnapshot(ctrl=True, chars="c")
    if keysym in pc:
        k = KeySnapshot(**pc[keysym])  # type: ignore[arg-type]
        k.fn = k.fn or fn
        return k
    if len(char) == 1 and _printable(char):
        return KeySnapshot(fn=fn, shift=shift, chars=char, space=char == " ")
    return None
