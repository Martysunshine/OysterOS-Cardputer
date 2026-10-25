"""Built-in mock data. Everything the simulator shows comes from here: there is no radio, no backend and no
network. Addresses are locally administered placeholders; names are invented."""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_SSID_BYTES = 32  # oyster::cfg::kMaxSsidBytes
MAX_BLE_NAME_BYTES = 29  # oyster::cfg::kMaxBleNameBytes
MAX_ROOM_LABEL_LEN = 32  # oyster::cfg::kMaxRoomLabelLen
MAX_QUESTION_LEN = 240  # oyster::cfg::kMaxQuestionLen


@dataclass(frozen=True)
class AccessPoint:
    ssid: bytes
    bssid: str
    channel: int
    rssi: int
    auth: str  # short code as shown in the list (OPEN, WPA2, W2/3, ...)
    auth_long: str


@dataclass(frozen=True)
class BleAdvertiser:
    name: bytes
    addr: str
    addr_type: str
    rssi: int
    connectable: bool | None
    services: int
    seen: int


@dataclass(frozen=True)
class Host:
    host_id: str
    name: str
    online: bool
    cpu: int
    mem: int
    disk: int
    uptime_h: int
    last_seen: str
    services: tuple[tuple[str, str], ...]
    logs: tuple[str, ...]


@dataclass(frozen=True)
class Macro:
    slot: int
    macro_id: str
    title: str
    host: str
    impact: str
    steps: int
    requires_confirm: bool
    read_only: bool


@dataclass(frozen=True)
class Room:
    name: str
    devices: tuple[tuple[str, str, bool], ...]  # (device, state, available)


@dataclass(frozen=True)
class AiAnswer:
    text: str
    pending: tuple[str, str, str] | None = None  # (operation, target, effect)


@dataclass
class Fixtures:
    wifi: list[AccessPoint] = field(default_factory=list)
    ble: list[BleAdvertiser] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)
    macros: list[Macro] = field(default_factory=list)
    rooms: list[Room] = field(default_factory=list)
    ir_profiles: tuple[str, ...] = ()
    history: list[tuple[str, str, str]] = field(default_factory=list)  # (job id, macro title, state)
    targets: list[tuple[str, str]] = field(default_factory=list)  # (target id, profile)
    gatt: list[str] = field(default_factory=list)
    battery_pct: int = 87
    session: str = "a1b2c3"
    profile_name: str = "Default"


def ai_answer(question: str) -> AiAnswer:
    """Canned answers: the simulator has no model. A question about GATT also carries a pending action."""
    q = question.lower()
    if "gatt" in q or "assess" in q:
        return AiAnswer(
            "Mock answer. The lab peripheral advertises one service. Inspecting its GATT table needs your "
            "local confirmation first; nothing runs until you approve the dialog.",
            ("ble_inspect_gatt", "lab-01", "Connects to the enrolled lab peripheral and lists its services."),
        )
    return AiAnswer(
        "Mock answer from the simulator, not a model. The last survey saw 9 networks; the strongest is "
        "OfficeWiFi at -54 dBm on channel 6. Two networks are open. Nothing was sent anywhere."
    )


def default_fixtures() -> Fixtures:
    def ap(ssid: str | bytes, n: int, ch: int, rssi: int, auth: str, long: str) -> AccessPoint:
        raw = ssid.encode() if isinstance(ssid, str) else ssid
        return AccessPoint(raw, f"02:00:00:00:00:{n:02x}", ch, rssi, auth, long)

    wifi = [
        ap("OfficeWiFi", 1, 6, -54, "WPA2", "WPA2_PSK"),
        ap("Guest", 2, 1, -72, "WPA2", "WPA2_PSK"),
        ap("IoT", 3, 11, -81, "WPA", "WPA_PSK"),
        ap("Cafe Free Wifi", 4, 6, -77, "OPEN", "OPEN"),
        ap("A-very-long-network-name-that-overflows-the-row", 5, 3, -69, "W2/3", "WPA2_WPA3_PSK"),
        ap("", 6, 9, -88, "WPA2", "WPA2_PSK"),
        ap(b"\xc3\xa9t\xc3\xa9-5G", 7, 4, -65, "WPA3", "WPA3_PSK"),
        ap("CorpNet", 8, 13, -60, "ENT", "WPA2_ENTERPRISE"),
        ap("printer-3F", 9, 8, -84, "OPEN", "OPEN"),
    ]
    ble = [
        BleAdvertiser(b"lab-01", "02:00:00:00:10:01", "public", -48, True, 1, 14),
        BleAdvertiser(b"Heart Sensor", "02:00:00:00:10:02", "random", -63, True, 2, 7),
        BleAdvertiser(b"", "02:00:00:00:10:03", "random", -79, None, 0, 3),
        BleAdvertiser(b"A-device-with-a-long-name-xxxxx", "02:00:00:00:10:04", "random", -71, False, 1, 5),
        BleAdvertiser(b"Beacon-7", "02:00:00:00:10:05", "random", -86, False, 0, 2),
        BleAdvertiser(b"Speaker", "02:00:00:00:10:06", "public", -58, True, 1, 9),
    ]
    hosts = [
        Host(
            "host-1",
            "MINI-PC",
            True,
            12,
            41,
            63,
            96,
            "now",
            (("ssh", "active"), ("nginx", "active"), ("backup", "failed")),
            (
                "12:00:01 sshd: session opened",
                "12:00:09 nginx: 200 GET /",
                "12:01:15 backup: exit code 2",
                "12:01:16 backup: unit entered failed state",
                "12:03:40 cron: job done",
            ),
        ),
        Host("host-2", "NAS-01", False, 0, 0, 0, 0, "12:00", (), ()),
    ]
    macros = [
        Macro(1, "get_server_status", "Server status", "MINI-PC", "reads status only", 1, False, True),
        Macro(2, "start_dev_stack", "Start dev stack", "MINI-PC", "starts services", 3, True, False),
        Macro(3, "get_site_status", "Red Orca status", "MINI-PC", "reads status only", 1, False, True),
        Macro(4, "run_backup", "Backup", "NAS-01", "writes a backup archive", 2, True, False),
    ]
    rooms = [
        Room(
            "Living room", (("Lamp", "on", True), ("Fan", "unknown", False), ("TV (IR)", "no feedback", True))
        ),
        Room("Office", (("Desk lamp", "off", True), ("Heater", "unavailable", False))),
        Room("Bedroom", ()),
    ]
    return Fixtures(
        wifi=wifi,
        ble=ble,
        hosts=hosts,
        macros=macros,
        rooms=rooms,
        ir_profiles=("TV power", "TV volume +", "TV volume -", "Fan speed"),
        history=[
            ("job-0003", "Backup", "success"),
            ("job-0002", "Start dev stack", "failed"),
            ("job-0001", "Server status", "success"),
        ],
        targets=[("lab-01", "oyster-lab-01")],
        gatt=["S oyster-lab service", " C status R", " C command W", "   D 2902"],
    )
