"""Design tokens, layout constants, glyph tables and icons: the simulator must read the firmware's own
values (ui_theme.h, M5GFX font tables, assets_gen.cpp) and fail loudly when they cannot be parsed."""

from __future__ import annotations

import re

import pytest
from oyster_ui_sim import assets, glyphs
from oyster_ui_sim.canvas import FONT_BODY, FONT_META, Canvas
from oyster_ui_sim.theme import (
    THEME_HEADER,
    Theme,
    ThemeParseError,
    load_theme,
    parse_header,
    rgb565,
    theme,
)
from sim_support import DOCS, FIRMWARE, read

TOKENS = {  # DESIGN_SYSTEM.md token name -> ui_theme.h constant
    "Background": "kSurfaceBase",
    "Panel": "kSurfacePanel",
    "Focus": "kSurfaceFocus",
    "Accent": "kAccent",
    "Text": "kTextPrimary",
    "Secondary": "kTextMuted",
    "Warning": "kWarning",
    "Error": "kError",
}


def test_palette_equals_the_design_tokens_of_the_design_system_document():
    rows = dict(re.findall(r"^\| (\w+) \| `#([0-9A-Fa-f]{6})` \|", read(DOCS / "DESIGN_SYSTEM.md"), re.M))
    assert set(TOKENS) <= set(rows), "token table of DESIGN_SYSTEM.md not found"
    th = theme()
    for doc_name, const in TOKENS.items():
        assert th.colors[const].rgb565 == rgb565(int(rows[doc_name], 16)), doc_name


def test_layout_equals_the_spatial_sizing_of_the_design_system_document():
    text = read(DOCS / "DESIGN_SYSTEM.md")
    th = theme()
    spans = {
        "Status bar": (th.StatusY, th.StatusY + th.StatusH - 1),
        "Heading": (th.HeadingY, th.HeadingY + th.HeadingH - 1),
        "Content": (th.ContentY, th.ContentY + th.ContentH - 1),
        "Action strip": (th.FooterY, th.FooterY + th.FooterH - 1),
    }
    for name, got in spans.items():
        m = re.search(rf"{name}: y (\d+)[–-](\d+)", text)
        assert m, name
        assert got == (int(m.group(1)), int(m.group(2))), name
    assert (th.ScreenW, th.ScreenH, th.PadX, th.StripeW) == (240, 135, 5, 3)


def test_theme_values_come_from_the_header_not_from_a_copy():
    ints, colors = parse_header(read(THEME_HEADER))
    th = theme()
    assert th.ListCols == ints["kListCols"] == 27
    assert th.TextCols == ints["kTextCols"] == 28
    assert th.Accent.rgb565 == colors["kAccent"]


def test_theme_parsing_fails_loudly_when_a_constant_is_missing():
    text = read(THEME_HEADER)
    broken = re.sub(r"constexpr uint16_t kAccent[^\n]*\n", "", text)
    ints, colors = parse_header(broken)
    with pytest.raises(ThemeParseError, match="kAccent"):
        Theme(ints, colors)


def test_theme_parsing_fails_loudly_on_a_wrong_screen_size_or_odd_expression(tmp_path):
    text = read(THEME_HEADER)
    wrong = text.replace("constexpr int kScreenW = 240;", "constexpr int kScreenW = 320;")
    ints, colors = parse_header(wrong)
    with pytest.raises(ThemeParseError, match="240x135"):
        Theme(ints, colors)
    with pytest.raises(ThemeParseError):
        parse_header("constexpr int kPadX = foo(3);")
    with pytest.raises(ThemeParseError):
        load_theme(tmp_path / "missing.h")


def test_cell_metrics_equal_the_firmware_fonts():
    th = theme()
    assert (FONT_BODY.width, FONT_BODY.height) == (th.BodyW, th.BodyH) == (8, 16)
    assert (FONT_META.width, FONT_META.height) == (th.MetaW, th.MetaH) == (6, 8)
    widgets_cpp = read(FIRMWARE / "src" / "ui" / "widgets.cpp")
    assert "AsciiFont8x16" in widgets_cpp and "Font0" in widgets_cpp


