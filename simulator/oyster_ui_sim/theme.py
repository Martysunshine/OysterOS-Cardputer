"""Design tokens read at runtime from firmware/include/ui_theme.h, the single source of the palette and
the 240x135 layout, so the simulator cannot drift from the firmware. Parsing fails loudly."""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
THEME_HEADER = REPO_ROOT / "firmware" / "include" / "ui_theme.h"


class ThemeParseError(RuntimeError):
    """ui_theme.h could not be parsed into the constants the simulator needs."""


@dataclass(frozen=True)
class Color:
    """An RGB565 constant of the firmware and its 8-bit expansion (bit replication, as most panels do)."""

    rgb565: int
    rgb888: bytes

    @staticmethod
    def from_565(v: int) -> Color:
        r5, g6, b5 = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
        return Color(v, bytes(((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2))))

    @property
    def hex(self) -> str:
        return "#" + self.rgb888.hex().upper()


def rgb565(rgb: int) -> int:
    """0xRRGGBB -> RGB565, the same arithmetic as oyster::theme::rgb565."""
    return ((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb >> 3) & 0x001F)


REQUIRED_COLORS = (
    "kSurfaceBase",
    "kSurfacePanel",
    "kSurfaceFocus",
    "kAccent",
    "kTextPrimary",
    "kTextMuted",
    "kWarning",
    "kError",
)
REQUIRED_INTS = (
    "kScreenW",
    "kScreenH",
    "kPadX",
    "kInnerW",
    "kStatusY",
    "kStatusH",
    "kHeadingY",
    "kHeadingH",
    "kContentY",
    "kContentH",
    "kFooterY",
    "kFooterH",
    "kBandH",
    "kStripeW",
    "kRowH",
    "kRows",
    "kRowsY",
    "kTileH",
    "kTileRows",
    "kScrollX",
    "kScrollW",
    "kBodyW",
    "kBodyH",
    "kMetaW",
    "kMetaH",
    "kListTextX",
    "kListCols",
    "kTextX",
    "kTextCols",
    "kFooterCols",
)

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Div: operator.floordiv,
}
_DECL = re.compile(r"constexpr\s+(?:uint16_t|int|uint32_t)\s+(k\w+\s*=[^;]*);")
_RGB = re.compile(r"^rgb565\(\s*(0[xX][0-9A-Fa-f]{6})\s*\)$")


def _eval(node: ast.AST, env: dict[str, int]) -> int:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name) and node.id in env:
        return env[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, env), _eval(node.right, env))
    raise ThemeParseError(f"unsupported expression in ui_theme.h: {ast.dump(node)[:80]}")


def _split_declarators(body: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [*parts, cur]


def parse_header(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Returns (integers, colours as RGB565) from the text of ui_theme.h."""
    text = re.sub(r"//[^\n]*", "", text)
    ints: dict[str, int] = {}
    colors: dict[str, int] = {}
    for m in _DECL.finditer(text):
        for decl in _split_declarators(m.group(1)):
            name, _, expr = (s.strip() for s in decl.partition("="))
            if not name or not expr:
                raise ThemeParseError(f"cannot parse declaration {decl!r}")
            rgb = _RGB.match(expr)
            if rgb:
                colors[name] = rgb565(int(rgb.group(1), 16))
                continue
            try:
                ints[name] = _eval(ast.parse(expr, mode="eval"), ints)
            except SyntaxError as exc:
                raise ThemeParseError(f"cannot parse {name} = {expr!r}") from exc
    return ints, colors


class Theme:
    """Attribute access to the constants without the leading 'k': theme.PadX, theme.Accent (a Color)."""

    def __init__(self, ints: dict[str, int], colors: dict[str, int]) -> None:
        missing = [n for n in REQUIRED_INTS if n not in ints] + [
            n for n in REQUIRED_COLORS if n not in colors
        ]
        if missing:
            raise ThemeParseError("ui_theme.h lacks the constants: " + ", ".join(missing))
        if (ints["kScreenW"], ints["kScreenH"]) != (240, 135):
            raise ThemeParseError("the simulator renders a 240x135 screen; ui_theme.h says otherwise")
        self.ints = dict(ints)
        self.colors = {n: Color.from_565(v) for n, v in colors.items()}
        for name, value in ints.items():
            setattr(self, name[1:], value)
        for name, color in self.colors.items():
            setattr(self, name[1:], color)

    @property
    def palette(self) -> dict[str, Color]:
        return {n[1:]: c for n, c in self.colors.items()}


def load_theme(path: Path = THEME_HEADER) -> Theme:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThemeParseError(f"cannot read {path}: {exc}") from exc
    ints, colors = parse_header(text)
    return Theme(ints, colors)


_cached: dict[Path, Any] = {}


def theme(path: Path = THEME_HEADER) -> Theme:
    """The parsed theme (cached per path)."""
    if path not in _cached:
        _cached[path] = load_theme(path)
    return _cached[path]
