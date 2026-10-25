"""Keyboard emulation (ported input mapping, key table vs the firmware source), the script replay language,
the command line and the desktop window glue."""

from __future__ import annotations

import re

import pytest
from oyster_ui_sim import __main__ as cli
from oyster_ui_sim.keys import (
    EVENT_TO_ACTION,
    KEY_TABLE,
    Action,
    ChordError,
    KeySnapshot,
    NavEvent,
    describe_snapshot,
    map_keys,
    parse_chord,
    snapshot_from_pc_key,
)
from oyster_ui_sim.script import ScriptError, run_script
from sim_support import FIRMWARE, read

INPUT_CPP = read(FIRMWARE / "lib" / "oyster_core" / "src" / "input_map.cpp")


def events(chord: str, text_mode: bool = False) -> list[str]:
    return [
        e.value if e is not NavEvent.CHAR else f"Char:{c}" for e, c in map_keys(parse_chord(chord), text_mode)
    ]


# ---- the key table against input_map.cpp ------------------------------------------------------------------


def test_key_table_agrees_with_the_firmware_input_map_source():
    arrows = dict(re.findall(r"case '(.)': return NavEvent::(\w+);", INPUT_CPP))
    assert arrows == {";": "Up", ".": "Down", ",": "Left", "/": "Right"}
    table = {b.action: b for b in KEY_TABLE}
    for ch, name in arrows.items():
        action = {"Up": "MOVE_UP", "Down": "MOVE_DOWN", "Left": "MOVE_LEFT", "Right": "MOVE_RIGHT"}[name]
        assert ch in table[action].keys and f"fn+{ch}" in table[action].keys
    back_char = re.search(r"c == '(.)'\) \{\s*push\(out, NavEvent::Back\);", INPUT_CPP).group(1)
    help_char = re.search(r"c == '(.)'\) \{[^}]*?push\(out, NavEvent::Help\);", INPUT_CPP, re.S).group(1)
    assert back_char in table["BACK"].keys and "del" in table["BACK"].keys
    assert help_char in table["HELP"].keys
    assert re.search(
        r"is_home\(const KeySnapshot& k\) \{ return k\.fn && \(k\.backspace \|\| k\.del\); \}", INPUT_CPP
    )
    assert "fn+del" in table["HOME"].keys
    cancel = re.search(r"bool is_cancel\(const KeySnapshot& k\) \{(.*?)\n\}", INPUT_CPP, re.S).group(1)
    assert (
        "k.fn && k.esc" in cancel and "has_char(k, '`')" in cancel and "k.ctrl && (has_char(k, 'c')" in cancel
    )
    assert set(table["CANCEL"].keys) == {"fn+`", "ctrl+c"}


def test_every_physical_chord_of_the_table_resolves_to_its_logical_action():
    expect = {
        "MOVE_UP": "Up",
        "MOVE_DOWN": "Down",
        "MOVE_LEFT": "Left",
        "MOVE_RIGHT": "Right",
        "SELECT": "Select",
        "BACK": "Back",
        "HOME": "Home",
        "HELP": "Help",
        "CANCEL": "Cancel",
    }
    for b in KEY_TABLE:
        if b.action in expect:
            for chord in b.keys:
                assert events(chord) == [expect[b.action]], (b.action, chord)
    assert events("del", text_mode=True) == ["Erase"]
    assert events("a", text_mode=True) == ["Char:a"]
    assert events("1") == ["Char:1"]  # a digit is a shortcut candidate: the screen decides


def test_key_table_documents_every_logical_action_of_the_plan():
    names = {b.action for b in KEY_TABLE}
    assert {
        "MOVE_UP",
        "MOVE_DOWN",
        "MOVE_LEFT",
        "MOVE_RIGHT",
        "SELECT",
        "BACK",
        "HOME",
        "HELP",
        "CANCEL",
    } <= names
    assert {a.value for a in Action} >= {"MOVE_UP", "SELECT", "BACK", "HOME", "HELP", "CANCEL", "CONFIRM"}
    ui_map = read(FIRMWARE.parent / "docs" / "UI_SCREEN_MAP.md")
    assert "UNVERIFIED" in ui_map and all(b.keys for b in KEY_TABLE)


