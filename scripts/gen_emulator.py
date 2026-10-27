"""Build docs/emulator/index.html: the OysterOS browser emulator as one self-contained page.

    python scripts/gen_emulator.py [--out PATH]

The page is docs/emulator/src/shell.html (the Cardputer ADV drawn in CSS, its keyboard and the legend)
plus the scripts in docs/emulator/src: lcd.js (a port of firmware/src/ui/widgets.cpp), calc.js (a port of
oyster_core calc_eval), os.js (the screens and strings of app_controller.cpp and the modules) and app.js
(keys, tour, status LED). The firmware's own pixel data is embedded: the two M5GFX fonts from
simulator/oyster_ui_sim/glyphs.py and the icons, splash and loader animation from scripts/gen_assets.py.
Rerun after changing either.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "emulator" / "src"
DEFAULT_OUT = ROOT / "docs" / "emulator" / "index.html"
SCRIPTS = ("lcd.js", "calc.js", "os.js", "app.js")

sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "simulator"))

import gen_assets  # noqa: E402
from oyster_ui_sim import glyphs  # noqa: E402


def pack565(values: list[int]) -> str:
    """RGB565 pixels as little-endian uint16, base64."""
    return base64.b64encode(struct.pack(f"<{len(values)}H", *values)).decode()


def assets_json() -> str:
    icons, _logo, splash, loader = gen_assets.build()
    data = {
        "f16": base64.b64encode(b"".join(glyphs.FONT_8X16)).decode(),
        "f8": base64.b64encode(b"".join(glyphs.FONT_6X8)).decode(),
        "icons": {name: pack565(px) for name, px in icons.items()},
        "splash": pack565(splash),
        "loader": pack565(loader),
    }
    return json.dumps(data, separators=(",", ":"))


# A page opened from disk needs its own document skeleton; --fragment leaves it out for hosts that add one.
HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    "<style>body{margin:0}[hidden]{display:none!important}</style>\n</head>\n<body>\n"
)


def build(fragment: bool = False) -> str:
    shell = (SRC / "shell.html").read_text(encoding="utf-8")
    code = "\n".join((SRC / name).read_text(encoding="utf-8") for name in SCRIPTS)
    page = f"{shell}<script>\nconst ASSETS = {assets_json()};\n{code}\n</script>\n"
    return page if fragment else f"{HEAD}{page}</body>\n</html>\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--fragment", action="store_true", help="omit <!doctype>/<head>/<body> (for hosts that add them)"
    )
    args = ap.parse_args()
    page = build(args.fragment)
    args.out.write_text(page, encoding="utf-8", newline="\n")
    print(f"wrote {args.out}: {len(page.encode()):,} bytes")


if __name__ == "__main__":
    main()
