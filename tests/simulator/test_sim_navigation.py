"""Navigation contract (docs/OysterOS_Project_Plan.md sections 4, 10, 14): NAV-01..03, Back/Home from every
route, the modal layer with its 700 ms input guard, no trapped user, cancellable tasks, Help and the key
tester."""

from __future__ import annotations

import pytest
from oyster_ui_sim.engine import STACK_CAP, Simulator
from oyster_ui_sim.keys import Action
from oyster_ui_sim.routes import BY_ID, HELP, ROUTES
from sim_support import back_chord, screen_text

PROFILES = pytest.mark.parametrize("preview", [False, True], ids=["firmware", "preview"])


def at(route: str, *, preview: bool = False, state: str | None = None) -> Simulator:
    s = Simulator(preview=preview)
    s.goto(route, state)
    s.advance(1000)  # past the 700 ms dialog guard
    return s


# ---- NAV-01 .. NAV-03 (Plan section 14) --------------------------------------------------------------------


@PROFILES
def test_nav_01_back_from_any_leaf_screen_returns_to_its_parent(preview):
    leaves = {r.id for r in ROUTES} - {r.parent for r in ROUTES}
    assert len(leaves) >= 12
    checked = 0
    for rt in ROUTES:
        if rt.parent is None or rt.modal:
            continue
        s = at(rt.id, preview=preview)
        s.press(back_chord(s))
        if rt.id == "P11":  # the key tester displays keys: Back twice leaves (documented exception)
            assert s.cur == "P11"
            s.press("`")
        assert s.cur == rt.parent, f"Back from {rt.id} landed on {s.cur}, expected {rt.parent}"
        assert s.modal is None
        checked += 1
    assert checked == 35  # 40 routes - root - 2 overlays - 2 modals
    assert leaves <= {r.id for r in ROUTES}


@PROFILES
def test_nav_01_back_pops_the_overlays_to_the_screen_below(preview):
    s = at("A12", preview=preview)
    s.press("?")
    assert s.cur == HELP
    s.press("`")
    assert s.cur == "A12"
    s2 = at("msg", preview=preview)
    s2.press("`")
    assert s2.cur == "H01"


@PROFILES
def test_nav_02_home_from_any_screen_returns_to_the_root(preview):
    for rt in ROUTES:
        s = at(rt.id, preview=preview)
        s.press("fn+del")
        if rt.id == "P11":  # key tester: Home twice, as documented in UI_SCREEN_MAP.md
            assert s.cur == "P11", "the key tester displays Home instead of acting on it the first time"
            s.press("fn+del")
        assert s.cur == "H01", f"Home from {rt.id} landed on {s.cur}"
        assert s.stack == [] and s.modal is None, rt.id


def test_nav_02_home_works_from_text_entry_and_from_a_running_task(sim):
    sim.goto("A10")
    sim.press("enter")  # Ask a question -> text entry
    assert sim.cur == "ask_entry"
    sim.type_text("hello")
    sim.press("fn+del")
    assert sim.cur == "H01" and sim.requests_sent == 0
    sim.goto("S11")
    sim.press("enter")
    assert sim.task is not None
    sim.press("fn+del")
    assert sim.cur == "H01" and sim.task is not None  # the task keeps running and stays cancellable


@PROFILES
def test_nav_03_back_on_a_confirm_modal_dismisses_it_without_execution(preview):
    for route in ("confirm", "D20") if preview else ("confirm",):
        s = at(route, preview=preview)
        assert s.modal is not None
        before_cur, before_stack, before_exec = s.cur, list(s.stack), list(s.executed)
        s.press("`")
        assert s.modal is None, route
        assert (s.cur, s.stack) == (before_cur, before_stack), "Back closes the modal first, it does not pop"
        assert s.executed == before_exec, "a rejected dialog must not execute anything"
        assert s.task is None and s.job is None or s.job.state not in ("queued", "running")
        assert s.toast_text.startswith("Rejected")


# ---- the modal layer and its input guard ------------------------------------------------------------------


