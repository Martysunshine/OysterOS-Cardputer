"""The "UI test checklist" of docs/DESIGN_SYSTEM.md and the behaviour tests of Plan section 14 that can run
without a device (SEC-01/02, NET-01, JOB-01/03, HOME-01, AI-01, ORIG-01). All data are mock fixtures."""

from __future__ import annotations

import pytest
from oyster_ui_sim import fixtures as fx
from oyster_ui_sim.engine import Simulator
from oyster_ui_sim.model import ConnState
from oyster_ui_sim.routes import BY_ID
from oyster_ui_sim.theme import theme
from oyster_ui_sim.widgets import Conn, fit, wrap_next
from sim_support import pixel, screen_text


def at(route: str, *, preview: bool = False, state: str | None = None) -> Simulator:
    s = Simulator(preview=preview)
    s.goto(route, state)
    s.advance(1000)
    return s


# ---- [ ] Scrolling never hides the focused item -----------------------------------------------------------


@pytest.mark.parametrize("route", ["P10", "S12", "S22", "D10"])
@pytest.mark.parametrize("keys", [".", ";", "/", ","], ids=["down", "up", "page-right", "page-left"])
def test_scrolling_never_hides_the_focused_list_item(route, keys):
    th = theme()
    s = at(route, preview=True)
    n = len(s.view().rows)
    assert n >= th.Rows or route == "D10"
    for _ in range(n + 3):
        s.press(keys)
        s.render()
        nav = s.nav(route)
        assert nav.top <= nav.sel < nav.top + th.Rows, (route, keys, nav)
        v = s.view()
        assert v.rows[nav.sel].text[: th.ListCols] in "\n".join(s.ui.log.rows) or v.rows[
            nav.sel
        ].text.startswith(s.ui.log.rows[nav.sel - nav.top].rstrip("~"))
        row_y = th.RowsY + (nav.sel - nav.top) * th.RowH
        assert pixel(s, th.PadX, row_y + 4) == th.Accent.rgb888, "the selected row shows its accent stripe"


def test_scrolling_never_hides_the_focused_tile(sim):
    th = theme()
    for _ in range(10):
        sim.press(".")
        sim.render()
        nav = sim.nav("H01")
        row = nav.sel // 2
        assert nav.top <= row < nav.top + th.TileRows
        y = th.ContentY + (row - nav.top) * th.TileH + 4
        assert pixel(sim, th.PadX, y) == th.Accent.rgb888
    # 10 moves down a 4-row grid wrap around twice: row 2 of 0..3, so rows 0..2 are on screen
    assert sim.nav("H01").sel == 4 and sim.nav("H01").top == 0


def test_the_last_home_tile_scrolls_into_view_and_help_opens(sim):
    for _ in range(3):
        sim.press(".")  # tile rows 1..3 of the 4-row grid
    assert sim.nav("H01").sel == 6 and sim.nav("H01").top == 1
    sim.press("/")  # right column of the last row: Help
    assert sim.nav("H01").sel == 7
    sim.press("enter")
    assert sim.cur == "help"


# ---- [ ] Text editor handles Backspace and Cancel ---------------------------------------------------------


def test_text_entry_handles_typing_backspace_caret_and_cancel(sim):
    sim.goto("A10")
    sim.press("enter")
    assert sim.cur == "ask_entry"
    sim.type_text("hello?")
    assert sim.entry.buffer == "hello?"
    sim.press("del")
    assert sim.entry.buffer == "hello" and sim.cur == "ask_entry"
    sim.press("fn+,")  # caret left
    sim.press("fn+,")
    sim.type_text("X")
    assert sim.entry.buffer == "helXlo"
    sim.press("del")
    assert sim.entry.buffer == "hello"
    sim.press("fn+`")  # Cancel leaves the editor without sending anything
    assert sim.cur == "A10" and sim.requests_sent == 0 and sim.entry.buffer != "" and not sim.task


def test_text_entry_del_on_an_empty_buffer_leaves_and_enter_validates(sim):
    sim.goto("A10")
    sim.press("enter")
    sim.press("enter")  # AI-01: empty question
    assert sim.cur == "ask_entry" and sim.requests_sent == 0 and "Type a question first" in sim.toast_text
    sim.press("del")
    assert sim.cur == "A10"


