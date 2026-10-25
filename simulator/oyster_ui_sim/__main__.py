"""python -m oyster_ui_sim [--headless --out DIR | --script FILE --out DIR | --routes | --keys]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .keys import KEY_TABLE
from .routes import HOME, ROUTES


def _routes_table() -> str:
    lines = [
        "| Route | Title | Kind | Module | Availability | Back | Enter | Job |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in ROUTES:
        enter = ", ".join(f"{k}->{t}" for k, t in r.enter) or "-"
        lines.append(
            f"| {r.id} | {r.title} | {r.kind}{' (modal)' if r.modal else ''} | {r.module} | {r.availability} "
            f"| {'(root)' if r.id == HOME else (r.parent or 'previous')} | {enter} | {r.job or '-'} |"
        )
    return "\n".join(lines)


def _keys_table() -> str:
    lines = [
        "| Action | Proposed physical keys (UNVERIFIED) | Scope | Desktop shortcut |",
        "|---|---|---|---|",
    ]
    lines += [f"| {b.action} | {b.shows} | {b.scope} | {b.pc} |" for b in KEY_TABLE]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="oyster_ui_sim", description="OysterOS 240x135 UI simulator (no device, no backend)"
    )
    ap.add_argument(
        "--headless", action="store_true", help="render every route and state to PNG (needs --out)"
    )
    ap.add_argument(
        "--script", type=Path, help="replay a key script (see oyster_ui_sim.script); frames go to --out"
    )
    ap.add_argument("--out", type=Path, help="output directory for PNG files")
    ap.add_argument("--scale", type=int, default=4, help="integer pixel scale (default 4)")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="also enable the Original Apps tile (its screen stays unavailable)",
    )
    ap.add_argument("--routes", action="store_true", help="print the route table as Markdown")
    ap.add_argument("--keys", action="store_true", help="print the key table as Markdown")
    args = ap.parse_args(argv)
    if args.scale < 1 or args.scale > 16:
        ap.error("--scale must be 1..16")
    if args.routes:
        print(_routes_table())
        return 0
    if args.keys:
        print(_keys_table())
        return 0
    if args.headless:
        if args.out is None:
            ap.error("--headless needs --out DIR")
        from .render import render_all

        rep = render_all(args.out, args.scale)
        print(
            f"rendered {rep.frames} frames ({rep.routes} routes x {rep.states} states x 2 profiles)"
            f" -> {rep.out}"
        )
        for s in rep.sheets:
            print(f"contact sheet: {s}")
        return 0
    if args.script:
        from .engine import Simulator
        from .script import ScriptError, run_script

        try:
            text = args.script.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read {args.script}: {exc}", file=sys.stderr)
            return 2
        try:
            res = run_script(text, args.out, Simulator(preview=args.preview))
        except ScriptError as exc:
            print(f"script failed: {exc}", file=sys.stderr)
            return 1
        print(f"replayed {args.script.name}: {len(res.frames)} frames, {res.checks} checks passed")
        for f in res.frames:
            print(f"  {f}")
        return 0
    from .tk_app import run

    return run(scale=args.scale, preview=args.preview)


if __name__ == "__main__":
    sys.exit(main())