def test_modal_ignores_keys_for_700_ms_then_answers(preview):
    preview.goto("D11")
    preview.selected["macro"] = 1  # "Start dev stack": needs confirmation
    preview.press("enter")
    assert preview.modal is not None and preview.modal.route == "D20"
    preview.advance(699)
    preview.press("enter")
    preview.press("`")
    preview.press("fn+del")
    assert preview.modal is not None and preview.executed == [], "keys typed before the dialog was readable"
    preview.advance(1)
    preview.press("enter")
    assert preview.modal is None and preview.executed == ["deck_macro"]
    assert preview.cur == "D21"


def test_cancel_rejects_a_modal_immediately_even_inside_the_guard(preview):
    preview.goto("D11")
    preview.selected["macro"] = 1
    preview.press("enter")
    assert preview.modal is not None
    preview.press("fn+`")
    assert preview.modal is None and preview.executed == []
    preview.goto("confirm")
    preview.press("ctrl+c")
    assert preview.modal is None and preview.executed == []


def test_home_over_a_modal_rejects_it_first_and_never_approves(sim):
    sim.goto("confirm")
    sim.advance(800)
    sim.press("fn+del")
    assert sim.modal is None and sim.cur == "H01" and sim.executed == []


def test_a_modal_expires_instead_of_trapping_the_user(sim):
    sim.goto("confirm")
    sim.advance(29_999)
    assert sim.modal is not None
    sim.advance(2)
    assert sim.modal is None and "expired" in sim.toast_text and sim.executed == []


def test_the_ai_pending_action_asks_for_local_confirmation_and_runs_only_after_approval(sim):
    sim.goto("A10")
    sim.press("enter")
    sim.type_text("assess the lab device gatt")
    sim.press("enter")
    assert sim.cur == "A11" and sim.requests_sent == 1
    sim.advance(3000)
    assert sim.cur == "A12" and sim.modal is None
    sim.advance(1200)
    assert sim.modal is not None and sim.modal.operation == "ble_inspect_gatt"
    assert "gatt" not in " ".join(sim.executed)
    sim.advance(700)
    sim.press("enter")
    assert sim.task is not None and sim.task.kind == "gatt"
    assert "ble_inspect_gatt" in sim.executed


def test_rejecting_the_ai_pending_action_runs_nothing(sim):
    sim.goto("A10")
    sim.press("enter")
    sim.type_text("inspect gatt")
    sim.press("enter")
    sim.advance(3000 + 1200 + 700)
    assert sim.modal is not None
    sim.press("`")
    assert sim.modal is None and sim.task is None and "ble_inspect_gatt" not in sim.executed


# ---- no action traps the user -----------------------------------------------------------------------------


def test_no_key_sequence_traps_the_user():
    chords = [
        "up",
        "down",
        "left",
        "right",
        "enter",
        "`",
        "?",
        "fn+`",
        "1",
        "2",
        "3",
        "a",
        "s",
        "x",
        "del",
        "esc",
        "space",
        "tab",
        ".",
        ",",
        "/",
        ";",
    ]
    x = 12345
    for preview in (False, True):
        for rt in ROUTES:
            s = at(rt.id, preview=preview)
            for _ in range(24):
                x = (x * 1103515245 + 12345) & 0x7FFFFFFF
                if x % 7 == 0:
                    s.advance(x % 900)
                else:
                    s.press(chords[x % len(chords)])
                assert len(s.stack) <= STACK_CAP and s.cur in BY_ID
            s.advance(1000)
            s.press("fn+del")
            if s.cur != "H01":  # the key tester wants Home twice
                s.press("fn+del")
            assert s.cur == "H01" and s.stack == [] and s.modal is None, (rt.id, preview)


def test_the_navigation_stack_is_bounded(sim):
    for i in range(40):
        sim.nav_go("S10" if i % 2 else "P10")
    assert len(sim.stack) <= STACK_CAP
    sim.press("fn+del")
    assert sim.cur == "H01" and sim.stack == []


