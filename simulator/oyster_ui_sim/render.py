"""Headless rendering: every route in every state as a PNG, plus a contact sheet per profile."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from .engine import Simulator
from .routes import ROUTES, STATES

PROFILES = {"firmware": False, "preview": True}  # name -> preview flag


def png_bytes(img: Image.Image) -> bytes:
    """Deterministic PNG bytes (same pixels -> same bytes)."""
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()


def render_route(route_id: str, state: str, *, preview: bool = False, scale: int = 1) -> Image.Image:
    """One frame of a route in a state on a fresh simulator (mock data, 1 s after entering it)."""
    sim = Simulator(preview=preview)
    sim.goto(route_id, state)
    sim.advance(1000)  # past the 700 ms dialog guard; loading views show "1 s"
    return sim.frame(scale)


@dataclass
class HeadlessReport:
    frames: int
    routes: int
    states: int
    out: Path
    sheets: list[Path]


def render_all(out_dir: Path, scale: int = 4, only: Sequence[str] | None = None) -> HeadlessReport:
    """PNG of every route x state for both profiles: out/<profile>/<route>_<state>.png + contact sheets.
    `only` limits the routes (tests)."""
    routes = [r for r in ROUTES if only is None or r.id in only]
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = 0
    sheets: list[Path] = []
    for profile, preview in PROFILES.items():
        folder = out_dir / profile
        folder.mkdir(parents=True, exist_ok=True)
        cell_w, cell_h, pad, label_h = 240, 135, 6, 12
        sheet = Image.new(
            "RGB",
            (pad + len(STATES) * (cell_w + pad), pad + len(routes) * (cell_h + pad + label_h)),
            (16, 25, 35),
        )
        draw = ImageDraw.Draw(sheet)
        for r, rt in enumerate(routes):
            for c, state in enumerate(STATES):
                img = render_route(rt.id, state, preview=preview, scale=1)
                img.resize((cell_w * scale, cell_h * scale), Image.NEAREST).save(
                    folder / f"{rt.id}_{state}.png", compress_level=1
                )
                x = pad + c * (cell_w + pad)
                y = pad + r * (cell_h + pad + label_h)
                sheet.paste(img, (x, y + label_h))
                draw.text((x, y), f"{profile} {rt.id} {state}", fill=(169, 196, 206))
                frames += 1
        path = out_dir / f"contact_sheet_{profile}.png"
        sheet.save(path)
        sheets.append(path)
    return HeadlessReport(frames, len(routes), len(STATES), out_dir, sheets)
