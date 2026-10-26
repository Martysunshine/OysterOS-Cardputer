"""The route table against the design documents and the firmware sources: every design screen ID has a route,
every route renders in every state, and the simulator agrees with docs/UI_SCREEN_MAP.md about which screens
are implemented and which are unavailable."""

from __future__ import annotations

import ast
import re

import pytest
from oyster_ui_sim import views
from oyster_ui_sim.engine import Simulator
from oyster_ui_sim.routes import (
    BY_ID,
    DESIGN_IDS,
    IMPLEMENTED,
    KINDS,
    ROUTES,
    STATES,
    UNAVAILABLE,
    UNAVAILABLE_TEXT,
    ancestors,
    check_table,
)
from oyster_ui_sim.theme import theme
from sim_support import FIRMWARE, REPO, colour_set, expand_ids, pixel, read, screen_map_rows, screen_text

DESIGN = (REPO / "docs" / "DESIGN_SYSTEM.md").read_text(encoding="utf-8")
APP_CPP = read(FIRMWARE / "src" / "app_controller.cpp")


def test_route_table_is_structurally_valid_and_acyclic():
    check_table()
    for r in ROUTES:
        assert r.kind in KINDS
        chain = ancestors(r.id)
        assert len(chain) < 6, r.id
        assert not chain or chain[0] == "H01", r.id  # every Back chain ends at the root
        assert r.states == STATES


def test_every_screen_id_of_the_design_system_has_a_route():
    section = DESIGN.split("## Mockup screen IDs", 1)[1].split("##", 1)[0]
    ids = set(re.findall(r"\b([A-Z]{1,2}\d{2})\b", section))
    assert ids == set(DESIGN_IDS)
    assert ids <= set(BY_ID)
    assert len(ids) == 26


def test_the_design_navigation_graph_is_reachable_from_home_by_enter_targets():
    reach, queue = {"H01"}, ["H01"]
    while queue:
        for _, tgt in BY_ID[queue.pop()].enter:
            if tgt not in reach:
                reach.add(tgt)
                queue.append(tgt)
    # A12 is entered when a question finishes (A11 turns into A12), not through a row.
    assert set(DESIGN_IDS) - {"A12"} <= reach


@pytest.mark.parametrize("preview", [False, True], ids=["firmware", "preview"])
def test_every_route_renders_in_every_state(preview):
    th = theme()
    for rt in ROUTES:
        for state in STATES:
            s = Simulator(preview=preview)
            s.goto(rt.id, state)
            s.advance(1000)
            text = screen_text(s)  # renders the frame
            assert s.ui.screen.image(1).size == (240, 135), (rt.id, state)
            assert text.strip(), (rt.id, state)
            if preview or rt.availability == IMPLEMENTED:
                if state == "loading":
                    assert "IN PROGRESS" in text, (rt.id, state)
                if state == "error":
                    assert pixel(s, 0, th.HeadingY) == th.Error.rgb888, (rt.id, state)
                if state == "disabled" and rt.id not in ("H01", "S10", "S21"):
                    assert (
                        "Not available in this build" in text.replace("\n", " ") or "unavailable" in text
                    ), (
                        rt.id,
                        state,
                    )
            elif rt.availability == UNAVAILABLE:
                assert "unavailable" in text.lower(), (rt.id, state)


def test_empty_state_texts_fit_one_row():
    th = theme()
    for rt in ROUTES:
        if rt.kind in ("menu", "list"):
            assert len(rt.empty) <= th.ListCols, rt.id


def test_empty_state_shows_the_routes_empty_text(sim):
    for rid in ("S11", "S12", "S22", "targets", "gatt", "A10", "P10"):
        sim.goto(rid, "empty")
        assert BY_ID[rid].empty in screen_text(sim), rid


# ---- agreement with docs/UI_SCREEN_MAP.md -----------------------------------------------------------------


def test_screen_map_and_simulator_agree_on_implemented_versus_unavailable_screens():
    rows = screen_map_rows()
    assert rows, "no screen table parsed from UI_SCREEN_MAP.md"
    seen: set[str] = set()
    for ids, status in rows:
        want = UNAVAILABLE if status.lower().startswith("unavailable") else IMPLEMENTED
        assert status.lower().startswith(("implemented", "unavailable")), status
        for sid in ids:
            assert sid in BY_ID, f"{sid} is in the screen map but not in the simulator"
            assert BY_ID[sid].availability == want, (
                f"{sid}: map says {want}, simulator says {BY_ID[sid].availability}"
            )
            seen.add(sid)
    assert seen == set(DESIGN_IDS), f"screen map and design differ: {seen ^ set(DESIGN_IDS)}"
    # And nothing the simulator calls unavailable is missing from the map's unavailable rows.
    unavailable = {r.id for r in ROUTES if r.availability == UNAVAILABLE}
    assert unavailable == {sid for ids, st in rows if st.lower().startswith("unavailable") for sid in ids}