def test_text_entry_is_bounded_and_filters_room_labels(sim):
    sim.goto("ask_entry")
    sim.type_text("a" * (fx.MAX_QUESTION_LEN + 20))
    assert len(sim.entry.buffer) == fx.MAX_QUESTION_LEN
    sim.goto("room_entry")
    sim.type_text("Lab #1!?")
    assert sim.entry.buffer == "Lab #1"
    sim.press("enter")
    assert sim.cur == "S11" and sim.room == "Lab #1"
    assert "Room: Lab #1" in screen_text(sim)


def test_cancel_also_leaves_the_room_label_editor(sim):
    sim.goto("room_entry")
    sim.type_text("abc")
    sim.press("ctrl+c")
    assert sim.cur == "S11" and sim.room == ""


# ---- [ ] All jobs have a failure state --------------------------------------------------------------------


def start_wifi(s: Simulator) -> None:
    s.goto("S11")
    s.press("enter")


@pytest.mark.parametrize("outcome", ["failed", "timed_out"])
def test_wifi_survey_failure_is_reported_and_saves_nothing(sim, outcome):
    sim.outcomes["wifi"] = outcome
    start_wifi(sim)
    sim.advance(4000)
    assert sim.last_result == f"wifi_survey: {outcome}"
    assert sim.wifi_results is None and sim.cur == "S11" and sim.task is None
    assert outcome in screen_text(sim) and "success" not in screen_text(sim)


@pytest.mark.parametrize("outcome", ["failed", "timed_out"])
def test_ble_discovery_failure_is_reported(sim, outcome):
    sim.outcomes["ble"] = outcome
    sim.goto("S21")
    sim.press("enter")
    sim.advance(5000)
    assert sim.last_result == f"ble_discover: {outcome}" and sim.ble_results is None and sim.cur == "S21"


def test_gatt_inspection_failure_is_reported(sim):
    sim.outcomes["gatt"] = "failed"
    sim.goto("assess")
    sim.press("down")
    sim.press("enter")
    sim.advance(3000)
    assert sim.last_result == "ble_inspect_gatt: failed" and sim.gatt_results is None


def test_a_failed_ai_run_shows_status_failed_and_no_answer(sim):
    sim.outcomes["ai"] = "failed"
    sim.goto("A10")
    sim.press("enter")
    sim.type_text("how many networks")
    sim.press("enter")
    sim.advance(3000)
    assert sim.run.status == "failed" and sim.cur == "A11"
    text = screen_text(sim)
    assert "Status: failed" in text and "Mock answer" not in text


def test_deck_job_failure_timeout_and_unknown_are_shown_and_never_rerun(preview):
    for outcome, head in (
        ("failed", "Job failed"),
        ("timed_out", "Job timed out"),
        ("unknown", "Job status unknown"),
    ):
        s = at("D10", preview=True)
        s.outcomes["deck"] = outcome
        s.press("enter")  # macro 1: read-only
        assert s.cur == "D11"
        s.press("enter")
        assert s.cur == "D21" and s.job.state == "queued"
        s.advance(700)
        assert s.job.state == "running" and "Elapsed" in screen_text(s)
        s.advance(2000)
        assert s.job.state == outcome and head in screen_text(s), outcome
        assert s.executed == ["deck_macro"]
        assert s.history[0][2] == outcome


def test_job_03_status_unknown_is_reconciled_with_the_same_key_without_a_second_run(preview):
    preview.outcomes["deck"] = "unknown"
    preview.goto("D11")
    preview.press("enter")
    preview.advance(3000)
    job = preview.job
    assert job.state == "unknown" and "same idempotency key" in preview.view().body
    idem, runs = job.idem, list(preview.executed)
    preview.press("enter")
    assert preview.job.state == "success" and preview.job.idem == idem
    assert preview.executed == runs, "checking the status must not execute the macro again"
    assert preview.history[0][2] == "success"


def test_job_01_a_run_shows_job_id_and_final_status(preview):
    preview.goto("D11")
    preview.press("enter")
    preview.advance(3000)
    text = screen_text(preview)
    assert (
        "Job: job-0004" in text
        and "State: success" in text
        and preview.history[0] == ("job-0004", "Server status", "success")
    )
    preview.press("`")
    assert preview.cur == "D11"


def test_a_job_can_be_cancelled_while_queued_or_running(preview):
    preview.goto("D11")
    preview.press("enter")
    preview.advance(300)
    preview.press("fn+`")
    assert preview.job.state == "cancelled" and preview.task is None
    assert "Job cancelled" in screen_text(preview)


