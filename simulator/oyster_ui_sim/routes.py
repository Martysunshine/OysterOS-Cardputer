"""The route table: every screen of docs/DESIGN_SYSTEM.md ("Mockup screen IDs") plus the Oyster Pocket screens
that sit next to them, as data. Each route names its parent (Back), the targets of Enter, the Help target and
which module it belongs to; Home always goes to H01. `availability` mirrors docs/UI_SCREEN_MAP.md:
"implemented" screens exist in the firmware, "unavailable" ones open the "Not available in this build"
screen there and are only drawn as a mock-data preview when the simulator runs with preview=True.
The Linux (Hosts), Control Deck and Smart Home screens are implemented in the firmware but unverified on a
device; the simulator draws them from built-in mock data in both profiles (labelled SIMULATED), not from the
backend. Original Apps (O10) is the one unavailable module."""

from __future__ import annotations

from dataclasses import dataclass

STATES = ("normal", "loading", "error", "empty", "disabled")
KINDS = ("menu", "list", "detail", "progress", "text", "confirm", "message")
HOME = "H01"
HELP = "help"
IMPLEMENTED = "implemented"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Route:
    id: str
    title: str
    parent: str | None  # target of Back; None = the previous route on the stack (overlays)
    kind: str  # one of KINDS
    module: str  # home, security, ai, linux, deck, smart, original, settings, system
    availability: str = IMPLEMENTED
    enter: tuple[tuple[str, str], ...] = ()  # (item key, target route id) for Enter
    job: str = ""  # job kind shown by this route: wifi, ble, gatt, ai, deck, ir
    modal: bool = False  # drawn on the modal layer over its parent
    states: tuple[str, ...] = STATES  # supported state variants
    shortcuts: str = ""  # digit shortcuts advertised in the footer
    empty: str = "(empty)"  # text of the empty state
    error: str = ""  # text of the error state
    disabled: str = ""  # reason shown in the disabled state

    @property
    def back(self) -> str | None:
        return self.parent

    @property
    def home(self) -> str:
        return HOME

    @property
    def help(self) -> str:
        return HELP

    def target(self, key: str) -> str | None:
        return dict(self.enter).get(key)


def _r(*a, **k) -> Route:  # type: ignore[no-untyped-def]
    return Route(*a, **k)


