"""The simulator core: navigation stack + one modal layer, logical actions, simulated jobs and connectivity.

No wall clock: time only moves through advance(ms), so every run is deterministic. All data are mock fixtures;
nothing here talks to a radio, a backend or the network. The navigation contract is the plan's (section 4):
BACK closes the modal first, otherwise pops the stack; HOME goes to H01; no action traps the user; a running
task stays cancellable."""

from __future__ import annotations

from collections import deque
from dataclasses import replace

from PIL import Image

from . import fixtures as fx
from .keys import (
    EVENT_TO_ACTION,
    Action,
    KeySnapshot,
    NavEvent,
    describe_snapshot,
    is_key_edge,
    map_keys,
    parse_chord,
)
from .model import ConnState, Entry, Job, KeyTest, ListNav, Modal, Row, Run, Task, View
from .routes import BY_ID, HELP, HOME, UNAVAILABLE, Route, ancestors
from .views import build, confirm_view
from .widgets import ListModel, ModeBanner, TileModel, Ui, fit, wrap_next, wrapped_lines

STACK_CAP = 12  # s_stack[12]
TOAST_MS = 3000
CANCEL_ACK_MS = 3000
KEYTEST_LEAVE_MS = 2000
JOB_KINDS = ("wifi", "ble", "gatt", "ai", "deck", "ir")
DURATIONS = {"wifi": 4000, "ble": 5000, "gatt": 3000, "ai": 3000, "deck": 2600}
OP_NAMES = {
    "wifi": "wifi_survey",
    "ble": "ble_discover",
    "gatt": "ble_inspect_gatt",
    "ai": "ask_oyster",
    "deck": "deck_macro",
    "ir": "ir_send",
}
SELECT_CTX = {"S12": "ap", "S22": "ble", "L10": "host", "D10": "macro", "SH10": "room"}
BRIGHTNESS_LEVELS = 5