def test_ir_failure_is_reported_and_success_never_claims_the_appliance_reacted(preview):
    preview.goto("SH12")
    preview.press("enter")
    assert preview.toast_text == "IR sent (no device feedback)"
    preview.outcomes["ir"] = "failed"
    preview.press("enter")
    assert preview.toast_text == "IR send failed"


# ---- [ ] Connected labels derive from actual connectivity -------------------------------------------------


@pytest.mark.parametrize("conn", list(ConnState))
def test_status_bar_and_labels_follow_the_simulated_connectivity(sim, conn):
    th = theme()
    sim.set_connectivity(conn)
    sim.render()
    assert sim.ui.log.status.split()[-2] == conn.short
    assert f"2 {conn.short}" in screen_text(sim)  # the Oyster AI tile
    cx = sim.ui.status_cx
    dot = {
        Conn.ONLINE: th.Accent,
        Conn.CONNECTING: th.Warning,
        Conn.ERROR: th.Error,
    }.get(conn.icon)
    if dot is not None:
        assert pixel(sim, cx, 7) == dot.rgb888
    else:  # offline: an outlined circle in the muted colour, hollow in the middle
        assert pixel(sim, cx + 2, 7) == th.TextMuted.rgb888 and pixel(sim, cx, 7) == th.SurfaceBase.rgb888
    assert ("online" in sim.ui.log.status) is (conn is ConnState.ONLINE)


def test_nothing_says_connected_unless_the_backend_is_online(sim):
    for conn in ConnState:
        sim.set_connectivity(conn)
        for rid in ("H01", "A10", "P10", "settings_backend"):
            sim.goto(rid)
            text = screen_text(sim).lower()
            assert (
                ("online" in text) is (conn is ConnState.ONLINE) or rid == "A10" and "unavailable" in text
            ), (conn, rid)


def test_status_bar_never_shows_a_fixed_label_for_a_missing_battery(sim):
    sim.fixtures.battery_pct = -1
    sim.render()
    assert sim.ui.log.status.endswith("--%")


# ---- [ ] No fake functional toggles -----------------------------------------------------------------------


def test_home_01_an_unavailable_device_is_not_toggled_and_no_success_is_shown(preview):
    preview.goto("SH11")
    preview.press("down")  # Fan: unavailable (not available in the fixture)
    preview.press("enter")
    assert "unavailable: not switched" in preview.toast_text and "Fan: unknown" in screen_text(preview)
    assert preview.executed == [] and preview.dev_states == {}


def test_a_device_is_not_toggled_when_the_integration_is_offline(preview):
    preview.set_connectivity(ConnState.OFFLINE)
    preview.goto("SH11")
    assert preview.view().state == "error" and "offline" in screen_text(preview).lower()
    preview.press("enter")  # nothing to toggle: the only action is a retry
    assert preview.dev_states == {} and preview.executed == [] and preview.toast_text == "Retrying..."
    preview._h_toggle_device(("Living room", "Lamp"))  # even a direct request is refused
    assert "not switched" in preview.toast_text and preview.dev_states == {}


def test_a_toggle_changes_state_only_through_an_acknowledged_change(preview):
    preview.goto("SH11")
    preview.press("enter")
    assert preview.toast_text == "Lamp: off (acknowledged)" and "Lamp: off" in screen_text(preview)
    preview.press("enter")
    assert "Lamp: on" in screen_text(preview)


def test_a_device_without_state_has_nothing_to_toggle(preview):
    preview.goto("SH11")
    preview.nav("SH11").sel = 2  # TV (IR): "no feedback"
    preview.press("enter")
    assert "no state to toggle" in preview.toast_text


def test_unavailable_modules_have_no_controls_in_the_firmware_profile(sim):
    for key in ("3", "4", "5", "6"):
        sim.press(key)
        assert "Not available in this build" in screen_text(sim).replace(
            "\n", " "
        ) or "unavailable" in screen_text(sim)
        before = (sim.cur, list(sim.stack), list(sim.executed), sim.task)
        for chord in ("enter", "down", "right", "left", "up"):
            sim.press(chord)
        assert (sim.cur, list(sim.stack), list(sim.executed), sim.task) == before
        sim.press("`")
        assert sim.cur == "H01"


def test_orig_01_original_apps_shows_an_explicit_unavailable_state_and_no_switch(sim, preview):
    for s in (sim, preview):
        s.press("6")
        assert s.cur == "O10"
        text = screen_text(s).replace("\n", " ")
        assert "unavailable" in text.lower()
        s.press("enter")
        assert s.cur == "O10" and s.executed == [] and s.task is None
        s.press("`")
        assert s.cur == "H01"