ROUTES: tuple[Route, ...] = (
    _r(
        "H01",
        "OYSTER OS",
        None,
        "menu",
        "home",
        shortcuts="12345678",
        enter=(
            ("security", "S10"),
            ("ai", "A10"),
            ("linux", "L10"),
            ("deck", "D10"),
            ("smart", "SH10"),
            ("original", "O10"),
            ("settings", "P10"),
            ("help", HELP),
        ),
        empty="No modules",
        error="Start failed: see Settings > Diagnostics.",
        disabled="Unfinished modules are greyed out and open an explanation.",
    ),
    # ---- Security ---------------------------------------------------------------------------------------
    _r(
        "S10",
        "Security",
        "H01",
        "menu",
        "security",
        enter=(
            ("wifi", "S11"),
            ("ble", "S21"),
            ("findings", "findings"),
            ("targets", "targets"),
            ("assess", "assess"),
        ),
        empty="No tools",
        disabled="The radio tools are switched off in this simulated state.",
    ),
    _r(
        "S11",
        "Wi-Fi Survey",
        "S10",
        "list",
        "security",
        job="wifi",
        enter=(("results", "S12"), ("room", "room_entry")),
        empty="No survey yet",
        error="The survey did not complete. Nothing was saved.",
        disabled="The Wi-Fi radio is switched off in this simulated state.",
    ),
    _r(
        "S12",
        "Wi-Fi results",
        "S11",
        "list",
        "security",
        enter=(("detail", "S13"),),
        empty="No networks seen (2.4 GHz)",
        error="The result list could not be read.",
        disabled="No survey can run while the radio is off.",
    ),
    _r(
        "S13",
        "Access point",
        "S12",
        "detail",
        "security",
        empty="No access point selected.",
        error="The observation is no longer available.",
    ),
    _r(
        "S21",
        "Bluetooth Explorer",
        "S10",
        "list",
        "security",
        job="ble",
        enter=(("results", "S22"),),
        empty="No discovery yet",
        error="The discovery did not complete.",
        disabled="Bluetooth did not start.",
    ),
    _r(
        "S22",
        "BLE results",
        "S21",
        "list",
        "security",
        enter=(("detail", "S23"),),
        empty="No advertisers seen",
        error="The result list could not be read.",
    ),
    _r(
        "S23",
        "BLE advertiser",
        "S22",
        "detail",
        "security",
        empty="No advertiser selected.",
        error="The observation is no longer available.",
    ),
    _r("findings", "Saved Findings", "S10", "detail", "security", empty="No findings saved in this session."),
    _r(
        "targets",
        "Targets (hint)",
        "S10",
        "list",
        "security",
        enter=(("detail", "msg"), ("assess", "assess")),
        empty="No enrolled targets",
    ),
    _r(
        "assess",
        "Assess",
        "S10",
        "list",
        "security",
        job="gatt",
        enter=(("gatt", "gatt"), ("msg", "msg")),
        empty="No enrolled target",
        error="The inspection did not complete.",
    ),
    _r("gatt", "GATT results", "assess", "list", "security", empty="No GATT table yet"),
    # ---- Oyster AI --------------------------------------------------------------------------------------
    _r(
        "A10",
        "Oyster AI",
        "H01",
        "list",
        "ai",
        enter=(("ask", "ask_entry"), ("latest", "A11")),
        empty="Nothing here",
        error="The backend is not reachable.",
        disabled="The AI needs the backend.",
    ),
    _r("ask_entry", "Ask Oyster", "A10", "text", "ai", empty="", error="The question could not be sent."),
    _r(
        "A11",
        "Ask Oyster run",
        "A10",
        "message",
        "ai",
        job="ai",
        empty="No AI run yet in this boot.",
        error="The run did not complete.",
    ),
    _r(
        "A12",
        "Ask Oyster answer",
        "A10",
        "message",
        "ai",
        job="ai",
        empty="No AI run yet in this boot.",
        error="The run ended without an answer.",
    ),
    # ---- Linux ------------------------------------------------------------------------------------------
    _r(
        "L10",
        "Linux",
        "H01",
        "list",
        "linux",
        enter=(("host", "L11"),),
        empty="No hosts known",
        error="The companion is offline: host list not available.",
    ),
    _r(
        "L11",
        "Linux host",
        "L10",
        "list",
        "linux",
        enter=(("health", "msg"), ("services", "L12"), ("logs", "L13")),
        empty="No data from this host",
        error="The host did not answer.",
    ),
    _r(
        "L12",
        "Services",
        "L11",
        "list",
        "linux",
        enter=(("detail", "msg"),),
        empty="No services reported",
        error="The service list could not be read.",
    ),
    _r(
        "L13",
        "Logs",
        "L11",
        "message",
        "linux",
        empty="No log lines",
        error="The log could not be read.",
    ),
    # ---- Control Deck -----------------------------------------------------------------------------------
    _r(
        "D10",
        "Control Deck",
        "H01",
        "list",
        "deck",
        enter=(("macro", "D11"), ("history", "D22")),
        shortcuts="1234",
        empty="This profile has no macros",
        error="The profile could not be loaded.",
    ),
    _r(
        "D11",
        "Macro",
        "D10",
        "message",
        "deck",
        enter=(("confirm", "D20"), ("run", "D21")),
        error="The macro summary could not be loaded.",
    ),
    _r(
        "D20",
        "Confirm macro",
        "D11",
        "confirm",
        "deck",
        modal=True,
        error="The confirmation expired. Nothing was run.",
        empty="Nothing to confirm.",
    ),
    _r("D21", "Job", "D11", "progress", "deck", job="deck", empty="No job"),
    _r("D22", "Job history", "D10", "list", "deck", empty="No jobs yet"),
    # ---- Smart Home -------------------------------------------------------------------------------------
    _r(
        "SH10",
        "Rooms",
        "H01",
        "list",
        "smart",
        enter=(("room", "SH11"),),
        empty="No rooms",
        error="The home integration is not connected.",
    ),
    _r(
        "SH11",
        "Devices",
        "SH10",
        "list",
        "smart",
        enter=(("ir", "SH12"),),
        empty="No devices in this room",
        error="Device state could not be read.",
    ),
    _r(
        "SH12",
        "IR profiles",
        "SH11",
        "list",
        "smart",
        job="ir",
        empty="No IR profile set up",
        error="The IR transmitter reported an error.",
    ),
    # ---- Original Apps ----------------------------------------------------------------------------------
    _r(
        "O10",
        "Original Apps",
        "H01",
        "message",
        "original",
        UNAVAILABLE,
        empty="Nothing to launch",
        error="Original launcher integration unavailable in simulator",
    ),
    # ---- Settings ---------------------------------------------------------------------------------------
    _r(
        "P10",
        "Settings",
        "H01",
        "list",
        "settings",
        enter=(
            ("backend", "settings_backend"),
            ("storage", "settings_storage"),
            ("diagnostics", "settings_diag"),
            ("keyboard", "settings_keyboard"),
            ("keytest", "P11"),
            ("about", "settings_about"),
        ),
        empty="No settings",
    ),
    _r("P11", "Key tester [P11]", "P10", "detail", "settings", error="No key events can be read."),
    _r("settings_backend", "Backend", "P10", "detail", "settings"),
    _r("settings_storage", "Storage", "P10", "detail", "settings"),
    _r("settings_diag", "Diagnostics", "P10", "detail", "settings"),
    _r("settings_keyboard", "Keyboard", "P10", "detail", "settings"),
    _r("settings_about", "About", "P10", "detail", "settings"),
    # ---- overlays and helpers -----------------------------------------------------------------------------
    _r("room_entry", "Room label", "S11", "text", "security"),
    _r(HELP, "Help", None, "message", "system"),
    _r("msg", "Message", None, "message", "system"),
    _r(
        "confirm",
        "Confirm action",
        "A12",
        "confirm",
        "system",
        modal=True,
        error="The confirmation expired. Nothing was run.",
        empty="Nothing to confirm.",
    ),
)

