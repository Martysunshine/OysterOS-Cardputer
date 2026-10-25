"""Interactive window (tkinter, standard library): the 240x135 frame at an integer scale, a key legend,
clickable logical keys, and controls for connectivity, state variants and the mock backend. The only
place that
reads a wall clock: it feeds elapsed time into Simulator.advance()."""

from __future__ import annotations

import base64
import io
import time
import tkinter as tk
from tkinter import ttk

from .engine import JOB_KINDS, Simulator
from .keys import KEY_TABLE, Action, snapshot_from_pc_key
from .model import ConnState
from .routes import ROUTES, STATES

TICK_MS = 100


class App:
    def __init__(self, root: tk.Tk, scale: int, preview: bool) -> None:
        self.root, self.scale = root, scale
        self.sim = Simulator(preview=preview)
        self.fn_alt = False
        self.last = time.monotonic()
        self.last_png = b""
        root.title("OysterOS UI simulator (mock data, no device)")
        root.configure(bg="#101923")
        root.resizable(False, False)

        left = ttk.Frame(root, padding=8)
        left.grid(row=0, column=0, sticky="n")
        w, h = 240 * scale, 135 * scale
        self.canvas = tk.Canvas(left, width=w, height=h, bg="#101923", highlightthickness=0)
        self.canvas.grid(row=0, column=0, columnspan=5)
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.photo: tk.PhotoImage | None = None
        self.info = tk.StringVar()
        ttk.Label(left, textvariable=self.info, font=("Consolas", 9)).grid(
            row=1, column=0, columnspan=5, sticky="w"
        )
        pad = [
            ("Up", Action.MOVE_UP),
            ("Down", Action.MOVE_DOWN),
            ("Left", Action.MOVE_LEFT),
            ("Right", Action.MOVE_RIGHT),
            ("Select", Action.SELECT),
            ("Back", Action.BACK),
            ("Home", Action.HOME),
            ("Help", Action.HELP),
            ("Cancel", Action.CANCEL),
        ]
        for i, (name, act) in enumerate(pad):
            ttk.Button(left, text=name, command=lambda a=act: self.press(a)).grid(
                row=2 + i // 5, column=i % 5, sticky="ew"
            )

        right = ttk.Frame(root, padding=8)
        right.grid(row=0, column=1, sticky="n")
        ttk.Label(right, text="Key legend (proposed, UNVERIFIED)", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        for b in KEY_TABLE:
            ttk.Label(right, text=f"{b.action:<14} {b.shows}   [{b.pc}]", font=("Consolas", 9)).pack(
                anchor="w"
            )
        ttk.Label(
            right, text="Fn: hold Alt.  Desktop: arrows, Esc, Home, F1, Ctrl+C.", font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(4, 8))

        form = ttk.Frame(right)
        form.pack(anchor="w", fill="x")
        self.conn = tk.StringVar(value=self.sim.conn.name)
        self.state = tk.StringVar(value="auto")
        self.profile = tk.StringVar(value="preview" if preview else "firmware")
        self.route = tk.StringVar(value=self.sim.cur)
        for r, (label, var, values, cb) in enumerate(
            [
                ("Connectivity", self.conn, [c.name for c in ConnState], self.on_conn),
                ("State", self.state, ["auto", *STATES], self.on_state),
                ("Profile", self.profile, ["firmware", "preview"], self.on_profile),
                ("Go to route", self.route, [r.id for r in ROUTES], self.on_route),
            ]
        ):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w")
            box = ttk.Combobox(form, textvariable=var, values=values, state="readonly", width=18)
            box.grid(row=r, column=1, sticky="w")
            box.bind("<<ComboboxSelected>>", lambda _e, f=cb: f())
        self.zero = tk.BooleanVar(value=False)
        self.ble_fault = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Scans find nothing", variable=self.zero, command=self.on_flags).grid(
            row=4, column=0, columnspan=2, sticky="w"
        )
        ttk.Checkbutton(form, text="BLE did not start", variable=self.ble_fault, command=self.on_flags).grid(
            row=5, column=0, columnspan=2, sticky="w"
        )
        ttk.Button(right, text="Backend asks for confirmation", command=self.on_confirm).pack(
            anchor="w", pady=(6, 0)
        )
        self.fail = tk.StringVar(value="none")
        ff = ttk.Frame(right)
        ff.pack(anchor="w", pady=4)
        ttk.Label(ff, text="Next jobs fail:").grid(row=0, column=0)
        box = ttk.Combobox(ff, textvariable=self.fail, values=["none", *JOB_KINDS], state="readonly", width=8)
        box.grid(row=0, column=1)
        box.bind("<<ComboboxSelected>>", lambda _e: self.on_fail())
        ttk.Label(
            right,
            text="All data are built-in mock fixtures. Not verified against a device.",
            font=("Segoe UI", 9, "italic"),
        ).pack(anchor="w", pady=(8, 0))

        root.bind("<KeyPress>", self.on_key)
        root.bind("<KeyRelease>", self.on_key_up)
        self.refresh()
        root.after(TICK_MS, self.tick)

    # ---- input ---------------------------------------------------------------------------------------------
    def on_key(self, e: tk.Event) -> str:
        if e.keysym in ("Alt_L", "Alt_R"):
            self.fn_alt = True
            return "break"
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        snap = snapshot_from_pc_key(e.keysym, e.char, ctrl, shift, self.fn_alt)
        if snap is not None:
            self.sim.feed(snap)
            self.refresh()
        return "break"

    def on_key_up(self, e: tk.Event) -> None:
        if e.keysym in ("Alt_L", "Alt_R"):
            self.fn_alt = False

    def press(self, action: Action) -> None:
        self.sim.act(action)
        self.refresh()

    # ---- controls -----------------------------------------------------------------------------------------
    def on_conn(self) -> None:
        self.sim.set_connectivity(ConnState[self.conn.get()])
        self.refresh()

    def on_state(self) -> None:
        v = self.state.get()
        self.sim.forced = None if v == "auto" else v
        self.refresh()

    def on_profile(self) -> None:
        self.sim = Simulator(preview=self.profile.get() == "preview", conn=ConnState[self.conn.get()])
        self.on_flags()

    def on_route(self) -> None:
        self.sim.goto(self.route.get(), None if self.state.get() == "auto" else self.state.get())
        self.refresh()

    def on_flags(self) -> None:
        self.sim.scan_data = "zero" if self.zero.get() else "normal"
        self.sim.faults = {"ble"} if self.ble_fault.get() else set()
        self.refresh()

    def on_confirm(self) -> None:
        self.sim.request_confirmation(
            "ble_inspect_gatt", "lab-01", "Lists the services of the enrolled lab peripheral.", "ai_pending"
        )
        self.refresh()

    def on_fail(self) -> None:
        for k in JOB_KINDS:
            self.sim.outcomes[k] = "failed" if k == self.fail.get() else "success"

    # ---- loop ---------------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        self.sim.advance(int((now - self.last) * 1000))
        self.last = now
        self.refresh()
        self.root.after(TICK_MS, self.tick)

    def refresh(self) -> None:
        img = self.sim.frame(self.scale)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
        if data != self.last_png:
            self.last_png = data
            self.photo = tk.PhotoImage(data=base64.b64encode(data))
            self.canvas.itemconfigure(self.image_id, image=self.photo)
        s = self.sim
        modal = f" modal={s.modal.route}" if s.modal else ""
        self.info.set(f"route {s.cur} state {s.state_of()}{modal} stack {'>'.join(s.stack + [s.cur])}")
        if self.route.get() != s.cur and s.cur in {r.id for r in ROUTES}:
            self.route.set(s.cur)


def run(scale: int = 4, preview: bool = False) -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"cannot open a window ({exc}); use --headless or --script instead")
        return 2
    App(root, scale, preview)
    root.mainloop()
    return 0