def test_the_original_apps_text_names_the_simulator_limit():
    s = at("O10", preview=True)
    assert "unavailable in simulator" in s.view().body


def test_brightness_is_the_only_toggle_and_it_changes_a_real_value(sim):
    sim.goto("P10")
    sim.press("down")
    assert "Brightness: 3/5" in screen_text(sim)
    for _ in range(6):
        sim.press("fn+/")
    assert sim.brightness == 4 and "Brightness: 5/5" in screen_text(sim)
    for _ in range(9):
        sim.press(",")
    assert sim.brightness == 0


# ---- [ ] Long SSIDs / BLE names are safely clipped --------------------------------------------------------


def test_fit_clips_with_a_tilde_and_replaces_non_printable_characters():
    assert fit("abcdef", 6) == "abcdef"
    assert fit("abcdefg", 6) == "abcde~"
    assert fit("a\x07bé", 8) == "a?b?"
    assert fit("abc", 0) == ""


def test_wrap_next_breaks_on_spaces_and_on_newlines():
    assert wrap_next("one two three", 8) == ("one two", 8)
    assert wrap_next("a\nb", 8) == ("a", 2)
    assert wrap_next("", 8) == ("", 0)
    assert wrap_next("abcdefghij", 4) == ("abcd", 4)


def test_long_ssids_and_ble_names_are_clipped_in_lists_and_complete_in_the_detail():
    th = theme()
    s = at("S12")
    s.render()
    rows = []
    for _ in range(9):
        rows += s.ui.log.rows
        s.press(".")
        s.render()
    long_rows = [r for r in rows if "A-very-long" in r]
    assert long_rows and all(len(r) <= th.ListCols and r.endswith("~") for r in long_rows)
    assert any("<non-ASCII>" in r for r in rows) and any("<hidden>" in r for r in rows)
    assert all(len(r) <= th.ListCols for r in rows)
    # No text pixel may reach the scrollbar column (the panel ends at x 234, the bar sits in the margin).
    s.goto("S12")
    s.render()
    margin = {pixel(s, x, y) for x in range(th.ScrollX - 1, 240) for y in range(th.RowsY, th.RowsY + 80)}
    assert th.TextPrimary.rgb888 not in margin
    s.nav("S12").sel = 3  # the long name is the fourth strongest
    s.press("enter")
    assert "A-very-long-network-name-that-overflows-the-row" in s.view().body


def test_ble_list_clips_long_names_and_falls_back_to_the_address():
    s = at("S22")
    s.render()
    rows = []
    for _ in range(6):
        rows += s.ui.log.rows
        s.press(".")
        s.render()
    assert any(r.endswith("~") and "A-device-with" in r for r in rows)
    assert any("02:00:00:00:10:03" in r for r in rows)  # the nameless advertiser shows its address
    assert all(len(r) <= theme().ListCols for r in rows)


# ---- [ ] Zero-scan results handled (SEC-01, SEC-02) -------------------------------------------------------


def test_sec_01_a_survey_shows_its_results_without_crashing(sim):
    start_wifi(sim)
    sim.advance(4000)
    assert sim.cur == "S12" and len(sim.view().rows) == 9
    assert "OfficeWiFi" in screen_text(sim)


def test_sec_02_no_access_points_shows_the_empty_state(sim):
    sim.scan_data = "zero"
    start_wifi(sim)
    sim.advance(4000)
    assert sim.cur == "S12" and sim.wifi_results == []
    text = screen_text(sim)
    assert "No networks seen (2.4 GHz)" in text and "0/0" in text
    sim.press("enter")  # nothing to inspect
    sim.press("s")
    assert sim.cur == "S12"
    sim.press("`")
    assert "Results (0)" in screen_text(sim)


def test_no_discovery_and_zero_advertisers_each_have_their_own_empty_text(sim):
    sim.goto("S22")
    sim.ble_results = None
    assert "Start a discovery first" in screen_text(sim)
    sim.scan_data = "zero"
    sim.goto("S21")
    sim.press("enter")
    sim.advance(5000)
    assert sim.cur == "S22" and "No advertisers seen" in screen_text(sim)


def test_results_screens_before_any_scan_say_so(sim):
    sim.goto("S12")
    sim.wifi_results = None
    assert "Start a survey first" in screen_text(sim) and "no survey yet" in screen_text(sim)