# ---- the ported mapper (host tests of firmware/test/test_input) -------------------------------------------


def test_cancel_wins_and_suppresses_everything_else():
    k = KeySnapshot(fn=True, esc=True, enter=True, chars=";")
    assert [e for e, _ in map_keys(k)] == [NavEvent.CANCEL]
    assert [e for e, _ in map_keys(KeySnapshot(ctrl=True, chars="C"))] == [NavEvent.CANCEL]


def test_home_is_alone_and_bare_delete_is_back_or_erase():
    assert events("fn+del") == ["Home"] and events("fn+del", text_mode=True) == ["Home"]
    assert events("del") == ["Back"] and events("del", text_mode=True) == ["Erase"]


def test_fn_layer_arrows_work_in_both_modes_and_other_fn_chords_are_ignored():
    assert events("fn+;") == ["Up"] and events("fn+;", text_mode=True) == ["Up"]
    assert events("fn+a") == []
    assert events(";", text_mode=True) == ["Char:;"], "bare arrows are text in text mode"


def test_help_and_backtick_are_navigation_only_and_modifier_chords_are_never_text():
    assert events("?") == ["Help"] and events("?", text_mode=True) == ["Char:?"]
    assert events("`") == ["Back"] and events("`", text_mode=True) == ["Char:`"]
    assert events("ctrl+x", text_mode=True) == [] and events("alt+a") == []


def test_logical_aliases_and_events_map_to_actions():
    assert events("up") == ["Up"] and events("home") == ["Home"] and events("cancel") == ["Cancel"]
    assert events("esc") == ["Back"] and events("enter") == ["Select"] and events("tab") == ["Tab"]
    assert (
        EVENT_TO_ACTION[NavEvent.SELECT] is Action.SELECT
        and EVENT_TO_ACTION[NavEvent.CHAR] is Action.TEXT_INPUT
    )


def test_chord_parser_rejects_nonsense():
    for bad in ("", "hyper+x", "fn+bogus", "ctrl+"):
        with pytest.raises(ChordError):
            parse_chord(bad)
    assert parse_chord("shift+/").chars == "?" and parse_chord("fn++").chars == "+"
    assert parse_chord("space").space and parse_chord("fn+backspace").backspace


def test_describe_snapshot_matches_the_key_tester_format():
    k = parse_chord("fn+del")
    assert describe_snapshot(k, map_keys(k)) == ("Fn Del", "Home")
    k = parse_chord("a")
    assert describe_snapshot(k, map_keys(k, True)) == ("'a'", "Char")
    assert describe_snapshot(KeySnapshot(), []) == ("(released)", "none")


def test_desktop_keys_translate_to_the_physical_snapshot():
    assert snapshot_from_pc_key("Up", "", False, False, False).up
    assert [e for e, _ in map_keys(snapshot_from_pc_key("Home", "", False, False, False))] == [NavEvent.HOME]
    assert [e for e, _ in map_keys(snapshot_from_pc_key("c", "\x03", True, False, False))] == [
        NavEvent.CANCEL
    ]
    assert [e for e, _ in map_keys(snapshot_from_pc_key("F1", "", False, False, False))] == [NavEvent.HELP]
    assert [e for e, _ in map_keys(snapshot_from_pc_key("grave", "`", False, False, True))] == [
        NavEvent.CANCEL
    ]
    assert snapshot_from_pc_key("Shift_L", "", False, True, False) is None


# ---- script replay ----------------------------------------------------------------------------------------


