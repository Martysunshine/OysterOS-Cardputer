"""Helpers shared by the simulator tests."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
FIRMWARE = REPO / "firmware"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def screen_text(sim) -> str:
    """Render and return every text the frame shows (heading, rows, footer)."""
    sim.render()
    log = sim.ui.log
    return "\n".join([log.status, log.heading, *log.rows, log.footer])


def pixel(sim, x: int, y: int) -> bytes:
    return sim.ui.screen.get(x, y)


def back_chord(sim) -> str:
    """The physical Back key of the current screen: ` in navigation mode, Del (empty buffer) in text entry."""
    return "del" if sim.view().kind == "entry" else "`"


def expand_ids(cell: str) -> list[str]:
    """'L10-L13' -> L10..L13, 'D10, D11, D20-D22' -> D10 D11 D20 D21 D22, '(dialog)' -> []."""
    out: list[str] = []
    for part in re.split(r",\s*", cell.strip()):
        m = re.fullmatch(r"([A-Z]+)(\d+)(?:-([A-Z]*)(\d+))?", part.strip())
        if not m:
            continue
        prefix, a, _, b = m.groups()
        out += [f"{prefix}{n:0{len(a)}d}" for n in range(int(a), int(b or a) + 1)]
    return out


def screen_map_rows() -> list[tuple[list[str], str]]:
    """(design IDs, status text) of every row of the screen table of docs/UI_SCREEN_MAP.md."""
    rows = []
    for line in read(DOCS / "UI_SCREEN_MAP.md").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 5 and re.match(r"^[A-Z]+\d+", cells[0]):
            rows.append((expand_ids(cells[0]), cells[-1]))
    return rows


def colour_set(img) -> set[tuple[int, int, int]]:
    """The distinct RGB colours of a PIL image (works across Pillow versions)."""
    flat = getattr(img, "get_flattened_data", None)
    return set(flat() if flat else img.getdata())