def test_sorting_the_results_changes_the_order_and_restarts_at_the_top(sim):
    sim.goto("S12")
    first_rssi = sim.view().rows[0].text
    sim.press("s")
    assert sim.wifi_sort == "ch" and sim.view().rows[0].text != first_rssi and sim.nav("S12").sel == 0
    assert "by ch" in screen_text(sim)


# ---- [ ] Show a clearly disabled state for unfinished modules ---------------------------------------------


def test_selecting_an_unfinished_module_explains_it_and_back_returns_home(sim):
    th = theme()
    sim.press("3")
    assert sim.cur == "L10"
    text = screen_text(sim)
    assert "Linux: unavailable" in text and "Not available in this build." in text
    sim.render()
    assert pixel(sim, 0, th.HeadingY) == th.SurfaceBase.rgb888  # an explanation, not an error alert
    sim.press("`")
    assert sim.cur == "H01"


def test_unfinished_modules_become_screens_only_in_the_preview_profile(preview):
    preview.press("4")
    assert preview.cur == "D10" and "CONTROL DECK" in screen_text(preview)
    assert "SIMULATED" in preview.ui.log.status


# ---- connectivity-dependent views (NET-01, AI) ------------------------------------------------------------


def test_net_01_linux_with_the_companion_offline_shows_an_explanatory_view(preview):
    preview.set_connectivity(ConnState.OFFLINE)
    preview.press("3")
    text = screen_text(preview)
    assert preview.cur == "L10" and preview.view().state == "error"
    assert "offline" in text.lower() and "No data is shown" in preview.view().body
    preview.set_connectivity(ConnState.ONLINE)
    assert "MINI-PC" in screen_text(preview)


def test_oyster_ai_refuses_a_question_while_offline_and_sends_nothing(sim):
    sim.set_connectivity(ConnState.AUTH_ERROR)
    sim.goto("A10")
    assert "Oyster AI: unavailable" in screen_text(sim)
    sim.press("enter")
    assert sim.cur == "msg" and "auth-err" in screen_text(sim) and sim.requests_sent == 0
    sim.press("`")
    assert sim.cur == "A10"


def test_linux_health_is_read_only_and_never_asks_for_confirmation(preview):
    preview.goto("L11")
    preview.press("enter")  # System health
    assert preview.cur == "msg" and "Read-only" in preview.view().body and preview.modal is None
    preview.press("`")
    preview.press("down")
    preview.press("enter")  # Services
    preview.press("down")
    preview.press("enter")  # a service
    assert preview.modal is None and preview.executed == []


def test_an_offline_host_shows_no_data(preview):
    preview.goto("L10")
    preview.press("down")
    preview.press("enter")
    assert preview.cur == "msg" and "NAS-01: offline" in screen_text(preview)


def test_deck_slot_shortcuts_open_a_summary_and_never_run_anything(preview):
    preview.goto("D10")
    preview.press("2")
    assert preview.cur == "D11" and preview.executed == [] and preview.modal is None
    assert (
        "Confirmation: required" in screen_text(preview).replace("\n", " ")
        or "required" in preview.view().body
    )


def test_every_screen_id_is_reachable_by_keys_from_home_and_leaves_with_back():
    """A walk through the design navigation graph using only physical keys."""
    s = Simulator(preview=True)
    walk = [
        ("1", "S10"),
        ("enter", "S11"),
        ("`", "S10"),
        (".", "S10"),
        ("enter", "S21"),
        ("`", "S10"),
        ("`", "H01"),
        ("2", "A10"),
        ("`", "H01"),
        ("3", "L10"),
        ("enter", "L11"),
        ("down", "L11"),
        ("enter", "L12"),
        ("`", "L11"),
        ("down", "L11"),
        ("enter", "L13"),
        ("fn+del", "H01"),
        ("4", "D10"),
        ("1", "D11"),
        ("`", "D10"),
        ("fn+del", "H01"),
        ("5", "SH10"),
        ("enter", "SH11"),
        ("fn+del", "H01"),
        ("6", "O10"),
        ("`", "H01"),
        ("7", "P10"),
        ("fn+del", "H01"),
        ("8", "help"),
        ("`", "H01"),
    ]
    for chord, route in walk:
        s.advance(800)
        s.press(chord)
        assert s.cur == route, f"after {chord!r}: on {s.cur}, expected {route}"
    assert {r for r in BY_ID} >= {"S21", "L13", "D11", "SH11", "O10", "P10"}