class Simulator:
    def __init__(
        self,
        *,
        preview: bool = False,
        conn: ConnState = ConnState.ONLINE,
        fixtures: fx.Fixtures | None = None,
        scan_data: str = "normal",
    ) -> None:
        self.preview = preview
        self.conn = conn
        self.fixtures = fixtures or fx.default_fixtures()
        self.scan_data = scan_data  # "normal" or "zero" (scans find nothing)
        self.ui = Ui()
        self.now = 0
        self.stack: list[str] = []
        self.cur = HOME
        self.forced: str | None = None
        self.entered_ms = 0
        self.navs: dict[str, ListNav] = {}
        self.scrolls: dict[str, int] = {}
        self.modal: Modal | None = None
        self.task: Task | None = None
        self.toast_text = ""
        self.toast_until = 0
        self.cancel_ack_until = 0
        self.cancel_ack_busy = False  # something was running when Cancel was pressed
        self.faults: set[str] = set()
        self.outcomes = dict.fromkeys(JOB_KINDS, "success")
        self.wifi_results: list[fx.AccessPoint] | None = None
        self.ble_results: list[fx.BleAdvertiser] | None = None
        self.gatt_results: list[str] | None = None
        self.wifi_sort = "rssi"
        self.room = ""
        self.selected: dict[str, int] = {}
        self.run: Run | None = None
        self.run_seq = 0
        self.job: Job | None = None
        self.job_seq = 3
        self.history: list[tuple[str, str, str]] = list(self.fixtures.history)
        self.executed: list[str] = []  # operations that actually started (a rejected dialog adds nothing)
        self.requests_sent = 0  # backend requests (AI-01: an empty question sends none)
        self.events_written = 0
        self.last_result = "none yet"
        self.entry = Entry("room")
        self.keytest = KeyTest()
        self.message = ("Message", "")
        self.help_from: str | None = None
        self.help_scroll = 0
        self.brightness = 2
        self.ir_ready = True
        self.dev_states: dict[tuple[str, str], str] = {}
        self.sess_seq = 0
        self.scheduled: list[tuple[int, str]] = []
        self.log: deque[str] = deque(maxlen=1000)  # navigation / action trace for scripts and tests

    # ---- small accessors (used by the views) ---------------------------------------------------------------
    @property
    def online(self) -> bool:
        return self.conn is ConnState.ONLINE

    @property
    def route(self) -> Route:
        return BY_ID[self.cur]

    def route_of(self, rid: str) -> Route:
        return BY_ID[rid]

    def elapsed_s(self, start: int | None = None) -> int:
        return max(0, (self.now - (self.entered_ms if start is None else start)) // 1000)

    def task_items(self) -> int:
        return min(max(self.now - self.task.started, 0) // 250, 99) if self.task else 0

    def host(self) -> fx.Host:
        return self.fixtures.hosts[min(self.selected.get("host", 0), len(self.fixtures.hosts) - 1)]

    def macro(self) -> fx.Macro:
        return self.fixtures.macros[min(self.selected.get("macro", 0), len(self.fixtures.macros) - 1)]

    def room_obj(self) -> fx.Room:
        return self.fixtures.rooms[min(self.selected.get("room", 0), len(self.fixtures.rooms) - 1)]

    def device_state(self, room: str, name: str, base: str) -> str:
        return self.dev_states.get((room, name), base)

    def nav(self, rid: str) -> ListNav:
        return self.navs.setdefault(rid, ListNav())

    def toast(self, text: str) -> None:
        self.toast_text, self.toast_until = text, self.now + TOAST_MS
        self.log.append(f"toast: {text}")

    @property
    def toast_active(self) -> bool:
        return self.now < self.toast_until

    # ---- state of the current route -----------------------------------------------------------------------
    def natural_state(self, rt: Route) -> str:
        if rt.availability == UNAVAILABLE and not self.preview:
            return "disabled"
        if rt.module in ("linux", "deck", "smart") and not self.online:
            return "error"
        if self.task and self.task.route == rt.id and rt.id in ("S11", "S21", "assess"):
            return "loading"
        return "normal"

    def state_of(self, rt: Route | None = None) -> str:
        rt = rt or self.route
        if self.forced and rt.id == self.cur:
            return self.forced
        return self.natural_state(rt)

    def view_of(self, rt: Route, state: str | None = None) -> View:
        """The view of a route in a state, with selection and scrolling resolved."""
        st = state or self.state_of(rt)
        v = build(self, rt, st)
        t = self.ui.t
        if v.kind in ("list", "tiles"):
            nav = self.nav(rt.id)
            if v.kind == "tiles":
                nav.sel = max(0, min(nav.sel, len(v.rows) - 1))
                tile_scroll(nav, v.columns, t.TileRows)
                total_rows = (len(v.rows) + v.columns - 1) // v.columns
                nav.top = max(0, min(nav.top, total_rows - t.TileRows))
            else:
                clamp_nav(nav, len(v.rows), t.Rows)
            v.selected, v.top = nav.sel, nav.top
            if v.kind == "tiles" and v.rows and not v.right:
                v.right = f"{v.rows[min(nav.sel, len(v.rows) - 1)].shortcut or nav.sel + 1}/{len(v.rows)}"
        elif v.kind == "message":
            v.total_lines = len(wrapped_lines(v.body, t.TextCols))
            v.scroll = min(self.scrolls.get(rt.id, 0), max(v.total_lines - t.Rows, 0))
        return v

    def view(self) -> View:
        """What the screen shows now (the modal layer wins)."""
        if self.modal:
            return confirm_view(self, BY_ID[self.modal.route], "normal")
        return self.view_of(self.route)

    # ---- navigation ---------------------------------------------------------------------------------------
    def _entered(self, rid: str, returning: bool) -> None:
        self.forced = None
        self.entered_ms = self.now
        if not returning:
            if rid in ("S12", "S22", "gatt"):
                self.navs[rid] = ListNav()
            self.scrolls[rid] = 0
        self.log.append(f"route: {rid}")

    def nav_go(self, rid: str) -> None:
        if rid == self.cur:
            return
        if rid == HELP:
            self.help_from = self.cur
            self.help_scroll = self.scrolls.get(self.cur, 0)
        if len(self.stack) >= STACK_CAP:
            del self.stack[0]
        self.stack.append(self.cur)
        self.cur = rid
        self._entered(rid, False)

    def nav_back(self) -> None:
        prev = self.stack.pop() if self.stack else HOME
        if self.cur == HOME and prev == HOME:
            return
        self.cur = prev
        self._entered(prev, True)

    def nav_home(self) -> None:
        self.stack.clear()
        if self.cur != HOME:
            self.cur = HOME
            self._entered(HOME, True)

    def goto(self, rid: str, state: str | None = None) -> None:
        """Jump to a route with a Back chain built from the route table (headless rendering and tests)."""
        rt = BY_ID[rid]
        self.modal = None
        self.task = None
        # A modal route is drawn on the modal layer over its parent; in any other state (or when the module is
        # unavailable in this profile) it is treated like a plain route so that its state variant shows.
        as_modal = rt.modal and state in (None, "normal") and (self.preview or rt.availability != UNAVAILABLE)
        base = rt.parent if as_modal else rid
        if base is None:  # overlays: opened over Home
            self.stack, self.cur = [HOME], rid
            if rid == HELP:
                self.help_from = HOME
            elif rid == "msg":
                self.message = ("Message", "Sample message of the simulator.")
        else:
            self.stack, self.cur = ancestors(base), base
        self._seed(rid)
        self.forced = state
        self.entered_ms = self.now
        if as_modal:
            self._open_sample_modal(rid)

    def _seed(self, rid: str) -> None:
        """Mock prerequisites of a route reached directly (nothing is overwritten)."""
        if rid in ("S12", "S13") and self.wifi_results is None:
            self.wifi_results = [] if self.scan_data == "zero" else list(self.fixtures.wifi)
        if rid in ("S22", "S23") and self.ble_results is None:
            self.ble_results = [] if self.scan_data == "zero" else list(self.fixtures.ble)
        if rid == "gatt" and self.gatt_results is None:
            self.gatt_results = list(self.fixtures.gatt)
        if rid in ("A11", "A12") and self.run is None:
            self.run_seq += 1
            ans = fx.ai_answer("summarise the last survey")
            done = rid == "A12"
            self.run = Run(
                f"run-{self.run_seq:04d}",
                "summarise the last survey",
                "succeeded" if done else "running",
                ans.text if done else "",
                None,
            )
        if rid == "ask_entry":
            self.entry = Entry("question", "", 0, fx.MAX_QUESTION_LEN)
        if rid == "room_entry":
            self.entry = Entry("room", "", 0, fx.MAX_ROOM_LABEL_LEN)
        if rid == "D21" and self.job is None:
            self.job_seq += 1
            m = self.fixtures.macros[0]
            self.job = Job(
                f"job-{self.job_seq:04d}", m.macro_id, m.title, m.host, "success", f"idem-{self.job_seq:04d}"
            )
        if rid == "D20":
            self.selected.setdefault("macro", 1)
            self.selected["macro"] = 1

    def _open_sample_modal(self, rid: str) -> None:
        if rid == "D20":
            m = self.macro()
            self.modal = Modal(
                "D20", m.macro_id, m.host, f"Impact: {m.impact}.", self.now, on_approve="deck_run", arg=1
            )
        else:
            self.modal = Modal(
                "confirm",
                "ble_inspect_gatt",
                "lab-01",
                "Connects to the enrolled lab peripheral and lists its services.",
                self.now,
                on_approve="ai_pending",
            )

    # ---- input --------------------------------------------------------------------------------------------
    @property
    def text_mode(self) -> bool:
        return self.view().kind == "entry"

    def feed(self, snap: KeySnapshot) -> list[tuple[NavEvent, str]]:
        """A physical key change: map it (input_map.cpp), record it for the Key tester, run the actions."""
        events = map_keys(snap, self.text_mode and not self.modal)
        if self.cur == "P11" and is_key_edge(snap, events):
            kt = self.keytest
            kt.before = kt.action
            kt.keys, kt.action = describe_snapshot(snap, events)
            kt.presses = min(kt.presses + 1, 0xFFFF)
        for ev, ch in events:
            self.act(EVENT_TO_ACTION[ev], ch)
        return events

    def press(self, chord: str) -> list[tuple[NavEvent, str]]:
        return self.feed(parse_chord(chord))

    def type_text(self, text: str) -> None:
        for ch in text:
            self.feed(KeySnapshot(chars=ch, space=ch == " "))

    def act(self, action: Action, ch: str = "") -> None:
        self.log.append(f"action: {action.value}{' ' + ch if ch else ''}")
        if action is Action.CANCEL:
            self._cancel()
            return
        if self.modal:
            self._modal_act(action)
            return
        if self.cur == "P11":
            self._keytest_act(action)
            return
        if action is Action.HOME:
            self.nav_home()
            return
        v = self.view()
        if action is Action.HELP:
            if v.kind != "entry":
                self._open_help()
            return
        if action is Action.TAB:
            return
        if v.kind == "entry":
            self._entry_act(action, ch)
        elif v.kind in ("list", "tiles"):
            self._list_act(v, action, ch)
        else:
            self._message_act(v, action)

    # ---- per-kind input ----------------------------------------------------------------------
    def _list_act(self, v: View, action: Action, ch: str) -> None:
        rt, nav = self.route, self.nav(self.cur)
        if action is Action.BACK:
            self.nav_back()
        elif action is Action.SELECT:
            if v.rows:
                self._activate(v.rows[min(nav.sel, len(v.rows) - 1)])
        elif action in (Action.TEXT_INPUT, Action.SHORTCUT):
            self._list_char(v, rt, ch)
        elif action in (Action.MOVE_UP, Action.MOVE_DOWN, Action.MOVE_LEFT, Action.MOVE_RIGHT):
            if rt.id == "P10" and nav.sel == 1 and action in (Action.MOVE_LEFT, Action.MOVE_RIGHT):
                step = 1 if action is Action.MOVE_RIGHT else -1
                self.brightness = max(0, min(BRIGHTNESS_LEVELS - 1, self.brightness + step))
            elif rt.id == "assess" and action in (Action.MOVE_LEFT, Action.MOVE_RIGHT):
                pass  # a single enrolled target: nothing to switch
            elif v.kind == "tiles":
                tile_move(nav, len(v.rows), v.columns, action, self.ui.t.TileRows)
            else:
                list_move(nav, len(v.rows), action, self.ui.t.Rows)

    def _list_char(self, v: View, rt: Route, ch: str) -> None:
        if ch and ch in v.shortcuts:
            for i, r in enumerate(v.rows):
                if r.shortcut == ch:
                    self.nav(self.cur).sel = i
                    tile_scroll(self.nav(self.cur), v.columns, self.ui.t.TileRows)
                    self._activate(r)
                    return
        elif rt.id == "S12" and ch in ("s", "S"):
            self.wifi_sort = "ch" if self.wifi_sort == "rssi" else "rssi"
            self.navs["S12"] = ListNav()
        elif rt.id == "targets" and ch in ("a", "A") and v.rows:
            self.nav_go("assess")

    def _activate(self, row: Row) -> None:
        start = (self.cur, len(self.stack))
        key = SELECT_CTX.get(self.cur)
        if key and row.arg is not None and row.to:
            self.selected[key] = row.arg
        if row.do:
            self._do(row.do, row.arg)
        if row.to and (self.cur, len(self.stack)) == start:
            self.nav_go(row.to)

    def _message_act(self, v: View, action: Action) -> None:
        rt = self.route
        if action is Action.BACK:
            self.nav_back()
            if rt.id == HELP:
                self.scrolls[self.cur] = self.help_scroll
        elif action is Action.SELECT:
            if rt.id == "D11":
                self._deck_run()
            elif rt.id == "D21" and self.job and self.job.state == "unknown":
                self._deck_reconcile()
            elif v.state == "error":
                self._retry()
        elif (
            action in (Action.MOVE_UP, Action.MOVE_DOWN, Action.MOVE_LEFT, Action.MOVE_RIGHT)
            and v.kind == "message"
        ):
            rows = self.ui.t.Rows
            mx = max(v.total_lines - rows, 0)
            s = self.scrolls.get(self.cur, 0)
            s = {
                Action.MOVE_UP: s - 1,
                Action.MOVE_DOWN: s + 1,
                Action.MOVE_LEFT: s - rows,
                Action.MOVE_RIGHT: s + rows,
            }[action]
            self.scrolls[self.cur] = max(0, min(s, mx))

    def _retry(self) -> None:
        self.forced = None
        self.toast("Retrying...")

    def _entry_act(self, action: Action, ch: str) -> None:
        e = self.entry
        if action is Action.TEXT_INPUT or action is Action.SHORTCUT:
            if len(e.buffer) < e.max_len and self._entry_accepts(ch):
                e.buffer = e.buffer[: e.cursor] + ch + e.buffer[e.cursor :]
                e.cursor += 1
        elif action is Action.ERASE:
            if not e.buffer:
                self.nav_back()  # Del on an empty buffer leaves the entry (no bare Esc key)
            elif e.cursor > 0:
                e.buffer = e.buffer[: e.cursor - 1] + e.buffer[e.cursor :]
                e.cursor -= 1
        elif action is Action.MOVE_LEFT:
            e.cursor = max(e.cursor - 1, 0)
        elif action is Action.MOVE_RIGHT:
            e.cursor = min(e.cursor + 1, len(e.buffer))
        elif action is Action.MOVE_UP:
            e.cursor = max(e.cursor - self.ui.t.TextCols, 0)
        elif action is Action.MOVE_DOWN:
            e.cursor = min(e.cursor + self.ui.t.TextCols, len(e.buffer))
        elif action is Action.SELECT:
            self._submit_entry()
        elif action is Action.BACK:
            self.nav_back()

    def _entry_accepts(self, c: str) -> bool:
        if not " " <= c <= "~":
            return False
        if self.entry.purpose == "room":
            return c.isalnum() or c in " -_.#"
        return True

    def _submit_entry(self) -> None:
        text = self.entry.buffer.strip(" ")
        if self.entry.purpose == "room":
            self.room = text
            self.nav_back()
            self.toast(f"Room label {'set' if text else 'cleared'}")
            return
        if not text:
            self.toast("Type a question first")  # AI-01: validation, no request
            return
        if not self.online:
            self.nav_back()
            self._ai_unavailable()
            return
        self.nav_back()
        self.requests_sent += 1
        self.run_seq += 1
        ans = fx.ai_answer(text)
        self.run = Run(f"run-{self.run_seq:04d}", text, "queued", ans.text, ans.pending)
        if self.start_task("ai", "Ask Oyster", route="A11"):
            self.run.status = "running"
            self.nav_go("A11")

    def _keytest_act(self, action: Action) -> None:
        """Key tester: every action is only displayed; Back or Home twice within 2 s leaves."""
        kt = self.keytest
        leave = action in (Action.BACK, Action.HOME)
        if leave and kt.armed == action.value and self.now < kt.armed_until:
            kt.armed = ""
            self.nav_back() if action is Action.BACK else self.nav_home()
            return
        kt.armed = action.value if leave else ""
        kt.armed_until = self.now + KEYTEST_LEAVE_MS

    def _open_help(self) -> None:
        if self.cur != HELP:
            self.nav_go(HELP)

    # ---- modal layer --------------------------------------------------------------------------------------
    def request_confirmation(
        self,
        operation: str,
        target: str,
        effect: str,
        on_approve: str = "",
        ttl_ms: int = 30000,
        route: str = "confirm",
    ) -> None:
        """The (mock) backend asks for a local confirmation: a modal over whatever is on screen."""
        self.modal = Modal(route, operation, target, effect, self.now, ttl_ms, on_approve)
        self.log.append(f"modal: {route}")

    def _modal_act(self, action: Action) -> None:
        m = self.modal
        if m is None or self.now < m.guard_until:
            return  # keys typed before the dialog was readable never answer it
        if action in (Action.SELECT, Action.CONFIRM):
            self._resolve_modal(True)
        elif action is Action.BACK:
            self._resolve_modal(False)
        elif action is Action.HOME:
            self._resolve_modal(False)
            self.nav_home()

    def _resolve_modal(self, approve: bool) -> None:
        m = self.modal
        if m is None:
            return
        self.modal = None
        self.toast(f"{'Approved' if approve else 'Rejected'}: {m.operation}")
        if approve:
            if m.on_approve == "deck_run":
                self._start_deck_job()
            elif m.on_approve == "ai_pending":
                self.start_task("gatt", "Inspect GATT", route="assess")

    # ---- tasks and jobs -----------------------------------------------------------------------------------
    def start_task(self, kind: str, label: str, arg: str | None = None, route: str | None = None) -> bool:
        if self.task:
            self.toast("Busy - wait or Fn+` to cancel")
            return False
        dur = DURATIONS[kind] + (1000 if arg == "active" else 0)
        self.task = Task(kind, label, route or self.cur, self.now, dur, self.outcomes[kind], arg)
        self.executed.append(OP_NAMES[kind])
        self.log.append(f"task: {kind} start")
        return True

    def _cancel(self) -> None:
        self.cancel_ack_until = self.now + CANCEL_ACK_MS
        self.cancel_ack_busy = self.task is not None
        if self.modal and self.now >= self.modal.shown_ms:
            self._resolve_modal(False)
        if self.task:
            self._finish_task("cancelled")
        if self.view().kind == "entry":
            self.nav_back()

    def _finish_task(self, outcome: str) -> None:
        t, self.task = self.task, None
        if t is None:
            return
        op = OP_NAMES[t.kind]
        watching = self.cur == t.route
        ok = outcome == "success"
        self.last_result = f"{op}: {outcome}"
        if t.kind in ("wifi", "ble", "gatt"):
            self.events_written += 1
            if ok:
                zero = self.scan_data == "zero"
                if t.kind == "wifi":
                    self.wifi_results = [] if zero else list(self.fixtures.wifi)
                elif t.kind == "ble":
                    self.ble_results = [] if zero else list(self.fixtures.ble)
                else:
                    self.gatt_results = list(self.fixtures.gatt)
                if watching:
                    self.nav_go({"wifi": "S12", "ble": "S22", "gatt": "gatt"}[t.kind])
        elif t.kind == "ai" and self.run:
            self.run.status = {"success": "succeeded", "cancelled": "cancelled"}.get(outcome, "failed")
            if watching and self.cur == "A11" and ok:
                self.cur = "A12"
                self._entered("A12", False)
            if ok and self.run.pending:
                self.scheduled.append((self.now + 1200, "ai_confirm"))
        elif t.kind == "deck" and self.job:
            self._finish_job(outcome)
        self.toast(self.last_result)

    def _finish_job(self, outcome: str) -> None:
        job = self.job
        assert job is not None
        job.state = outcome
        if outcome == "unknown":
            job.final = "success"  # what the mock host really did; revealed by a status check only
        self.history = [h for h in self.history if h[0] != job.job_id]
        self.history.insert(0, (job.job_id, job.title, job.state))

    def _start_deck_job(self) -> None:
        if self.task:
            self.toast("Busy - wait or Fn+` to cancel")
            return
        m = self.macro()
        self.job_seq += 1
        self.job = Job(
            f"job-{self.job_seq:04d}", m.macro_id, m.title, m.host, "queued", f"idem-{self.job_seq:04d}"
        )
        self.requests_sent += 1
        if self.start_task("deck", f"Job {self.job.job_id}", route="D21"):
            self.nav_go("D21")

    def _deck_run(self) -> None:
        m = self.macro()
        if m.requires_confirm:
            self.modal = Modal(
                "D20", m.macro_id, m.host, f"Impact: {m.impact}.", self.now, on_approve="deck_run"
            )
            self.log.append("modal: D20")
        else:
            self._start_deck_job()

    def _deck_reconcile(self) -> None:
        job = self.job
        if job is None:
            return
        job.state = job.final or "success"  # a status lookup with the same idempotency key: no second run
        self.history = [(j, t, job.state if j == job.job_id else s) for j, t, s in self.history]
        self.toast(f"Status checked: {job.state}")

    def _service_job_state(self) -> None:
        if self.job and self.task and self.task.kind == "deck":
            self.job.state = self.task.phase(self.now)

    # ---- time --------------------------------------------------------------------------------
    def advance(self, ms: int) -> None:
        """Move the simulation clock; due events (task end, delayed confirmation, expiry) run in order."""
        target = self.now + max(ms, 0)
        while True:
            due: list[tuple[int, str]] = [(when, name) for when, name in self.scheduled]
            if self.task:
                due.append((self.task.started + self.task.duration, "task"))
            if self.modal:
                due.append((self.modal.shown_ms + self.modal.ttl_ms, "modal"))
            due = [d for d in due if d[0] <= target]
            if not due:
                break
            when, name = min(due)
            self.now = max(self.now, when)
            if name == "task":
                self._finish_task(self.task.outcome if self.task else "success")
            elif name == "modal":
                self.modal = None
                self.toast("Confirmation closed (expired?)")
            else:
                self.scheduled.remove((when, name))
                if name == "ai_confirm" and self.run and self.run.pending and not self.modal:
                    op, tgt, eff = self.run.pending
                    self.request_confirmation(op, tgt, eff, "ai_pending")
        self.now = target
        self._service_job_state()

    def set_connectivity(self, conn: ConnState) -> None:
        self.conn = conn

    # ---- named handlers -----------------------------------------------------------------------------------
    def _do(self, name: str, arg: object) -> None:
        h = getattr(self, f"_h_{name}", None)
        if h is None:
            raise KeyError(f"no handler {name!r}")
        self.log.append(f"do: {name}")
        h(arg)

    def _h_start_wifi(self, _: object) -> None:
        self.start_task("wifi", "Wi-Fi survey")

    def _h_start_ble(self, mode: object) -> None:
        if "ble" in self.faults:
            self.toast("Bluetooth did not start")
            return
        self.start_task("ble", "BLE discovery", str(mode))

    def _h_start_gatt(self, _: object) -> None:
        self.start_task("gatt", "Inspect GATT", route="assess")

    def _h_read_value(self, _: object) -> None:
        outcome = self.outcomes["gatt"]
        self.executed.append("ble_read_profile_value")
        self.last_result = f"ble_read_profile_value: {outcome}"
        self.toast(self.last_result)

    def _h_last_read(self, _: object) -> None:
        body = "No read result yet. Use Assess > Read <characteristic>."
        if "ble_read_profile_value" in self.executed:
            body = (
                "Target: lab-01\nCharacteristic: status\nStatus: success\n"
                "Length: 4 bytes\nValue (hex): 00000001"
            )
        self.message = ("Last read", body)
        self.nav_go("msg")

    def _h_not_in_build(self, _: object) -> None:
        self.message = (
            "Not in this build",
            "This test is not run by this firmware build; a backend job for it is answered as unsupported.",
        )
        self.nav_go("msg")

    def _h_no_target(self, _: object) -> None:
        self.message = (
            "!No enrolled target",
            "Enrol your own test peripheral over USB first. The device only connects to enrolled targets.",
        )
        self.nav_go("msg")

    def _h_target_detail(self, tid: object) -> None:
        self.message = (
            "Enrolled target",
            f"Target: {tid}\nProfile: oyster-lab-01\nAllowed: ble_inspect_gatt "
            "ble_read_profile_value\nScope version: 1",
        )

    def _h_gatt_detail(self, i: object) -> None:
        rows = self.gatt_results or []
        row = rows[int(i)] if isinstance(i, int) and i < len(rows) else "?"
        self.message = (
            "GATT entry",
            f"Entry: {row.strip()}\nAdvertised properties are not a permission guarantee.",
        )
        self.nav_go("msg")

    def _h_ask(self, _: object) -> None:
        if not self.online:
            self._ai_unavailable()
            return
        self.entry = Entry("question", "", 0, fx.MAX_QUESTION_LEN)
        self.nav_go("ask_entry")

    def _ai_unavailable(self) -> None:
        self.message = (
            "!AI unavailable",
            f"Backend state: {self.conn.short}.\nNo answer is generated offline. Local surveys still work.",
        )
        self.nav_go("msg")

    def _h_latest_run(self, _: object) -> None:
        done = self.run is not None and self.run.status not in ("queued", "running")
        self.nav_go("A12" if done else "A11")

    def _h_cancel_ai(self, _: object) -> None:
        if self.run is None:
            self.toast("No AI run to cancel")
        elif self.task and self.task.kind == "ai":
            self._cancel()
        else:
            self.toast("Sent: Cancel AI run")

    def _h_sync(self, _: object) -> None:
        self.toast("Sent: Sync now" if self.online else f"Backend {self.conn.short}: not synced")

    def _h_new_session(self, _: object) -> None:
        if self.task:
            self.toast("Busy - finish or cancel first")
            return
        self.sess_seq += 1
        self.fixtures = replace(self.fixtures, session=f"{0xA1B2C3 + self.sess_seq:06x}")
        self.toast(f"New session {self.fixtures.session}")

    def _h_brightness(self, _: object) -> None:
        self.brightness = (self.brightness + 1) % BRIGHTNESS_LEVELS

    def _h_host_offline(self, i: object) -> None:
        h = self.fixtures.hosts[int(i) if isinstance(i, int) else 0]
        self.message = (
            f"{h.name}: offline",
            f"Last seen {h.last_seen}. No health data is shown for a host that does not answer.",
        )
        self.nav_go("msg")

    def _h_host_health(self, _: object) -> None:
        h = self.host()
        self.message = (
            f"{h.name} health",
            f"CPU: {h.cpu}%\nMemory: {h.mem}%\nDisk: {h.disk}%\nUptime: {h.uptime_h} h\n"
            f"Updated: {h.last_seen}\nRead-only inspection: no confirmation needed.",
        )

    def _h_service_detail(self, arg: object) -> None:
        name, state = arg  # type: ignore[misc]
        self.message = (
            f"Service {name}",
            f"State: {state}\nRead-only. Starting or stopping a service is an approved "
            "task that asks for confirmation first.",
        )

    def _h_history_detail(self, arg: object) -> None:
        job, title, state = arg  # type: ignore[misc]
        self.message = (f"Job {job}", f"Macro: {title}\nState: {state}\nMock history entry.")
        self.nav_go("msg")

    def _h_toggle_device(self, arg: object) -> None:
        room, name = arg  # type: ignore[misc]
        dev = next(d for r in self.fixtures.rooms if r.name == room for d in r.devices if d[0] == name)
        state = self.device_state(room, name, dev[1])
        if not dev[2] or not self.online:
            self.toast(f"{name} unavailable: not switched")  # no false success indicator
        elif state in ("on", "off"):
            new = "off" if state == "on" else "on"
            self.dev_states[(room, name)] = new
            self.executed.append(f"toggle:{name}")
            self.toast(f"{name}: {new} (acknowledged)")
        else:
            self.toast(f"{name}: no state to toggle")

    def _h_send_ir(self, profile: object) -> None:
        self.executed.append(f"ir:{profile}")
        if self.outcomes["ir"] == "success":
            self.toast("IR sent (no device feedback)")
        else:
            self.toast("IR send failed")

    # ---- rendering ----------------------------------------------------------------------------------------
    def render(self) -> Ui:
        """Paint the current frame into self.ui.screen (240x135) the way AppController::draw does."""
        ui, t = self.ui, self.ui.t
        sb = self.status_bar()
        ui.draw_status(sb["mode"], sb["session"], sb["backend"], sb["battery"], sb["conn"])  # type: ignore[arg-type]
        v = self.view()
        if v.kind == "list":
            ui.draw_list(
                ListModel(
                    v.title,
                    [r.text for r in v.rows],
                    v.selected,
                    v.top,
                    [not r.enabled for r in v.rows],
                    v.empty,
                    v.right,
                )
            )
        elif v.kind == "tiles":
            top_row = v.top
            ui.draw_tiles(TileModel(v.title, v.right, v.tiles, v.columns, v.selected, top_row))
        elif v.kind == "message":
            ui.draw_message(v.title, _skip_lines(v.body, v.scroll, t.TextCols), v.scroll, v.total_lines)
        elif v.kind == "progress":
            ui.draw_progress(v.label, v.elapsed_s, v.items, v.cancellable)
        elif v.kind == "confirm":
            ui.draw_confirm(v.op, v.target, v.effect)
        elif v.kind == "entry":
            e = self.entry
            ui.draw_text_entry(v.title, e.buffer, e.cursor, "Del on empty text: Back")
        self._render_footer(v)
        return ui

    def _default_footer(self, v: View) -> tuple[str, str]:
        rid = self.cur
        if v.kind == "tiles" and rid == HOME:
            return "?:Help 1-8:Open", "Ent:Open"
        if v.kind == "entry":
            return "Ent:OK Del:Erase", f"{len(self.entry.buffer)}/{self.entry.max_len}"
        if v.kind == "message":
            return "Del:Back ;.:Scroll", f"{v.scroll + 1}/{max(v.total_lines, 1)}"
        table = {
            "S12": ("Del:Back s:Sort", "Ent:Info"),
            "targets": ("Del:Back a:Assess", "Ent:Info"),
            "assess": ("Del:Back ?:Help", "Ent:Run"),
            "P10": ("Del:Back ,/:Adjust", "Ent:Open"),
            "S22": ("Del:Back ?:Help", "Ent:Info"),
            "gatt": ("Del:Back ?:Help", "Ent:Info"),
        }
        return table.get(rid, ("Del:Back ?:Help", "Ent:Open"))

    def _render_footer(self, v: View) -> None:
        ui = self.ui
        left, right = v.footer or self._default_footer(v)
        if self.route.id == "P11" and not self.modal:
            left, right = "Del Del:Leave", ""
        if self.now < self.cancel_ack_until:
            left, right = "!CANCEL REQUESTED", "stopping..." if self.cancel_ack_busy else "nothing running"
        elif self.modal:
            rem = max(self.modal.shown_ms + self.modal.ttl_ms - self.now, 0) // 1000
            left, right = "Ent:Approve Del:Reject", f"{rem}s"
        elif self.toast_active:
            ui.draw_notice(self.toast_text)
            return
        elif self.task:
            left, right = (
                f"{fit(self.task.label, 24)} {(self.now - self.task.started) // 1000}s",
                "Fn+`:Cancel",
            )
        ui.draw_footer(left, right)

    def frame(self, scale: int = 1) -> Image.Image:
        self.render()
        return self.ui.screen.image(scale)

    def status_bar(self) -> dict[str, object]:
        """What the status bar shows, derived from the simulated state."""
        mode = ModeBanner.START_FAILED if self.faults else ModeBanner.SIMULATED
        return {
            "mode": mode,
            "session": self.fixtures.session,
            "backend": self.conn.short,
            "battery": self.fixtures.battery_pct,
            "conn": self.conn.icon,
        }


def _skip_lines(body: str, n: int, cols: int) -> str:
    p = body
    for _ in range(n):
        _, used = wrap_next(p, cols)
        if used == 0:
            break
        p = p[used:]
    return p


# ---- list / tile navigation (ports of list_move, clamp_nav, tile_scroll, tile_move) -----------------------


def list_move(n: ListNav, count: int, action: Action, rows: int) -> None:
    if count == 0:
        n.sel = n.top = 0
        return
    if action is Action.MOVE_UP:
        n.sel = n.sel - 1 if n.sel > 0 else count - 1
    elif action is Action.MOVE_DOWN:
        n.sel = n.sel + 1 if n.sel + 1 < count else 0
    elif action is Action.MOVE_LEFT:
        n.sel = n.sel - rows if n.sel >= rows else 0
    elif action is Action.MOVE_RIGHT:
        n.sel = n.sel + rows if n.sel + rows < count else count - 1
    else:
        return
    if n.sel < n.top:
        n.top = n.sel
    if n.sel >= n.top + rows:
        n.top = n.sel - rows + 1


def clamp_nav(n: ListNav, count: int, rows: int) -> None:
    if count == 0:
        n.sel = n.top = 0
        return
    n.sel = min(n.sel, count - 1)
    if n.sel < n.top:
        n.top = n.sel
    if n.sel >= n.top + rows:
        n.top = n.sel - rows + 1
    if n.top + rows > count:
        n.top = count - rows if count > rows else 0


def tile_scroll(n: ListNav, cols: int, tile_rows: int) -> None:
    row = n.sel // cols
    if row < n.top:
        n.top = row
    if row >= n.top + tile_rows:
        n.top = row - tile_rows + 1


def tile_move(n: ListNav, count: int, cols: int, action: Action, tile_rows: int) -> None:
    if count == 0 or cols == 0:
        n.sel = n.top = 0
        return
    rows = (count + cols - 1) // cols
    row, col = divmod(n.sel, cols)
    if action is Action.MOVE_UP:
        row = row - 1 if row > 0 else rows - 1
    elif action is Action.MOVE_DOWN:
        row = row + 1 if row + 1 < rows else 0
    elif action in (Action.MOVE_LEFT, Action.MOVE_RIGHT):
        if cols < 2:
            return
        col = (col + (1 if action is Action.MOVE_RIGHT else cols - 1)) % cols
    else:
        return
    n.sel = min(row * cols + col, count - 1)
    tile_scroll(n, cols, tile_rows)
