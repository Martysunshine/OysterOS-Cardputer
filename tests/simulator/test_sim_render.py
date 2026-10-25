"""Pixel-level behaviour of the renderer: exact size, integer nearest-neighbour scaling, palette-only colours,
focus drawing, region geometry, determinism and speed."""

from __future__ import annotations

import hashlib
import statistics
import time

import pytest
from oyster_ui_sim.engine import Simulator
from oyster_ui_sim.render import png_bytes, render_all, render_route
from oyster_ui_sim.theme import theme
from PIL import Image
from sim_support import colour_set, pixel, screen_text


def test_frame_is_exactly_240_by_135_and_scales_by_integers(sim):
    assert sim.frame(1).size == (240, 135)
    for k in (1, 2, 4, 6):
        assert sim.frame(k).size == (240 * k, 135 * k)


def test_scaling_is_nearest_neighbour_without_anti_aliasing(sim):
    sim.goto("S12")
    one, four = sim.frame(1), sim.frame(4)
    px1, px4 = one.load(), four.load()
    for y in range(0, 135, 7):
        for x in range(0, 240, 5):
            block = {px4[x * 4 + i, y * 4 + j] for i in range(4) for j in range(4)}
            assert block == {px1[x, y]}


@pytest.mark.parametrize("route", ["S12", "S13", "P10", "A12", "D20", "S11", "help", "P11"])
def test_only_palette_colours_are_drawn_on_text_screens(preview, route):
    preview.goto(route)
    preview.advance(1000)
    colours = colour_set(preview.frame(1))
    allowed = {tuple(c.rgb888) for c in theme().colors.values()}
    assert colours <= allowed, colours - allowed


def test_status_bar_heading_content_and_footer_regions(sim):
    th = theme()
    sim.goto("P10")
    sim.render()
    assert pixel(sim, 0, th.StatusH) == th.SurfacePanel.rgb888  # divider at y 15
    assert pixel(sim, 0, th.HeadingY) == th.SurfaceBase.rgb888
    assert pixel(sim, 1, th.ContentY + 10) == th.SurfaceBase.rgb888  # left margin
    assert pixel(sim, 120, th.RowsY + th.RowH * 2 + 8) == th.SurfacePanel.rgb888  # panel row
    assert pixel(sim, 0, th.FooterY + 8) == th.SurfaceBase.rgb888
    assert pixel(sim, 239, th.FooterY + 15) == th.SurfaceBase.rgb888  # last footer pixel row y 134 exists


def test_selected_row_has_stripe_marker_and_focus_background(sim):
    th = theme()
    sim.goto("P10")
    sim.render()
    y0 = th.RowsY
    stripe = {pixel(sim, th.PadX + i, y0 + j) for i in range(th.StripeW) for j in range(th.RowH)}
    assert stripe == {th.Accent.rgb888}
    assert pixel(sim, th.PadX + th.InnerW - 6, y0 + 8) == th.SurfaceFocus.rgb888
    assert pixel(sim, th.PadX + th.InnerW - 6, y0 + th.RowH + 8) == th.SurfacePanel.rgb888  # unselected row
    marker = [
        pixel(sim, x, y0 + r)
        for x in range(th.PadX + th.StripeW, th.PadX + th.StripeW + 8)
        for r in range(16)
    ]
    assert th.Accent.rgb888 in marker  # the '>' glyph: focus is never colour alone
    unselected = [pixel(sim, x, y0 + th.RowH + r) for x in range(th.PadX, th.PadX + 11) for r in range(16)]
    assert th.Accent.rgb888 not in unselected


def test_alert_and_warning_headings_use_their_tones(sim):
    th = theme()
    sim.goto("A10", "error")
    sim.render()
    assert pixel(sim, 0, th.HeadingY) == th.Error.rgb888
    sim.goto("confirm")
    sim.advance(800)
    sim.render()
    assert pixel(sim, 0, th.HeadingY) == th.Warning.rgb888


def test_scrollbar_sits_in_the_right_margin_and_only_when_scrolling(sim):
    th = theme()
    sim.goto("P10")  # 9 rows > 5 visible
    sim.render()
    assert pixel(sim, th.ScrollX + 1, th.RowsY + 1) == th.Accent.rgb888
    sim.goto("S11")  # 3 rows: no scrollbar
    sim.render()
    assert th.Accent.rgb888 not in {pixel(sim, th.ScrollX + 1, y) for y in range(th.RowsY, th.RowsY + 80)}


def test_render_is_deterministic_png_bytes_for_the_same_input():
    a = png_bytes(render_route("S12", "normal", scale=4))
    b = png_bytes(render_route("S12", "normal", scale=4))
    assert a == b
    other = png_bytes(render_route("S12", "empty", scale=4))
    assert a != other


def test_two_simulators_given_the_same_keys_render_identical_frames():
    def run() -> bytes:
        s = Simulator()
        for chord in ("1", "enter", "enter"):
            s.press(chord)
        s.advance(4200)
        s.press(".")
        return png_bytes(s.frame(2))

    assert run() == run()


def test_headless_export_is_reproducible(tmp_path):
    def digest(out):
        render_all(out, 1, only=["S12", "D20"])
        return {
            p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.rglob("*.png"))
        }

    first, second = digest(tmp_path / "a"), digest(tmp_path / "b")
    assert first == second
    assert len(first) == 2 * 5 * 2 + 2  # routes x states x profiles + one contact sheet per profile


@pytest.mark.parametrize("route", ["H01", "S10", "S12", "A12", "P10"])
def test_a_screen_renders_in_under_50_ms(sim, route):
    sim.goto(route)
    sim.render()  # warm the glyph and icon caches
    times = []
    for _ in range(7):
        t = time.perf_counter()
        sim.render()
        times.append(time.perf_counter() - t)
    assert statistics.median(times) < 0.05, times


def test_the_loading_screen_shows_elapsed_time_from_the_simulation_clock(sim):
    sim.goto("S12", "loading")
    sim.advance(3000)
    assert "Elapsed: 3 s" in screen_text(sim)
    sim.advance(2000)
    assert "Elapsed: 5 s" in screen_text(sim)


def test_exported_png_has_the_requested_size(tmp_path):
    render_all(tmp_path, 3, only=["H01"])
    img = Image.open(tmp_path / "firmware" / "H01_normal.png")
    assert img.size == (720, 405)
