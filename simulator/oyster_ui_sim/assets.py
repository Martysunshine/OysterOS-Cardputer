"""The 24x24 module icons, read from the firmware's generated asset file (src/assets_gen.cpp), so the
simulator shows the very pixels the firmware blits. Parsing fails loudly."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .theme import REPO_ROOT

ASSETS_CPP = REPO_ROOT / "firmware" / "src" / "assets_gen.cpp"
ASSETS_H = REPO_ROOT / "firmware" / "include" / "assets.h"
TRANSPARENT = 0xF81F
ICON_SIZE = 24


class AssetParseError(RuntimeError):
    """The firmware asset files do not have the expected shape."""


@dataclass(frozen=True)
class Image:
    w: int
    h: int
    px: tuple[int, ...]


def icon_names(header: Path = ASSETS_H) -> tuple[str, ...]:
    """IconId enumerators in declaration order (everything before `Count`)."""
    text = re.sub(r"//[^\n]*", "", header.read_text(encoding="utf-8"))
    m = re.search(r"enum class IconId[^{]*\{([^}]*)\}", text)
    if not m:
        raise AssetParseError("IconId enum not found in assets.h")
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    if not names or names[-1] != "Count":
        raise AssetParseError("IconId enum must end with Count")
    return tuple(names[:-1])


@lru_cache(maxsize=1)
def _icons() -> dict[str, Image]:
    text = ASSETS_CPP.read_text(encoding="utf-8")
    out: dict[str, Image] = {}
    for name in icon_names():
        m = re.search(rf"kIcon{name}\[(\d+)\]\s*=\s*\{{([^}}]*)\}};", text)
        if not m:
            raise AssetParseError(f"icon array kIcon{name} not found in assets_gen.cpp")
        px = tuple(int(h, 16) for h in re.findall(r"0x([0-9A-Fa-f]{4})", m.group(2)))
        if len(px) != int(m.group(1)) or len(px) != ICON_SIZE * ICON_SIZE:
            raise AssetParseError(f"icon {name} has {len(px)} pixels, expected {ICON_SIZE * ICON_SIZE}")
        out[name] = Image(ICON_SIZE, ICON_SIZE, px)
    return out


def icon(name: str) -> Image:
    return _icons()[name]