BY_ID: dict[str, Route] = {r.id: r for r in ROUTES}

# The design's "Mockup screen IDs" (docs/DESIGN_SYSTEM.md), in document order.
DESIGN_IDS: tuple[str, ...] = (
    "H01",
    "S10",
    "S11",
    "S12",
    "S13",
    "S21",
    "S22",
    "S23",
    "A10",
    "A11",
    "A12",
    "L10",
    "L11",
    "L12",
    "L13",
    "D10",
    "D11",
    "D20",
    "D21",
    "D22",
    "SH10",
    "SH11",
    "SH12",
    "O10",
    "P10",
    "P11",
)

# The "Not available in this build" text of the modules the firmware does not implement
# (firmware/src/app_controller.cpp, kUnavailable). Only Original Apps is still unavailable; the Linux,
# Control Deck and Smart Home entries mirror the firmware table and are not shown (see views.unavailable).
UNAVAILABLE_TEXT: dict[str, tuple[str, str]] = {
    "linux": (
        "Linux: unavailable",
        "Not available in this build.\nHost health, services and logs need a companion host adapter that the "
        "backend does not provide. Nothing on this screen runs or connects.",
    ),
    "deck": (
        "Control Deck: unavailable",
        "Not available in this build.\nMacro profiles and a job runner for them do not exist on the device "
        "or "
        "the backend. No action can be started from here.",
    ),
    "smart": (
        "Smart Home: unavailable",
        "Not available in this build.\nThere are no IR profiles and no Home Assistant integration, so no "
        "device "
        "state is shown and nothing is switched.",
    ),
    "original": (
        "Original Apps: unavailable",
        "Not available in this build.\nThis firmware cannot start the original launcher or its apps and "
        "does not "
        "imitate them. That needs a verified launcher integration (docs/OysterOS_Project_Plan.md section 1).",
    ),
}


def route(route_id: str) -> Route:
    return BY_ID[route_id]


def ancestors(route_id: str) -> list[str]:
    """The Back chain from the root down to (not including) `route_id`."""
    chain: list[str] = []
    cur = BY_ID[route_id].parent
    while cur is not None:
        chain.append(cur)
        cur = BY_ID[cur].parent
    return chain[::-1]


def check_table() -> None:
    """Structural invariants of the table (also run by the tests)."""
    seen: set[str] = set()
    for r in ROUTES:
        if r.id in seen:
            raise ValueError(f"duplicate route {r.id}")
        seen.add(r.id)
        if r.kind not in KINDS:
            raise ValueError(f"{r.id}: unknown kind {r.kind}")
        if r.parent is not None and r.parent not in BY_ID:
            raise ValueError(f"{r.id}: unknown parent {r.parent}")
        for _, tgt in r.enter:
            if tgt not in BY_ID:
                raise ValueError(f"{r.id}: unknown Enter target {tgt}")
    for rid in DESIGN_IDS:
        if rid not in BY_ID:
            raise ValueError(f"design screen {rid} has no route")
