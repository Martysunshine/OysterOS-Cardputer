"""UI primitives: a line-by-line port of firmware/src/ui/widgets.cpp (design tokens from ui_theme.h).

Like the firmware, every region is painted into ONE 240x16 band that is then pushed to the screen, so
overlapping bands (icon tiles) and clipping behave the same way. Conventions are the firmware's: a leading
'!' in a title or in the footer `left` text selects the alert style, '~' marks clipped text."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from . import assets
from .canvas import FONT_BODY, FONT_META, Canvas, Font
from .theme import Color, Theme, theme


class ModeBanner(enum.Enum):
    OFFLINE = "OFFLINE"
    LIVE_RADIO = "LIVE RADIO"
    AI_CONNECTED = "AI CONNECTED"
    SIMULATED = "SIMULATED"
    BACKEND_OK = "BACKEND OK"
    START_FAILED = "START FAILED"


class Conn(enum.Enum):
    UNKNOWN = "unknown"
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ONLINE = "online"
    ERROR = "error"


@dataclass
class Tile:
    icon: str | None  # IconId name; None draws the outlined '?' glyph (Help)
    label: str
    detail: str = ""
    enabled: bool = True


@dataclass
class ListModel:
    title: str
    rows: Sequence[str]
    selected: int = 0
    top: int = 0
    disabled: Sequence[bool] = ()
    empty: str = "(empty)"
    right: str = ""  # heading right text; default "sel/count"


@dataclass
class TileModel:
    title: str
    right: str
    tiles: Sequence[Tile]
    columns: int = 1
    selected: int = 0
    top_row: int = 0


@dataclass
class Scroll:
    total: int
    visible: int
    first: int


@dataclass
class DrawLog:
    """What was last drawn (for tests): the text of every region."""

    heading: str = ""
    rows: list[str] = field(default_factory=list)
    footer: str = ""
    status: str = ""  # status bar: banner, session, backend label, battery


# ---- text helpers (ports of fit / wrap_next) --------------------------------------------------------------


def fit(s: str, cols: int) -> str:
    """Printable-ASCII copy of at most `cols` characters; '~' marks truncation."""
    if cols < 1:
        return ""
    out: list[str] = []
    for ch in s:
        if ch == "\0":
            break
        if len(out) >= cols:
            out[cols - 1] = "~"
            break
        out.append(ch if " " <= ch <= "~" else "?")
    return "".join(out)


def wrap_next(s: str, cols: int) -> tuple[str, int]:
    """Word-wraps the next line of `s`: (line of <= cols characters, input characters consumed)."""
    if not s:
        return "", 0
    n, last_space = 0, -1
    while n < len(s) and s[n] != "\n" and n < cols:
        if s[n] == " ":
            last_space = n
        n += 1
    take = consumed = n
    c = s[n] if n < len(s) else "\0"
    if c == "\n":
        consumed = take + 1
    elif c not in ("\0", " ") and last_space > 0:
        take = last_space  # break before the word that does not fit
        consumed = take + 1
    elif c == " ":
        consumed = take + 1
    return fit(s[:take], cols), consumed


def wrapped_lines(body: str, cols: int) -> list[str]:
    lines: list[str] = []
    p = body
    while p:
        line, used = wrap_next(p, cols)
        if used == 0:
            break
        lines.append(line)
        p = p[used:]
    return lines


@lru_cache(maxsize=4096)
def _color(v: int) -> Color:
    return Color.from_565(v)


@lru_cache(maxsize=64)
def _icon_pixels(name: str, dim: bool, bg565: int) -> tuple[tuple[int, int, bytes], ...]:
    """(row, col, rgb888) of every opaque icon pixel, dimmed towards the background when disabled."""
    img = assets.icon(name)
    out = []
    for i, p in enumerate(img.px):
        if p != assets.TRANSPARENT:
            out.append((i // img.w, i % img.w, _color(blend565(p, bg565) if dim else p).rgb888))
    return tuple(out)


def blend565(a: int, b: int) -> int:
    """One third `a`, two thirds `b` (RGB565 channels), as widgets.cpp blend565."""
    r = (((a >> 11) & 0x1F) + 2 * ((b >> 11) & 0x1F)) // 3
    g = (((a >> 5) & 0x3F) + 2 * ((b >> 5) & 0x3F)) // 3
    bl = ((a & 0x1F) + 2 * (b & 0x1F)) // 3
    return (r << 11) | (g << 5) | bl


class Ui:
    """The widget set bound to a screen canvas. `log` records the text of the last frame for tests."""

    def __init__(self, th: Theme | None = None) -> None:
        self.t = th or theme()
        self.screen = Canvas(self.t.ScreenW, self.t.ScreenH)
        self.band = Canvas(self.t.ScreenW, self.t.BandH)
        self.log = DrawLog()
        self.status_cx = 0  # x of the connection dot of the last status bar
        self.small_cols = self.t.InnerW // self.t.MetaW  # 38

    # ---- band primitives ------------------------------------------------------------------------------
    def _text(self, s: str, x: int, y: int, fg: Color, small: bool = False) -> None:
        font: Font = FONT_META if small else FONT_BODY
        self.band.draw_string(s, x, y, fg, font)

    def _push(self, y: int) -> None:
        self.band.push(self.screen, 0, y)

    def _panel(self, c: Color) -> None:
        self.band.fill_rect(self.t.PadX, 0, self.t.InnerW, self.t.BandH, c)

    def _scrollbar(self, sc: Scroll, region_y: int, region_h: int, band_y: int) -> None:
        t = self.t
        if sc.visible == 0 or sc.total <= sc.visible:
            return
        y = region_y - band_y
        self.band.fill_rect(t.ScrollX, y, t.ScrollW, region_h, t.SurfacePanel)
        th = max(region_h * sc.visible // sc.total, 6)
        span = sc.total - sc.visible
        first = min(sc.first, span)
        ty = (region_h - th) * first // span
        self.band.fill_rect(t.ScrollX, y + ty, t.ScrollW, th, t.Accent)

    def _pad_rows(self, y: int, h: int) -> None:
        t = self.t
        if h <= 0:
            return
        self.screen.fill_rect(0, y, t.PadX, h, t.SurfaceBase)
        self.screen.fill_rect(t.PadX, y, t.InnerW, h, t.SurfacePanel)
        self.screen.fill_rect(t.PadX + t.InnerW, y, t.ScreenW - t.PadX - t.InnerW, h, t.SurfaceBase)

    def _content_pads(self, row_layout: bool) -> None:
        t = self.t
        gap_y = t.HeadingY + t.HeadingH
        self.screen.fill_rect(0, gap_y, t.ScreenW, t.ContentY - gap_y, t.SurfaceBase)
        if row_layout:
            self._pad_rows(t.ContentY, t.RowsY - t.ContentY)
            below = t.RowsY + t.Rows * t.RowH
            self._pad_rows(below, t.ContentY + t.ContentH - below)
        end = t.ContentY + t.ContentH
        self.screen.fill_rect(0, end, t.ScreenW, t.FooterY - end, t.SurfaceBase)

    # ---- heading --------------------------------------------------------------------------------------
    def _heading_tone(self, title: str, right: str, tone: str) -> None:
        t = self.t
        bg = t.Error if tone == "alert" else (t.Warning if tone == "warn" else t.SurfaceBase)
        fg = t.TextPrimary if tone == "normal" else t.SurfaceBase
        self.band.fill(bg)
        right_x = t.PadX + t.InnerW
        if right:
            rbuf = fit(right, 12)
            right_x -= len(rbuf) * t.BodyW
            self._text(rbuf, right_x, 0, t.TextMuted if tone == "normal" else fg)
        cols = (right_x - t.PadX - 1 - t.BodyW) // t.BodyW  # one column gap + 1 px for the bold pass
        buf = fit(title, cols if cols > 0 else 1)
        self._text(buf, t.PadX, 0, fg)
        self._text(buf, t.PadX + 1, 0, fg)  # bold = drawn twice, 1 px apart
        self._push(t.HeadingY)
        self.log.heading = (buf + " " + right).strip()

    def heading(self, title: str, right: str = "") -> None:
        if title.startswith("!"):
            self._heading_tone(title[1:], right, "alert")
        else:
            self._heading_tone(title, right, "normal")

    # ---- rows -------------------------------------------------------------------------------------------
    def _row_begin(self, selected: bool, warn_stripe: bool = False) -> None:
        t = self.t
        self.band.fill(t.SurfaceBase)
        self._panel(t.SurfaceFocus if selected else t.SurfacePanel)
        if selected:
            self.band.fill_rect(t.PadX, 0, t.StripeW, t.RowH, t.Accent)
            self._text(">", t.PadX + t.StripeW, 0, t.Accent)
        elif warn_stripe:
            self.band.fill_rect(t.PadX, 0, t.StripeW, t.RowH, t.Warning)

    def _row_push(self, row: int, sc: Scroll | None) -> None:
        t = self.t
        y = t.RowsY + row * t.RowH
        if sc:
            self._scrollbar(sc, t.RowsY, t.Rows * t.RowH, y)
        self._push(y)

    def _list_row(self, row: int, s: str, selected: bool, fg: Color, sc: Scroll | None) -> None:
        self._row_begin(selected)
        self._text(fit(s, self.t.ListCols), self.t.ListTextX, 0, fg)
        self._row_push(row, sc)
        self.log.rows.append(fit(s, self.t.ListCols))

    def _text_row(self, row: int, s: str, fg: Color, sc: Scroll | None, warn_stripe: bool = False) -> None:
        self._row_begin(False, warn_stripe)
        self._text(fit(s, self.t.TextCols), self.t.TextX, 0, fg)
        self._row_push(row, sc)
        self.log.rows.append(fit(s, self.t.TextCols))

    def _begin_frame_log(self) -> None:
        self.log.rows = []

    # ---- images -----------------------------------------------------------------------------------------
    def _blit(self, name: str, x: int, y: int, dim: bool, bg: Color) -> None:
        """Draws the icon rows that fall into the band; (x, y) is the image origin in band coordinates."""
        b, bh = self.band, self.t.BandH
        buf, stride = b.buf, b.w * 3
        for r, c, rgb in _icon_pixels(name, dim, bg.rgb565):
            yy = y + r
            if 0 <= yy < bh:
                o = yy * stride + (x + c) * 3
                if 0 <= x + c < b.w:
                    buf[o : o + 3] = rgb

    # ---- tiles --------------------------------------------------------------------------------------------
    def _tile_cell(self, tile: Tile, x: int, y: int, w: int, selected: bool) -> None:
        t = self.t
        bg = t.SurfaceFocus if selected else t.SurfacePanel
        if selected:
            self.band.fill_rect(x, y, w, t.TileH, t.SurfaceFocus)
            self.band.fill_rect(x, y, t.StripeW, t.TileH, t.Accent)
        ix = x + t.StripeW + 2
        if tile.icon is not None:
            self._blit(tile.icon, ix, y + 2, not tile.enabled, bg)
        else:  # no bitmap (Help): outlined '?' glyph of the same size
            self.band.draw_round_rect(ix + 1, y + 3, assets.ICON_SIZE - 2, assets.ICON_SIZE - 2, 4, t.Accent)
            self._text(
                "?",
                ix + (assets.ICON_SIZE - t.BodyW) // 2,
                y + 2 + (assets.ICON_SIZE - t.BodyH) // 2,
                t.Accent,
            )
        tx = ix + assets.ICON_SIZE + 3
        cols = (x + w - 1 - tx) // t.BodyW
        if cols >= 2:
            lab = fit(tile.label, min(cols - 1, t.ListCols))
            if selected:
                self._text(">", tx, y + 2, t.Accent)
            self._text(lab, tx + t.BodyW, y + 2, t.TextPrimary if tile.enabled else t.TextMuted)
        if tile.detail:
            dcols = (x + w - 1 - tx) // t.MetaW
            self._text(fit(tile.detail, min(dcols, self.small_cols)), tx, y + 19, t.TextMuted, small=True)

    # ---- footer helper ----------------------------------------------------------------------------------
    def _hint_text(self, s: str, x: int, y: int, small: bool, fg: Color, key_fg: Color) -> None:
        cw = self.t.MetaW if small else self.t.BodyW
        in_key = False
        for i, ch in enumerate(s):
            if ch in " :":
                in_key = False
            elif i == 0 or s[i - 1] == " ":
                j = i
                while j < len(s) and s[j] not in " :":
                    j += 1
                in_key = j < len(s) and s[j] == ":" and j - i <= 5
            self._text(ch, x + i * cw, y, key_fg if in_key else fg, small)

    # ===== public API (ui.h) ===============================================================================

    def draw_status(
        self, mode: ModeBanner, session_short: str, backend: str, battery_pct: int, conn: Conn
    ) -> None:
        t = self.t
        label, fg, bg = "OFFLINE", t.TextMuted, t.SurfacePanel
        if mode is ModeBanner.LIVE_RADIO:
            label, fg, bg = "LIVE RADIO", t.SurfaceBase, t.Accent
        elif mode is ModeBanner.AI_CONNECTED:
            label, fg, bg = "AI CONNECTED", t.Accent, t.SurfaceFocus
        elif mode is ModeBanner.BACKEND_OK:
            label, fg, bg = "BACKEND OK", t.TextPrimary, t.SurfaceFocus
        elif mode is ModeBanner.START_FAILED:
            label, fg, bg = "START FAILED", t.SurfaceBase, t.Error
        elif mode is ModeBanner.SIMULATED:
            label, fg, bg = "SIMULATED", t.SurfaceBase, t.Warning
        b = self.band
        b.fill(t.SurfaceBase)
        b.hline(0, t.StatusH, t.ScreenW, t.SurfacePanel)  # divider at y 15
        banner_w = len(label) * t.MetaW + 6
        b.fill_round_rect(t.PadX, 2, banner_w, 11, 2, bg)
        self._text(label, t.PadX + 3, 4, fg, small=True)
        batt = f"{battery_pct}%" if 0 <= battery_pct <= 100 else "--%"
        batt_x = t.PadX + t.InnerW - len(batt) * t.MetaW
        self._text(batt, batt_x, 4, t.TextPrimary, small=True)
        be = fit(backend, 10)
        be_x = batt_x - 6 - len(be) * t.MetaW
        self._text(be, be_x, 4, t.TextPrimary, small=True)
        left_end = be_x - 4
        if conn is not Conn.UNKNOWN:
            cx = be_x - 5
            if conn is Conn.ONLINE:
                b.fill_circle(cx, 7, 2, t.Accent)
            elif conn is Conn.CONNECTING:
                b.fill_circle(cx, 7, 2, t.Warning)
            elif conn is Conn.ERROR:
                b.fill_circle(cx, 7, 2, t.Error)
            else:
                b.draw_circle(cx, 7, 2, t.TextMuted)
            left_end = cx - 6
        sess_x = t.PadX + banner_w + 4
        sess_cols = (left_end - sess_x) // t.MetaW
        if sess_cols > 1:
            self._text(
                fit(session_short or "no-session", min(sess_cols, self.small_cols)),
                sess_x,
                4,
                t.TextMuted,
                True,
            )
        self._push(t.StatusY)
        self.status_cx = be_x - 5
        self.log.status = f"{label} {session_short or 'no-session'} {be} {batt}"

    def draw_list(self, m: ListModel) -> None:
        t = self.t
        count = len(m.rows)
        pos = f"{m.selected + 1}/{count}" if count > 0 else "0/0"
        self._begin_frame_log()
        self.heading(m.title, m.right or pos)
        self._content_pads(True)
        sc = Scroll(count, t.Rows, m.top)
        for r in range(t.Rows):
            i = m.top + r
            if i < count:
                dis = i < len(m.disabled) and m.disabled[i]
                self._list_row(r, m.rows[i], i == m.selected, t.TextMuted if dis else t.TextPrimary, sc)
            elif count == 0 and r == 0:
                self._list_row(r, m.empty or "(empty)", False, t.TextMuted, sc)
            else:
                self._list_row(r, "", False, t.TextPrimary, sc)

    def draw_tiles(self, m: TileModel) -> None:
        t = self.t
        self._begin_frame_log()
        self.heading(m.title, m.right)
        self._content_pads(False)
        cols = 2 if m.columns >= 2 else 1
        cell_w = t.InnerW // cols
        count = len(m.tiles)
        sc = Scroll((count + cols - 1) // cols, t.TileRows, m.top_row)
        for vr in range(t.TileRows):
            y0 = t.ContentY + vr * t.TileH
            off = 0
            while True:  # two bands per tile row, y0..y0+15 and y0+12..y0+27; shared rows are identical
                self.band.fill(t.SurfaceBase)
                self._panel(t.SurfacePanel)
                for c in range(cols):
                    idx = (m.top_row + vr) * cols + c
                    if idx < count:
                        self._tile_cell(m.tiles[idx], t.PadX + c * cell_w, -off, cell_w, idx == m.selected)
                self._scrollbar(sc, t.ContentY, t.ContentH, y0 + off)
                self._push(y0 + off)
                if off != 0:
                    break
                off = t.TileH - t.BandH
        self.log.rows = [f"{x.label} | {x.detail}" for x in m.tiles]

    def draw_footer(self, left: str, right: str = "") -> None:
        t = self.t
        alert = left.startswith("!")
        if alert:
            left = left[1:]
        fg = t.SurfaceBase if alert else t.TextPrimary
        key_fg = t.SurfaceBase if alert else t.Accent
        right_fg = t.SurfaceBase if alert else t.TextMuted
        self.band.fill(t.Error if alert else t.SurfaceBase)
        rbuf = fit(right, 20)
        rn, ln = len(rbuf), len(left)
        if ln + (rn + 1 if rn > 0 else 0) <= t.FooterCols:
            if rn > 0:
                self._hint_text(rbuf, t.PadX + t.InnerW - rn * t.BodyW, 0, False, right_fg, key_fg)
            self._hint_text(fit(left, t.FooterCols), t.PadX, 0, False, fg, key_fg)
        else:  # too long for the 8x16 font: 6x8 font, one line, or two when the left text needs it
            right_x = t.PadX + t.InnerW - rn * t.MetaW
            cols1 = (right_x - t.PadX) // t.MetaW - (1 if rn > 0 else 0)
            if ln <= cols1:
                if rn > 0:
                    self._hint_text(rbuf, right_x, 4, True, right_fg, key_fg)
                self._hint_text(fit(left, max(cols1, 1)), t.PadX, 4, True, fg, key_fg)
            else:
                if rn > 0:
                    self._hint_text(rbuf, right_x, 0, True, right_fg, key_fg)
                line, used = wrap_next(left, max(cols1, 1))
                self._hint_text(line, t.PadX, 0, True, fg, key_fg)
                self._hint_text(fit(left[used:], self.small_cols), t.PadX, 8, True, fg, key_fg)
        self._push(t.FooterY)
        self.log.footer = (left + " " + rbuf).strip()

    def draw_notice(self, msg: str) -> None:
        t = self.t
        self.band.fill(t.SurfaceBase)
        self._panel(t.SurfaceFocus)
        self.band.fill_rect(t.PadX, 0, t.StripeW, t.BandH, t.Accent)
        x = t.PadX + t.StripeW + 3
        w = t.PadX + t.InnerW - 2 - x
        if len(msg) <= w // t.BodyW:
            self._text(fit(msg, w // t.BodyW), x, 0, t.TextPrimary)
        else:
            cols = w // t.MetaW
            line, used = wrap_next(msg, cols)
            self._text(line, x, 0, t.TextPrimary, True)
            self._text(fit(msg[used:], cols), x, 8, t.TextPrimary, True)
        self._push(t.FooterY)
        self.log.footer = msg

    def draw_progress(self, label: str, elapsed_s: int, items: int, cancellable: bool) -> None:
        t = self.t
        self._begin_frame_log()
        self._heading_tone("IN PROGRESS", f"{elapsed_s}s", "normal")
        self._content_pads(True)
        self._text_row(0, label, t.TextPrimary, None)
        self._text_row(1, f"Elapsed: {elapsed_s} s", t.TextMuted, None)
        self._text_row(2, f"Items so far: {items}", t.TextMuted, None)
        # Activity bar: moves one step per redraw (the caller redraws once a second), no timer of its own.
        self._row_begin(False)
        track_w = t.TextCols * t.BodyW
        seg, steps = 40, 6
        self.band.fill_rect(t.TextX, 6, track_w, 4, t.SurfaceFocus)
        self.band.fill_rect(
            t.TextX + (elapsed_s % steps) * (track_w - seg) // (steps - 1), 6, seg, 4, t.Accent
        )
        self._row_push(3, None)
        if cancellable:
            self._text_row(4, "Cancel: Fn+` or Ctrl+C", t.Warning, None)
        else:
            self._text_row(4, "(cannot be cancelled)", t.TextMuted, None)

    def draw_confirm(self, operation: str, target: str, effect: str) -> None:
        t = self.t
        self._begin_frame_log()
        self._heading_tone("CONFIRM ACTION", "", "warn")
        self._content_pads(True)
        self._text_row(0, f"Op: {operation or '?'}", t.TextPrimary, None, True)
        self._text_row(1, f"Target: {target or '(none)'}", t.TextPrimary, None, True)
        p = effect
        for r in range(2, t.Rows):
            out, used = wrap_next(p, t.TextCols)
            p = p[used:]
            if r == t.Rows - 1 and p:  # effect longer than three rows
                n = len(out)
                out = out[: t.TextCols - 1] + "~" if n >= t.TextCols else out + "~"
            self._text_row(r, out, t.TextPrimary, None, True)

    def draw_message(self, title: str, body: str, first_line: int = 0, total_lines: int = 0) -> None:
        t = self.t
        self._begin_frame_log()
        self.heading(title)
        self._content_pads(True)
        sc = Scroll(total_lines, t.Rows, first_line)
        p = body
        for r in range(t.Rows):
            out, used = wrap_next(p, t.TextCols)
            p = p[used:]
            self._text_row(r, out, t.TextPrimary, sc)

    def draw_text_entry(self, prompt: str, buffer: str, cursor: int, hint: str = "") -> None:
        t = self.t
        self._begin_frame_log()
        self.heading(prompt)
        self._content_pads(True)
        n_len = len(buffer)
        cursor = min(cursor, n_len)
        entry_rows = t.Rows - 1  # rows 0..3, row 4: character count
        cursor_line = cursor // t.TextCols
        first = cursor_line - (entry_rows - 1) if cursor_line >= entry_rows else 0
        for r in range(entry_rows):
            start = (first + r) * t.TextCols
            n = min(n_len - start, t.TextCols) if start < n_len else 0
            out = fit(buffer[start : start + n] if start < n_len else "", t.TextCols)
            self._row_begin(False)
            self._text(out, t.TextX, 0, t.TextPrimary)
            if first + r == cursor_line:
                cx = t.TextX + (cursor % t.TextCols) * t.BodyW
                under = " "
                if cursor < n_len:
                    under = buffer[cursor] if " " <= buffer[cursor] <= "~" else "?"
                self.band.fill_rect(cx, 0, t.BodyW, t.RowH, t.Accent)
                self._text(under, cx, 0, t.SurfaceBase)
            self._row_push(r, None)
            self.log.rows.append(out)
        self._text_row(t.Rows - 1, hint or f"{n_len} chars", t.TextMuted, None)