def test_back_on_the_root_stays_on_the_root(sim):
    sim.press("`")
    sim.press("esc")
    assert sim.cur == "H01" and sim.stack == []


def test_a_running_task_stays_visible_and_cancellable_on_every_screen(sim):
    sim.goto("S11")
    sim.press("enter")
    assert sim.task and sim.task.kind == "wifi"
    sim.press("`")  # Back from the progress view works
    sim.press("fn+del")
    assert sim.cur == "H01"
    text = screen_text(sim)
    assert "Wi-Fi survey" in text and "Fn+`:Cancel" in text
    sim.press("fn+`")
    assert sim.task is None and sim.last_result == "wifi_survey: cancelled" and sim.wifi_results is None
    text = screen_text(sim)
    assert "CANCEL REQUESTED" in text and "stopping..." in text


def test_cancel_with_nothing_running_is_acknowledged_and_harmless(sim):
    sim.press("ctrl+c")
    text = screen_text(sim)
    assert "CANCEL REQUESTED" in text and "nothing running" in text and sim.cur == "H01"


def test_a_second_task_is_refused_while_one_runs(sim):
    sim.goto("S11")
    sim.press("enter")  # Wi-Fi survey running
    sim.press("`")  # back to Security
    sim.press("down")
    sim.press("enter")  # Bluetooth Explorer
    assert sim.cur == "S21"
    sim.press("enter")  # Discover (passive): refused, the radio is busy
    assert sim.task.kind == "wifi" and "Busy" in sim.toast_text
    assert sim.executed == ["wifi_survey"]


# ---- Help, key tester -------------------------------------------------------------------------------------


@PROFILES
def test_help_opens_from_every_screen_except_text_entry_and_modals_and_returns(preview):
    for rt in ROUTES:
        if rt.id in (HELP,) or rt.modal:
            continue
        s = at(rt.id, preview=preview)
        if s.view().kind == "entry":
            s.press("?")  # '?' is a character in text entry, never Help
            assert s.cur == rt.id
            assert s.entry.buffer == ("?" if rt.id == "ask_entry" else "")  # a room label accepts no '?'
            continue
        if rt.id == "P11":
            continue
        s.press("?")
        assert s.cur == HELP, rt.id
        assert "Proposed keys, NOT yet confirmed on hardware" in "\n".join(s.view().body.splitlines())
        s.press("`")
        assert s.cur == rt.id


def test_help_is_not_available_over_a_modal(sim):
    sim.goto("confirm")
    sim.advance(800)
    sim.act(Action.HELP)
    assert sim.cur != HELP and sim.modal is not None


def test_key_tester_shows_keys_and_actions_and_needs_back_twice_to_leave(sim):
    sim.goto("P11")
    sim.press("fn+del")
    assert sim.cur == "P11"
    assert sim.keytest.keys == "Fn Del" and sim.keytest.action == "Home"
    sim.press("enter")
    assert sim.keytest.keys == "Enter" and sim.keytest.action == "Select" and sim.keytest.before == "Home"
    text = screen_text(sim)
    assert "Keys: Enter" in text and "Action: Select" in text
    sim.press("`")
    assert sim.cur == "P11"
    sim.advance(2001)  # armed state expired
    sim.press("`")
    assert sim.cur == "P11"
    sim.press("`")
    assert sim.cur == "P10"


def test_key_tester_still_cancels(sim):
    sim.goto("P11")
    sim.start_task("wifi", "Wi-Fi survey", route="S11")
    sim.press("fn+`")
    assert sim.task is None and sim.cur == "P11"


def test_physical_keys_resolve_to_logical_actions_in_the_tester(sim):
    sim.goto("P11")
    for chord, keys, action in (
        (";", "';'", "Up"),
        ("ctrl+c", "Ctrl 'c'", "Cancel"),
        ("?", "'?'", "Help"),
        ("fn+`", "Fn '`'", "Cancel"),
    ):
        sim.press(chord)
        assert (sim.keytest.keys, sim.keytest.action) == (keys, action), chord