def test_expand_ids_handles_ranges_and_lists():
    assert expand_ids("L10-L13") == ["L10", "L11", "L12", "L13"]
    assert expand_ids("D10, D11, D20-D22") == ["D10", "D11", "D20", "D21", "D22"]
    assert expand_ids("SH10-SH12") == ["SH10", "SH11", "SH12"]
    assert expand_ids("(dialog)") == []


def test_unavailable_screen_texts_are_the_firmware_texts():
    block = APP_CPP.split("const UnavailableModule kUnavailable[] = {", 1)[1].split("};", 1)[0]
    found = []
    for m in re.finditer(r'\{\s*"([^"]+)",\s*((?:"(?:[^"\\]|\\.)*"\s*)+)\}', block):
        body = "".join(ast.literal_eval(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', m.group(2)))
        found.append((m.group(1), body))
    assert len(found) == 4
    assert found == [UNAVAILABLE_TEXT[k] for k in ("linux", "deck", "smart", "original")]


def test_home_tiles_are_the_firmware_tiles():
    block = APP_CPP.split("const HomeTile kHome[] = {", 1)[1].split("};", 1)[0]
    fw = re.findall(r'\{HomeItem::\w+, (\d), "([^"]+)", assets::IconId::(\w+)\}', block)
    mine = [(str(n), label, icon or "Count") for _, label, icon, n, _ in views.HOME_TILES]
    assert fw == mine


def test_static_labels_exist_in_the_firmware_source():
    labels = [
        "Start survey (passive)",
        "Discover (passive)",
        "Discover (active)",
        "Ask a question",
        "Latest AI run",
        "Cancel AI run",
        "Sync evidence now",
        "Diagnostics",
        "New session",
        "Keyboard",
        "Key tester",
        "About",
        "Wi-Fi Survey",
        "Bluetooth Explorer",
        "Saved Findings",
        "Enrolled Targets",
        "Assess (GATT)",
        "Inspect GATT",
        "Last read result",
        "OYSTER OS",
        "Saved Findings",
        "IN PROGRESS",
        "CONFIRM ACTION",
        "Ent:Approve Del:Reject",
        "?:Help 1-8:Open",
        "Del:Back ;.:Scroll",
        "Ent:OK Del:Erase",
    ]
    cpp = APP_CPP + read(FIRMWARE / "src" / "ui" / "widgets.cpp")
    missing = [x for x in labels if x not in cpp]
    assert not missing, missing


def test_every_job_route_has_an_error_state_that_renders_as_an_alert():
    th = theme()
    jobs = [r for r in ROUTES if r.job]
    assert {r.job for r in jobs} >= {"wifi", "ble", "gatt", "ai", "deck", "ir"}
    for rt in jobs:
        s = Simulator(preview=True)
        s.goto(rt.id, "error")
        s.advance(1000)
        s.render()
        assert pixel(s, 0, th.HeadingY) == th.Error.rgb888, rt.id
        assert s.view().state in ("error",), rt.id


def test_every_home_tile_is_enabled(sim, preview):
    th = theme()
    sim.render()
    labels = {r.text: r.enabled for r in sim.view().rows}
    # Every module tile is implemented now (tile 3 is the on-device WiFi recon; Original Apps is an
    # informational tile). None is greyed out, in either profile.
    assert labels == {
        "Security": True,
        "Control": True,
        "Oyster AI": True,
        "SmartHome": True,
        "WiFi Scan": True,
        "Original": True,
        "Settings": True,
        "Help": True,
    }
    preview.render()
    assert all(r.enabled for r in preview.view().rows)
    # An enabled tile label is drawn in the primary text colour, not the muted (disabled) one.
    cell = (th.PadX + 4, th.ContentY + 4, 100, 24)
    a = sim.ui.screen.image(1).crop((cell[0], cell[1], cell[0] + cell[2], cell[1] + cell[3]))
    assert tuple(th.TextPrimary.rgb888) in colour_set(a)
