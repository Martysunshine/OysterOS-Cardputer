"""A tiny RGB canvas with the drawing primitives the firmware uses (M5GFX/LovyanGFX names in snake_case).

Everything is integer, aliasing-free and clipped. Circle and rounded-rectangle routines are ports of the
algorithms in M5GFX LGFXBase.cpp (the pinned library), so their pixel patterns follow the library; they have
not been compared with a photo of the display. Text uses the glyph tables extracted from M5GFX."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image

from . import glyphs
from .theme import Color


class Font:
    """Fixed-cell bitmap font: `width` x `height` cell, glyph runs cached per character."""

    def __init__(self, name: str, width: int, height: int, table: tuple[bytes, ...], column_major: bool):
        self.name, self.width, self.height = name, width, height
        self._table, self._cols = table, column_major

    @lru_cache(maxsize=256)  # noqa: B019 - fonts are module-level singletons
    def runs(self, ch: str) -> tuple[tuple[int, int, int], ...]:
        """(dx, dy, length) horizontal runs of set pixels for a printable ASCII character."""
        code = ord(ch)
        if not glyphs.FIRST <= code <= glyphs.LAST:
            code = ord("?")
        g = self._table[code - glyphs.FIRST]
        out: list[tuple[int, int, int]] = []
        for row in range(self.height):
            start = -1
            for col in range(self.width):
                if self._cols:
                    on = col < len(g) and bool(g[col] >> row & 1)
                else:
                    on = bool(g[row] & (0x80 >> col))
                if on and start < 0:
                    start = col
                if (not on) and start >= 0:
                    out.append((start, row, col - start))
                    start = -1
            if start >= 0:
                out.append((start, row, self.width - start))
        return tuple(out)


FONT_BODY = Font("AsciiFont8x16", 8, 16, glyphs.FONT_8X16, column_major=False)
FONT_META = Font("Font0", 6, 8, glyphs.FONT_6X8, column_major=True)


class Canvas:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 3)

    # ---- basic fills ------------------------------------------------------------------------------
    def fill(self, c: Color) -> None:
        self.buf[:] = c.rgb888 * (self.w * self.h)

    def fill_rect(self, x: int, y: int, w: int, h: int, c: Color) -> None:
        x0, x1 = max(x, 0), min(x + w, self.w)
        y0, y1 = max(y, 0), min(y + h, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        run = c.rgb888 * (x1 - x0)
        stride = self.w * 3
        for yy in range(y0, y1):
            o = yy * stride + x0 * 3
            self.buf[o : o + len(run)] = run

    def hline(self, x: int, y: int, w: int, c: Color) -> None:
        self.fill_rect(x, y, w, 1, c)

    def vline(self, x: int, y: int, h: int, c: Color) -> None:
        self.fill_rect(x, y, 1, h, c)

    def pixel(self, x: int, y: int, c: Color) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            o = (y * self.w + x) * 3
            self.buf[o : o + 3] = c.rgb888

    def get(self, x: int, y: int) -> bytes:
        o = (y * self.w + x) * 3
        return bytes(self.buf[o : o + 3])

    # ---- shapes (ports of LGFXBase) -------------------------------------------------------------------
    def draw_circle(self, x: int, y: int, r: int, c: Color) -> None:
        if r <= 0:
            self.pixel(x, y, c)
            return
        f, ddf_y, ddf_x, i, j = 1 - r, -(r << 1), 1, 0, -1
        while True:
            while f < 0:
                i += 1
                ddf_x += 2
                f += ddf_x
            ddf_y += 2
            f += ddf_y
            n = i - j
            self.hline(x - i, y + r, n, c)
            self.hline(x - i, y - r, n, c)
            self.hline(x + j + 1, y - r, n, c)
            self.hline(x + j + 1, y + r, n, c)
            self.vline(x + r, y + j + 1, n, c)
            self.vline(x + r, y - i, n, c)
            self.vline(x - r, y - i, n, c)
            self.vline(x - r, y + j + 1, n, c)
            j = i
            r -= 1
            if not i < r:
                break

    def fill_circle(self, x: int, y: int, r: int, c: Color) -> None:
        self.hline(x - r, y, (r << 1) + 1, c)
        if r <= 0:
            return
        delta = 1
        f, ddf_y, ddf_x, i = 1 - r, -(r << 1), 1, 0
        while True:
            ln = 0
            while f < 0:
                ddf_x += 2
                f += ddf_x
                ln += 1
            i += ln
            ddf_y += 2
            f += ddf_y
            if ln:
                self.fill_rect(x - r, y + i - ln + 1, (r << 1) + delta, ln, c)
            self.hline(x - i, y + r, (i << 1) + delta, c)
            self.hline(x - i, y - r, (i << 1) + delta, c)
            if ln:
                self.fill_rect(x - r, y - i, (r << 1) + delta, ln, c)
            r -= 1
            if not i < r:
                break

    def draw_round_rect(self, x: int, y: int, w: int, h: int, r: int, c: Color) -> None:
        w -= 1
        h -= 1
        length = (r << 1) + 1
        y1, y0 = y + h - r, y + r
        self.vline(x, y0 + 1, h - length, c)
        self.vline(x + w, y0 + 1, h - length, c)
        x1, x0 = x + w - r, x + r
        self.hline(x0 + 1, y, w - length, c)
        self.hline(x0 + 1, y + h, w - length, c)
        f, ddf_y, ddf_x, length = 1 - r, -(r << 1), 1, 0
        i = 0
        while i <= r:  # r shrinks inside the loop, as in the library
            length += 1
            if f >= 0:
                self.hline(x0 - i, y0 - r, length, c)
                self.hline(x0 - i, y1 + r, length, c)
                self.hline(x1 + i - length + 1, y1 + r, length, c)
                self.hline(x1 + i - length + 1, y0 - r, length, c)
                self.vline(x1 + r, y1 + i - length + 1, length, c)
                self.vline(x0 - r, y1 + i - length + 1, length, c)
                self.vline(x1 + r, y0 - i, length, c)
                self.vline(x0 - r, y0 - i, length, c)
                length = 0
                r -= 1
                ddf_y += 2
                f += ddf_y
            ddf_x += 2
            f += ddf_x
            i += 1

    def fill_round_rect(self, x: int, y: int, w: int, h: int, r: int, c: Color) -> None:
        y2, y1 = y + r, y + h - r - 1
        ddf_y = -(r << 1)
        delta = w + ddf_y
        self.fill_rect(x, y2, w, h + ddf_y, c)
        x0 = x + r
        f, ddf_x, length = 1 - r, 1, 0
        i = 0
        while i <= r:
            length += 1
            if f >= 0:
                self.fill_rect(x0 - r, y2 - i, (r << 1) + delta, length, c)
                self.fill_rect(x0 - r, y1 + i - length + 1, (r << 1) + delta, length, c)
                if i == r:
                    break
                length = 0
                self.hline(x0 - i, y1 + r, (i << 1) + delta, c)
                ddf_y += 2
                f += ddf_y
                self.hline(x0 - i, y2 - r, (i << 1) + delta, c)
                r -= 1
            ddf_x += 2
            f += ddf_x
            i += 1

    # ---- text ---------------------------------------------------------------------------------------------
    def draw_string(self, s: str, x: int, y: int, c: Color, font: Font = FONT_BODY) -> None:
        """Top-left datum, transparent background, no wrapping (as widgets.cpp text())."""
        for ch in s:
            if ch != " ":
                for dx, dy, n in font.runs(ch):
                    self.fill_rect(x + dx, y + dy, n, 1, c)
            x += font.width

    # ---- output -------------------------------------------------------------------------------------------
    def push(self, dst: Canvas, x: int, y: int) -> None:
        """pushSprite: copy this canvas into `dst` at (x, y), clipped to dst."""
        x0, x1 = max(x, 0), min(x + self.w, dst.w)
        for row in range(self.h):
            yy = y + row
            if not 0 <= yy < dst.h or x1 <= x0:
                continue
            src = (row * self.w + (x0 - x)) * 3
            o = (yy * dst.w + x0) * 3
            dst.buf[o : o + (x1 - x0) * 3] = self.buf[src : src + (x1 - x0) * 3]

    def image(self, scale: int = 1) -> Image.Image:
        img = Image.frombytes("RGB", (self.w, self.h), bytes(self.buf))
        return img if scale == 1 else img.resize((self.w * scale, self.h * scale), Image.NEAREST)