def test_body_glyphs_are_the_m5gfx_bitmaps():
    """Glyphs extracted from M5GFX: the first 'A' row bytes are those of Ascii8x16.h."""
    c = Canvas(8, 16)
    white = theme().TextPrimary
    c.draw_string("A", 0, 0, white, FONT_BODY)
    bits = glyphs.FONT_8X16[ord("A") - glyphs.FIRST]
    for row in range(16):
        for col in range(8):
            want = bool(bits[row] & (0x80 >> col))
            assert (c.get(col, row) == white.rgb888) == want, (row, col)


def test_meta_glyphs_are_the_m5gfx_column_bitmaps():
    c = Canvas(6, 8)
    white = theme().TextPrimary
    c.draw_string("g", 0, 0, white, FONT_META)
    cols = glyphs.FONT_6X8[ord("g") - glyphs.FIRST]
    for col in range(6):
        for row in range(8):
            want = col < 5 and bool(cols[col] >> row & 1)
            assert (c.get(col, row) == white.rgb888) == want, (row, col)


def test_unknown_characters_draw_as_a_question_mark():
    a, b = Canvas(8, 16), Canvas(8, 16)
    a.draw_string("é", 0, 0, theme().Accent)
    b.draw_string("?", 0, 0, theme().Accent)
    assert a.buf == b.buf


def test_glyph_module_matches_the_pinned_m5gfx_source():
    lib = FIRMWARE / ".pio" / "libdeps" / "cardputer-adv" / "M5GFX"
    if not lib.is_dir():
        pytest.skip("PlatformIO library cache not present")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_glyphs", FIRMWARE.parent / "simulator" / "tools" / "gen_glyphs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build(lib) == read(FIRMWARE.parent / "simulator" / "oyster_ui_sim" / "glyphs.py")


def test_glyph_tables_cover_printable_ascii_and_mention_their_source():
    assert glyphs.FIRST == 0x20 and glyphs.LAST == 0x7E
    assert len(glyphs.FONT_8X16) == len(glyphs.FONT_6X8) == 95
    assert all(len(g) == 16 for g in glyphs.FONT_8X16) and all(len(g) == 5 for g in glyphs.FONT_6X8)
    assert glyphs.SOURCE.startswith("M5GFX ")
    assert "licence" in (glyphs.__doc__ or "")


def test_icons_are_the_firmware_icons():
    names = assets.icon_names()
    assert names[:7] == ("Security", "OysterAi", "Linux", "ControlDeck", "SmartHome", "Apps", "Settings")
    for name in names:
        img = assets.icon(name)
        assert (img.w, img.h, len(img.px)) == (24, 24, 576)
    assert any(p != assets.TRANSPARENT for p in assets.icon("Security").px)


def test_icon_parsing_fails_loudly_on_a_missing_array(monkeypatch, tmp_path):
    bad = tmp_path / "assets_gen.cpp"
    bad.write_text("// nothing here\n", encoding="utf-8")
    monkeypatch.setattr(assets, "ASSETS_CPP", bad)
    assets._icons.cache_clear()
    try:
        with pytest.raises(assets.AssetParseError):
            assets.icon("Security")
    finally:
        monkeypatch.undo()
        assets._icons.cache_clear()


def test_readme_carries_the_licence_texts_of_the_extracted_glyph_data():
    lib = FIRMWARE / ".pio" / "libdeps" / "cardputer-adv" / "M5GFX"
    if not lib.is_dir():
        pytest.skip("PlatformIO library cache not present")

    def norm(text: str) -> str:
        return " ".join(text.split())

    readme = norm(read(FIRMWARE.parent / "simulator" / "README.md"))
    glcd = read(lib / "src" / "lgfx" / "Fonts" / "glcdfont.h")
    bsd = glcd[glcd.index("Software License") : glcd.index("*/")]
    assert norm(read(lib / "LICENSE")) in readme
    assert norm(bsd) in readme