def test_script_replays_keys_waits_and_checks(tmp_path):
    script = """
    # Home > Security > Wi-Fi: survey, results, detail
    expect route H01
    key 1 enter enter
    expect text Wi-Fi survey
    wait 4000
    expect route S12
    shot results
    key enter
    expect route S13
    key fn+del
    expect route H01
    conn offline
    key 2 enter
    expect route msg
    expect text AI unavailable
    """
    res = run_script(script, tmp_path)
    assert res.checks == 7 and [p.name for p in res.frames] == ["results.png"]
    assert (tmp_path / "results.png").stat().st_size > 100


def test_script_profile_goto_state_and_event_commands(tmp_path):
    res = run_script(
        "profile preview\ngoto D20\nexpect modal D20\nwait 800\nkey `\nexpect modal none\n"
        "goto S12 empty\nexpect state empty\nevent confirm\nexpect modal confirm\nkey fn+del\n"
        "expect modal confirm\nwait 800\nkey fn+del\ntype #not a comment\nexpect route H01\n",
        tmp_path,
    )
    # Home inside the 700 ms guard is ignored; after it the dialog is rejected and Home runs.
    assert res.checks == 6 and res.sim.cur == "H01" and res.sim.modal is None


def test_script_errors_name_the_line():
    for text, needle in (
        ("key\n", "line 1"),
        ("bogus 1\n", "unknown command"),
        ("goto nowhere\n", "goto <route>"),
        ("\nexpect route S12\n", "line 2"),
        ("key hyper+x\n", "unknown modifier"),
        ("expect text nothing-here\n", "not on screen"),
    ):
        with pytest.raises(ScriptError, match=needle):
            run_script(text)


# ---- command line -----------------------------------------------------------------------------------------


def test_cli_prints_the_route_table_and_the_key_table(capsys):
    assert cli.main(["--routes"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") >= 42 and "| P11 |" in out and "| D20 |" in out and "unavailable" in out
    assert cli.main(["--keys"]) == 0
    out = capsys.readouterr().out
    assert "HOME" in out and "Fn+Del" in out and "UNVERIFIED" in out


def test_cli_replays_a_script_file(tmp_path, capsys):
    f = tmp_path / "keys.txt"
    f.write_text("key 7\nexpect route P10\nshot settings\n", encoding="utf-8")
    assert cli.main(["--script", str(f), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "settings.png").is_file()
    assert "1 frames, 1 checks passed" in capsys.readouterr().out
    f.write_text("key 7\nexpect route H01\n", encoding="utf-8")
    assert cli.main(["--script", str(f)]) == 1
    assert "script failed: line 2" in capsys.readouterr().err
    assert cli.main(["--script", str(tmp_path / "missing.txt")]) == 2


def test_cli_headless_needs_an_output_folder_and_a_sane_scale(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--headless"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["--scale", "0", "--routes"])


def test_shipped_example_script_passes():
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "simulator" / "examples" / "navigation.keys.txt"
    res = run_script(example.read_text(encoding="utf-8"))
    assert res.checks >= 10


# ---- desktop window ---------------------------------------------------------------------------------------


def test_desktop_window_translates_keys_and_renders_frames():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    try:
        from oyster_ui_sim.tk_app import App

        app = App(root, 2, False)

        class Ev:
            def __init__(self, keysym, char="", state=0):
                self.keysym, self.char, self.state = keysym, char, state

        assert app.photo is not None
        app.on_key(Ev("1", "1"))
        assert app.sim.cur == "S10"
        app.on_key(Ev("Escape"))
        assert app.sim.cur == "H01"
        app.on_key(Ev("Alt_L"))
        app.on_key(Ev("BackSpace"))  # Fn+Del
        app.on_key(Ev("Alt_L"))
        app.on_key_up(Ev("Alt_L"))
        assert app.fn_alt is False
        app.conn.set("OFFLINE")
        app.on_conn()
        assert app.sim.conn.short == "offline"
        app.state.set("error")
        app.on_state()
        assert app.sim.state_of() == "error"
        app.profile.set("preview")
        app.on_profile()
        assert app.sim.preview
        app.route.set("D11")
        app.on_route()
        assert app.sim.cur == "D11"
        app.on_confirm()
        assert app.sim.modal is not None
    finally:
        root.destroy()
