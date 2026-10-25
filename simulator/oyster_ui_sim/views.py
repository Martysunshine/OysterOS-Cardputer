"""View builders: route + state -> what the screen shows. Texts and row formats follow
firmware/src/app_controller.cpp where the firmware has the screen; the screens the firmware does not have
(Linux, Control Deck, Smart Home) are drawn from mock data only when the simulator runs with preview=True.

Every route supports the five states (routes.STATES). A builder handles the states it knows itself; the others
fall back to `generic()`, so "loading / error / empty / disabled" are always drawn the same way."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from . import fixtures as fx
from .model import Row, View
from .routes import HELP, UNAVAILABLE, UNAVAILABLE_TEXT, Route

if TYPE_CHECKING:
    from .engine import Simulator

BACKEND_MODULES = ("ai", "linux", "deck", "smart")
HOME_TILES = (  # (key, label, icon, number, module), row-major as kHome in app_controller.cpp
    ("security", "Security", "Security", 1, "security"),
    ("deck", "Control", "ControlDeck", 4, "deck"),
    ("ai", "Oyster AI", "OysterAi", 2, "ai"),
    ("smart", "SmartHome", "SmartHome", 5, "smart"),
    ("linux", "Linux", "Linux", 3, "linux"),
    ("original", "Original", "Apps", 6, "original"),
    ("settings", "Settings", "Settings", 7, "settings"),
    ("help", "Help", None, 8, "system"),
)
HINTS = {  # screen_hint() of the firmware, extended to the simulator's routes
    "H01": "Arrows move between tiles, Enter opens, 1-8 open directly.",
    "S10": "Pick a tool. Enter opens it.",
    "S11": "Start a passive 2.4 GHz survey, then open the results. The room label is stored with it.",
    "S12": "Enter: detail. s: sort by RSSI or channel. , /: page.",
    "S13": "; . scroll, , / page. Back returns to the list.",
    "S21": "Passive discovery only listens; active discovery sends scan requests.",
    "S22": "Enter: detail. , /: page.",
    "S23": "; . scroll, , / page. Back returns to the list.",
    "A10": "Ask a question, read the latest run, cancel it or sync.",
    "A11": "Status and answer pages. ; . scroll.",
    "A12": "Status and answer pages. ; . scroll.",
    "P10": "Enter opens an entry. , / adjust the brightness.",
    "P11": "Shows the physical key and the resolved action. Back or Home twice leaves.",
    "L10": "Pick a host. Read-only inspection never asks for confirmation.",
    "D10": "Slots run nothing by themselves: a slot opens its summary first.",
    "D11": "Enter runs the macro; impactful macros ask for confirmation first.",
    "SH10": "Pick a room. Device state comes only from the integration.",
    "O10": "The original launcher is not available from here.",
}
KEYS_TEXT = (
    "Proposed keys, NOT yet confirmed on hardware:\n"
    "Up/Down: ; .  (Fn+; . anywhere)\n"
    "Left/Right: , /  (page, adjust)\n"
    "Select: Enter\n"
    "Back: ` or Del. A dialog is answered 'reject' first.\n"
    "Home: Fn+Del (press Fn first). Back to the root.\n"
    "Help: ? (Shift+/). Not in text entry.\n"
    "Cancel a task: Fn+` or Ctrl+C, on every screen.\n"
    "Text entry: Del erases, Del on empty leaves, Fn+, / move the caret.\n"
    "Home screen: 1-8 open a tile.\n"
    "Settings > Key tester shows what each key does. Record results in docs/HARDWARE_VERIFICATION.md."
)


# ---- helpers -----------------------------------------------------------------------------------------------


def ascii_text(raw: bytes, empty: str) -> str:
    """Untrusted SSID / BLE name bytes -> display text (never assumed to be UTF-8)."""
    if not raw:
        return empty
    return raw.decode("ascii") if all(0x20 <= b <= 0x7E for b in raw) else "<non-ASCII>"


def _msg(title: str, body: str, state: str = "normal", footer: tuple[str, str] | None = None) -> View:
    return View("message", title, body=body, state=state, footer=footer)


def unavailable(module: str, rt: Route) -> View:
    title, body = UNAVAILABLE_TEXT.get(
        module,
        (
            f"{rt.title}: unavailable",
            "Not available in this build.\n"
            + (
                rt.disabled
                or "This feature is switched off in the simulated state. Nothing here runs or connects."
            ),
        ),
    )
    return _msg(title, body, "disabled")


def generic(sim: Simulator, rt: Route, state: str) -> View:
    """loading / error / empty / disabled for any route."""
    if state == "loading":
        return View(
            "progress",
            "IN PROGRESS",
            label=f"Loading {rt.title}",
            elapsed_s=sim.elapsed_s(),
            state=state,
            footer=("Del:Back", "Fn+`:Cancel"),
        )
    if state == "error" and rt.module in ("linux", "deck", "smart") and not sim.online:
        return _offline(rt.title, sim)
    if state == "error":
        why = rt.error or "This view could not be loaded. Nothing was changed."
        if rt.module in BACKEND_MODULES:
            why += f"\nBackend: {sim.conn.short}. {sim.conn.explanation}"
        return _msg(
            f"!{rt.title}: error",
            why + "\nEnter retries. Back returns, Home goes to the root.",
            "error",
            ("!Del:Back Ent:Retry", ""),
        )
    if state == "empty":
        if rt.kind in ("menu", "list"):
            return View("list", rt.title, rows=[], empty=rt.empty, state="empty")
        return _msg(rt.title, rt.empty, "empty")
    return unavailable(rt.module, rt)


# ---- Home and Security -----------------------------------------------------------------------------------


def home(sim: Simulator, rt: Route, state: str) -> View:
    rows: list[Row] = []
    for key, label, icon, number, module in HOME_TILES:
        built = module in ("home", "security", "ai", "settings", "system")
        enabled = built or (sim.preview and state != "disabled")
        if key == "security":
            what = "scanning" if sim.task and sim.task.kind in ("wifi", "ble") else "Wi-Fi, BLE"
        elif key == "ai":
            what = sim.conn.short
        elif key == "settings":
            what = "device"
        elif key == "help":
            what = "keys"
        else:
            what = "preview" if enabled else "unavailable"
        rows.append(
            Row(
                label,
                to=rt.target(key),
                enabled=enabled,
                detail=f"{number} {what}",
                icon=icon,
                shortcut=str(number),
            )
        )
    title = "OYSTER OS"
    if state == "error" or sim.faults:
        title = "!FAILED: " + ",".join(sorted(f.upper() for f in sim.faults or {"BLE"}))
    return View("tiles", title, rows=rows, columns=2, shortcuts="12345678", state=state)


def security(sim: Simulator, rt: Route, state: str) -> View:
    radio_off = state == "disabled"
    ble_fault = "ble" in sim.faults
    wifi_n = len(sim.wifi_results) if sim.wifi_results is not None else None
    ble_n = len(sim.ble_results) if sim.ble_results is not None else None
    rows = [
        Row(
            "Wi-Fi Survey",
            to=rt.target("wifi"),
            icon="Wifi",
            enabled=not radio_off,
            detail="radio off"
            if radio_off
            else ("passive 2.4 GHz survey" if wifi_n is None else f"{wifi_n} networks in the last survey"),
        ),
        Row(
            "Bluetooth Explorer",
            to=rt.target("ble"),
            icon="Ble",
            enabled=not (radio_off or ble_fault),
            detail="BLE did not start"
            if (radio_off or ble_fault)
            else ("advertisement discovery" if ble_n is None else f"{ble_n} advertisers in the last scan"),
        ),
        Row(
            "Saved Findings",
            to=rt.target("findings"),
            icon="Findings",
            detail="evidence log and upload state",
        ),
        Row(
            "Enrolled Targets",
            to=rt.target("targets"),
            icon="Targets",
            detail=f"{len(sim.fixtures.targets)} enrolled (your own devices)",
        ),
        Row("Assess (GATT)", to=rt.target("assess"), icon="Assess", detail="authorized GATT checks"),
    ]
    return View("tiles", "Security", rows=rows, columns=1, state=state)


# ---- Wi-Fi -------------------------------------------------------------------------------------------


def _progress(sim: Simulator, label: str, state: str = "loading") -> View:
    t = sim.task
    return View(
        "progress",
        "IN PROGRESS",
        label=label,
        elapsed_s=sim.elapsed_s(t.started if t else None),
        items=sim.task_items(),
        cancellable=True,
        state=state,
    )


def wifi_menu(sim: Simulator, rt: Route, state: str) -> View:
    if state == "loading" or (sim.task and sim.task.kind == "wifi" and sim.task.route == rt.id):
        return _progress(sim, "Wi-Fi survey")
    n = len(sim.wifi_results or [])
    rows = [
        Row("Start survey (passive)", do="start_wifi"),
        Row(f"Results ({n})", to=rt.target("results")),
        Row(f"Room: {sim.room or '(none)'}", to=rt.target("room")),
    ]
    return View("list", rt.title, rows=rows, state=state)


def sorted_wifi(sim: Simulator) -> list[fx.AccessPoint]:
    res = list(sim.wifi_results or [])
    return sorted(
        res, key=(lambda a: -a.rssi) if sim.wifi_sort == "rssi" else (lambda a: (a.channel, -a.rssi))
    )


def wifi_results(sim: Simulator, rt: Route, state: str) -> View:
    none_yet = sim.wifi_results is None
    rows = (
        []
        if state == "empty"
        else [
            Row(
                f"{a.rssi:4d} {a.channel:2d} {a.auth:<4s} {ascii_text(a.ssid, '<hidden>')}",
                to=rt.target("detail"),
                arg=i,
            )
            for i, a in enumerate(sorted_wifi(sim))
        ]
    )
    title = (
        "Wi-Fi: no survey yet" if none_yet else f"Wi-Fi {'by RSSI' if sim.wifi_sort == 'rssi' else 'by ch'}"
    )
    return View("list", title, rows=rows, empty="Start a survey first" if none_yet else rt.empty, state=state)


def wifi_detail(sim: Simulator, rt: Route, state: str) -> View:
    aps = sorted_wifi(sim)
    if not aps:
        return generic(sim, rt, "empty")
    a = aps[min(sim.selected.get("ap", 0), len(aps) - 1)]
    sl = len(a.ssid)
    lines = [f"SSID: {ascii_text(a.ssid, '<hidden or empty>')}"]
    if sl:
        lines.append(f"SSID hex: {a.ssid.hex()}")
    lines += [
        f"BSSID: {a.bssid}",
        f"Channel: {a.channel} (2.4 GHz)",
        f"RSSI: {a.rssi} dBm",
        f"Auth: {a.auth_long}",
        "WPS/PMF: unknown (not obtained)",
        f"Room: {sim.room or '(none)'}",
        "Scan: passive, 4000 ms (simulated)",
        "Built-in radio: 2.4 GHz only; 5/6 GHz networks are not visible.",
    ]
    return _msg(rt.title, "\n".join(lines), state)


# ---- Bluetooth --------------------------------------------------------------------------------------------


def ble_menu(sim: Simulator, rt: Route, state: str) -> View:
    fault = "ble" in sim.faults or state == "disabled"
    if state == "loading" or (sim.task and sim.task.kind == "ble" and sim.task.route == rt.id):
        return _progress(
            sim, "BLE discovery (active)" if sim.task and sim.task.arg == "active" else "BLE discovery"
        )
    n = len(sim.ble_results or [])
    rows = [
        Row("Discover (passive)", do="start_ble", arg="passive", enabled=not fault),
        Row("Discover (active)", do="start_ble", arg="active", enabled=not fault),
        Row(f"Results ({n})", to=rt.target("results")),
    ]
    return View(
        "list", "!Bluetooth: BLE off" if fault else rt.title, rows=rows, state="disabled" if fault else state
    )


def _ble_label(b: fx.BleAdvertiser) -> str:
    return ascii_text(b.name, "") or b.addr


def ble_results(sim: Simulator, rt: Route, state: str) -> View:
    none_yet = sim.ble_results is None
    res = [] if state == "empty" else sorted(sim.ble_results or [], key=lambda b: -b.rssi)
    rows = []
    for i, b in enumerate(res):
        c = "?" if b.connectable is None else ("C" if b.connectable else "-")
        rows.append(Row(f"{b.rssi:4d} {c} {_ble_label(b)}", to=rt.target("detail"), arg=i))
    title = "BLE: no discovery yet" if none_yet else "BLE passive"
    return View(
        "list", title, rows=rows, empty="Start a discovery first" if none_yet else rt.empty, state=state
    )


def ble_detail(sim: Simulator, rt: Route, state: str) -> View:
    res = sorted(sim.ble_results or [], key=lambda b: -b.rssi)
    if not res:
        return generic(sim, rt, "empty")
    b = res[min(sim.selected.get("ble", 0), len(res) - 1)]
    conn = "unknown" if b.connectable is None else ("yes" if b.connectable else "no")
    body = (
        f"Observation: {sim.selected.get('ble', 0) + 1} (session-local, not an identity)\n"
        f"Address: {b.addr} ({b.addr_type})\nRSSI (last): {b.rssi} dBm\nConnectable: {conn}\n"
        f"Name: {ascii_text(b.name, '(none)')}\nService UUIDs: {b.services}\n"
        f"Seen: {b.seen} times\nAddresses can rotate and names are not unique."
    )
    return _msg(rt.title, body, state)


# ---- Findings, targets, assess ---------------------------------------------------------------------------


def findings(sim: Simulator, rt: Route, state: str) -> View:
    body = (
        "Saved findings = the evidence log of this session.\n"
        f"Session: sess-{sim.fixtures.session}\nBoot: simulated\nDevice: sim-desktop (not a device)\n"
        "Storage: simulated, nothing is written\n"
        f"Events written (this run): {sim.events_written}\nAccepted by backend: not simulated\n"
        f"Backend: {sim.conn.short}\nLast operation: {sim.last_result}\n"
        "Findings are evaluated on the backend (Ask Oyster or the report)."
    )
    return _msg(rt.title, body, state)


def targets(sim: Simulator, rt: Route, state: str) -> View:
    rows = []
    for tid, _profile in [] if state == "empty" else sim.fixtures.targets:
        if sim.ble_results is None:
            seen = "no scan"
        else:
            seen = "name?" if any(ascii_text(b.name, "") == tid for b in sim.ble_results) else "not seen"
        rows.append(Row(f"{tid:<19.19s} {seen}", to=rt.target("detail"), do="target_detail", arg=tid))
    return View("list", rt.title, rows=rows, empty=rt.empty, state=state)


def assess(sim: Simulator, rt: Route, state: str) -> View:
    if state == "loading" or (sim.task and sim.task.kind == "gatt" and sim.task.route == rt.id):
        return _progress(sim, "Inspect GATT")
    if not sim.fixtures.targets or state == "empty":
        return View("list", rt.title, rows=[Row("Target: (none enrolled)", do="no_target")], state=state)
    n = len(sim.gatt_results or [])
    rows = [
        Row(f"Target: {sim.fixtures.targets[0][0]}"),
        Row("Inspect GATT", do="start_gatt"),
        Row("Read status [pub]", do="read_value"),
        Row(f"GATT results ({n})", to=rt.target("gatt")),
        Row("Last read result", do="last_read"),
        Row("Access-policy check: not in build", do="not_in_build", enabled=False),
    ]
    return View("list", rt.title, rows=rows, state=state)


def gatt(sim: Simulator, rt: Route, state: str) -> View:
    rows = (
        []
        if state == "empty"
        else [Row(g, do="gatt_detail", arg=i) for i, g in enumerate(sim.gatt_results or [])]
    )
    title = "GATT: not inspected" if sim.gatt_results is None else "GATT results"
    return View("list", title, rows=rows, empty=rt.empty, state=state)


# ---- Oyster AI --------------------------------------------------------------------------------------------


def ai_menu(sim: Simulator, rt: Route, state: str) -> View:
    offline = state == "error" or not sim.online
    rows = [
        Row("Ask a question", do="ask"),
        Row("Latest AI run", do="latest_run"),
        Row("Cancel AI run", do="cancel_ai"),
        Row("Sync evidence now", do="sync"),
    ]
    return View(
        "list",
        "!Oyster AI: unavailable" if offline else rt.title,
        rows=rows,
        state="error" if offline else state,
    )


def ai_run(sim: Simulator, rt: Route, state: str) -> View:
    run = sim.run
    if state == "loading":
        return _progress(sim, "Waiting for the backend")
    if state == "empty":
        run = None
    lines: list[str] = []
    if not sim.online:
        lines.append(f"AI unavailable: backend {sim.conn.short}.\nNo answer is generated while offline.\n")
    if sim.task and sim.task.kind == "ai":
        lines.append("Question sent; waiting for the backend to start the run.\n")
    if run is None:
        lines.append("No AI run yet in this boot.")
    else:
        status = "failed" if state == "error" else run.status
        lines += [f"Run: {run.run_id}", f"Status: {status}"]
        if status == "failed":
            lines.append("The run did not complete. No answer was generated.")
        elif run.answer:
            lines.append("\n" + run.answer)
        if run.pending and status != "failed":
            op, target, effect = run.pending
            lines.append(f"\nPending action: {op}\nTarget: {target}\n{effect}")
            lines.append("It needs your local confirmation on this device before anything runs.")
    title = "Ask Oyster run" if sim.online and state != "error" else "!Ask Oyster: AI unavailable"
    return _msg(title, "\n".join(lines), state if state in ("error", "empty") else "normal")


# ---- Linux (preview) -------------------------------------------------------------------------------------


def _offline(title: str, sim: Simulator) -> View:
    return _msg(
        f"!{title}: offline",
        f"Backend: {sim.conn.short}. {sim.conn.explanation}\n"
        "No data is shown and nothing is cached as current.\nEnter retries. Back returns.",
        "error",
        ("!Del:Back Ent:Retry", ""),
    )


def linux_hosts(sim: Simulator, rt: Route, state: str) -> View:
    if not sim.online:
        return _offline("Linux", sim)
    rows = [
        Row(
            f"{'*' if h.online else 'o'} {h.name} {'online' if h.online else 'offline'}",
            to=rt.target("host") if h.online else None,
            do=None if h.online else "host_offline",
            arg=i,
        )
        for i, h in enumerate(sim.fixtures.hosts)
    ]
    return View("list", rt.title, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


def linux_host(sim: Simulator, rt: Route, state: str) -> View:
    if not sim.online:
        return _offline("Linux", sim)
    h = sim.host()
    rows = [
        Row("System health", to=rt.target("health"), do="host_health"),
        Row(f"Services ({len(h.services)})", to=rt.target("services")),
        Row("Logs", to=rt.target("logs")),
    ]
    return View(
        "list",
        f"Linux {h.name}",
        rows=[] if state == "empty" else rows,
        empty=rt.empty,
        right="ONLINE",
        state=state,
    )


def linux_services(sim: Simulator, rt: Route, state: str) -> View:
    h = sim.host()
    rows = [
        Row(f"{n:<10} {s}", to=rt.target("detail"), do="service_detail", arg=(n, s)) for n, s in h.services
    ]
    return View("list", rt.title, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


def linux_logs(sim: Simulator, rt: Route, state: str) -> View:
    h = sim.host()
    return _msg(f"Logs {h.name}", "\n".join(h.logs) if state != "empty" else rt.empty, state)


# ---- Control Deck (preview) -------------------------------------------------------------------------------


def deck_profile(sim: Simulator, rt: Route, state: str) -> View:
    if not sim.online:
        return _offline("Control Deck", sim)
    rows = [
        Row(f"{m.slot}: {m.title}", to=rt.target("macro"), arg=i, shortcut=str(m.slot))
        for i, m in enumerate(sim.fixtures.macros)
    ]
    rows.append(Row(f"History ({len(sim.history)})", to=rt.target("history")))
    n = len(sim.fixtures.macros)
    return View(
        "list",
        "CONTROL DECK",
        rows=[] if state == "empty" else rows,
        empty=rt.empty,
        right=sim.fixtures.profile_name,
        shortcuts="".join(str(m.slot) for m in sim.fixtures.macros),
        footer=(f"Del:Back 1-{n}:Open", "Ent:Open"),
        state=state,
    )


def deck_macro(sim: Simulator, rt: Route, state: str) -> View:
    m = sim.macro()
    body = (
        f"Macro: {m.title}\nHost: {m.host}\nImpact: {m.impact}\nSteps: {m.steps}\n"
        f"Confirmation: {'required' if m.requires_confirm else 'not needed (read-only)'}\n"
        "Runs once per Enter: a retry reuses the idempotency key, never a second run."
    )
    return _msg(
        f"{m.slot}: {m.title}", body, state, ("Del:Back", "Ent:Run" if not m.requires_confirm else "Ent:Ask")
    )


def confirm_view(sim: Simulator, rt: Route, state: str) -> View:
    """The modal layer: drawn from the open dialog (D20 for a macro, the generic dialog otherwise)."""
    m = sim.modal
    if m is None:
        return generic(sim, rt, "empty")
    return View("confirm", "CONFIRM ACTION", op=m.operation, target=m.target, effect=m.effect, state=state)


def deck_job(sim: Simulator, rt: Route, state: str) -> View:
    job = sim.job
    if job is None:
        return generic(sim, rt, "empty")
    if job.state in ("queued", "running") or state == "loading":
        return View(
            "progress",
            "IN PROGRESS",
            label=f"{job.job_id}: {job.state}",
            elapsed_s=sim.elapsed_s(),
            items=0,
            cancellable=True,
            state="loading",
        )
    state_now = "failed" if state == "error" and job.state == "success" else job.state
    head = {
        "success": "Job finished",
        "failed": "!Job failed",
        "timed_out": "!Job timed out",
        "cancelled": "Job cancelled",
        "unknown": "!Job status unknown",
    }.get(state_now, "!Job status unknown")
    body = f"Job: {job.job_id}\nMacro: {job.title}\nHost: {job.host}\nState: {state_now}\n"
    if state_now == "unknown":
        body += (
            "The connection was lost after the request was sent. It may or may not have run.\n"
            "Enter checks the status with the same idempotency key; it never runs the macro again."
        )
    elif state_now == "failed":
        body += "The host reported a failure. Nothing was retried automatically."
    elif state_now == "timed_out":
        body += "No answer within the time limit. The final state is not known yet."
    elif state_now == "cancelled":
        body += "Cancelled before it finished."
    else:
        body += "The host reported completion (mock data)."
    footer = ("!Del:Back Ent:Check", "") if state_now == "unknown" else None
    return _msg(head, body, "error" if head.startswith("!") else "normal", footer)


def deck_history(sim: Simulator, rt: Route, state: str) -> View:
    rows = [Row(f"{j:<9} {s:<8} {t}", do="history_detail", arg=(j, t, s)) for j, t, s in sim.history]
    return View("list", rt.title, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


# ---- Smart Home (preview) --------------------------------------------------------------------------------


def smart_rooms(sim: Simulator, rt: Route, state: str) -> View:
    if not sim.online:
        return _offline("Smart Home", sim)
    rows = [
        Row(f"{r.name} ({len(r.devices)})", to=rt.target("room"), arg=i)
        for i, r in enumerate(sim.fixtures.rooms)
    ]
    return View("list", rt.title, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


def smart_devices(sim: Simulator, rt: Route, state: str) -> View:
    room = sim.room_obj()
    rows = [
        Row(f"{n}: {sim.device_state(room.name, n, s)}", do="toggle_device", arg=(room.name, n))
        for n, s, _ in room.devices
    ]
    rows.append(Row("IR profiles", to=rt.target("ir")))
    return View("list", room.name, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


def smart_ir(sim: Simulator, rt: Route, state: str) -> View:
    if not sim.ir_ready or state == "empty":
        return _msg(
            "IR: setup needed",
            "No IR profile is set up for this room.\nInfrared controls need a profile "
            "for a device you own. Nothing is sent.",
            "empty",
        )
    rows = [Row(p, do="send_ir", arg=p) for p in sim.fixtures.ir_profiles]
    return View("list", rt.title, rows=rows, empty=rt.empty, state=state)


# ---- Original Apps ----------------------------------------------------------------------------------------


def original(sim: Simulator, rt: Route, state: str) -> View:
    return _msg(
        "!Original Apps: unavailable" if state == "error" else "Original Apps",
        "Original launcher integration unavailable in simulator.\n"
        "The launcher bridge is not verified, so there is no switch action here. Your original apps and "
        "settings are untouched; nothing on this screen imitates them.",
        state,
    )


# ---- Settings ---------------------------------------------------------------------------------------------


def settings(sim: Simulator, rt: Route, state: str) -> View:
    rows = [
        Row(f"Backend: {sim.conn.short}", to=rt.target("backend")),
        Row(f"Brightness: {sim.brightness + 1}/5", do="brightness"),
        Row("Storage: simulated", to=rt.target("storage")),
        Row("Diagnostics", to=rt.target("diagnostics")),
        Row("Sync evidence now", do="sync"),
        Row("New session", do="new_session"),
        Row("Keyboard", to=rt.target("keyboard")),
        Row("Key tester", to=rt.target("keytest")),
        Row("About", to=rt.target("about")),
    ]
    return View("list", rt.title, rows=[] if state == "empty" else rows, empty=rt.empty, state=state)


def key_tester(sim: Simulator, rt: Route, state: str) -> View:
    kt = sim.keytest
    body = (
        f"Keys: {kt.keys or '(press a key)'}\nAction: {kt.action or '-'}\nBefore: {kt.before or '-'}\n"
        f"Changes: {kt.presses}. Leave: Back or Home twice.\n"
        "Navigation mode. Cancel (Fn+` / Ctrl+C) still cancels."
    )
    return _msg(rt.title, body, state, ("Del Del:Leave", ""))


DETAIL_BODIES: dict[str, Callable[[Simulator], str]] = {
    "settings_backend": lambda s: (
        f"State: {s.conn.short}\nConfig: simulated\nHost: (none: desktop simulator)\nWi-Fi SSID: (none)\n"
        "Device id: sim-desktop\nProvisioned: simulated\nToken and Wi-Fi password do not exist here."
    ),
    "settings_storage": lambda s: (
        "SD state: simulated\nTotal: n/a\nUsed: n/a\nEvidence: nothing is written by the simulator\n"
        "A card inserted after boot is used after a reboot."
    ),
    "settings_diag": lambda s: (
        "Startup: simulated, no modules\n"
        + ("Startup FAILED: BLE\n" if "ble" in s.faults else "")
        + f"Uptime: {s.now // 1000} s (simulation clock)\nHeap, stacks, queues: not simulated\n"
        "Radio: not started (no radio)\nBattery: "
        + (f"{s.fixtures.battery_pct}% (fixture)" if s.fixtures.battery_pct >= 0 else "unknown")
    ),
    "settings_keyboard": lambda s: KEYS_TEXT,
    "settings_about": lambda s: (
        "Oyster Pocket UI simulator (desktop)\nBoard: none, 240x135 software display\n"
        "Fonts: glyphs extracted from M5GFX; panel colours UNVERIFIED\n"
        "Keys: see Settings > Keyboard (proposed mapping, not yet confirmed on hardware)."
    ),
}


def detail_text(sim: Simulator, rt: Route, state: str) -> View:
    return _msg(rt.title, DETAIL_BODIES[rt.id](sim), state)


def help_view(sim: Simulator, rt: Route, state: str) -> View:
    origin = sim.help_from or "H01"
    name = sim.route_of(origin).title
    return _msg("Help", f"{name} [{origin}]: {HINTS.get(origin, 'Back returns.')}\n\n{KEYS_TEXT}", state)


def message_view(sim: Simulator, rt: Route, state: str) -> View:
    title, body = sim.message
    return _msg(title, body, state)


def entry_view(sim: Simulator, rt: Route, state: str) -> View:
    return View(
        "entry", "Room label" if rt.id == "room_entry" else "Ask Oyster", entry=sim.entry, state=state
    )


# ---- dispatch ---------------------------------------------------------------------------------------------

# route id -> (builder, states the builder draws itself)
BUILDERS: dict[str, tuple[Callable[..., View], tuple[str, ...]]] = {
    "H01": (home, ("normal", "error", "disabled")),
    "S10": (security, ("normal", "disabled")),
    "S11": (wifi_menu, ("normal", "loading")),
    "S12": (wifi_results, ("normal", "empty")),
    "S13": (wifi_detail, ("normal",)),
    "S21": (ble_menu, ("normal", "loading", "disabled")),
    "S22": (ble_results, ("normal", "empty")),
    "S23": (ble_detail, ("normal",)),
    "findings": (findings, ("normal",)),
    "targets": (targets, ("normal", "empty")),
    "assess": (assess, ("normal", "loading", "empty")),
    "gatt": (gatt, ("normal", "empty")),
    "A10": (ai_menu, ("normal", "error")),
    "A11": (ai_run, ("normal", "loading", "error", "empty")),
    "A12": (ai_run, ("normal", "loading", "error", "empty")),
    "L10": (linux_hosts, ("normal", "empty")),
    "L11": (linux_host, ("normal", "empty")),
    "L12": (linux_services, ("normal", "empty")),
    "L13": (linux_logs, ("normal", "empty")),
    "D10": (deck_profile, ("normal", "empty")),
    "D11": (deck_macro, ("normal",)),
    "D20": (confirm_view, ("normal",)),
    "D21": (deck_job, ("normal", "loading", "error")),
    "D22": (deck_history, ("normal", "empty")),
    "SH10": (smart_rooms, ("normal", "empty")),
    "SH11": (smart_devices, ("normal", "empty")),
    "SH12": (smart_ir, ("normal", "empty")),
    "O10": (original, ("normal", "error", "empty")),
    "P10": (settings, ("normal", "empty")),
    "P11": (key_tester, ("normal",)),
    "settings_backend": (detail_text, ("normal",)),
    "settings_storage": (detail_text, ("normal",)),
    "settings_diag": (detail_text, ("normal",)),
    "settings_keyboard": (detail_text, ("normal",)),
    "settings_about": (detail_text, ("normal",)),
    "ask_entry": (entry_view, ("normal",)),
    "room_entry": (entry_view, ("normal",)),
    HELP: (help_view, ("normal",)),
    "msg": (message_view, ("normal",)),
    "confirm": (confirm_view, ("normal",)),
}


def build(sim: Simulator, rt: Route, state: str) -> View:
    """The view of `rt` in `state` (the effective state: see Simulator.state_of)."""
    if rt.availability == UNAVAILABLE and not sim.preview:
        return unavailable(rt.module, rt)
    fn, own = BUILDERS[rt.id]
    if state in own:
        return fn(sim, rt, state)
    return generic(sim, rt, state)
