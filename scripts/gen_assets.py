"""Generate firmware/src/assets_gen.cpp, assets/preview.png and assets/banner.png from the art in assets/.

Dev-only tool, not part of the product install. Needs Pillow:  pip install pillow
    python scripts/gen_assets.py [--out PATH] [--preview PATH] [--banner PATH]

Deterministic: the same inputs and Pillow version give byte-identical output (no timestamps).
Inputs:
  assets/mascot/oysterOS-logo-512.png  the owner's mascot (RGBA). Used pixel-exact, never redrawn.
  assets/loader/frame_0..5.png         6 mascot-area frames cut from an image-to-video render of the mascot
  assets/raw/sheet_*.png               flat feature icons (3x2 sheets); their background becomes the key
  assets/raw/banner_plate_v1.png       README banner background + wordmark (the mascot is added here)
Outputs (RGB565 = r<<11 | g<<5 | b, row-major, 0xF81F = transparent):
  icons 24x24, logo 48x48 and the OysterAi icon (the mascot, reduced to <= 16 colours incl. the UI
  background token), splash 240x135 (procedural plate + mascot + wordmark), loader 8 x 80x80 opaque frames.
The loader frames are the exact mascot plus the video's frame-to-frame changes (blink, smile, sparkles),
so frame 0 equals the mascot area of the splash. The 8-frame sequence holds the neutral frame for half a
second, then plays blink, smile and sparkles (LOADER_SEQUENCE indexes the stored frames).
"""

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "assets" / "raw"
MASCOT = ROOT / "assets" / "mascot" / "oysterOS-logo-512.png"
LOADER_DIR = ROOT / "assets" / "loader"
BANNER_PLATE = RAW / "banner_plate_v1.png"
DEFAULT_OUT = ROOT / "firmware" / "src" / "assets_gen.cpp"
DEFAULT_PREVIEW = ROOT / "assets" / "preview.png"
DEFAULT_BANNER = ROOT / "assets" / "banner.png"

TRANSPARENT = 0xF81F  # must match assets::kTransparent in firmware/include/assets.h
ICON_PX = 24
LOGO_PX = 48
SPLASH_W, SPLASH_H = 240, 135
LOADER_PX = 80  # mascot box on the splash
LOADER_SEQUENCE = (0, 0, 0, 1, 2, 3, 4, 5)  # indices into assets/loader/frame_N.png
LOADER_FRAMES = len(LOADER_SEQUENCE)
LOADER_FPS = 6
BOX_X, BOX_Y = 18, 8  # top-left of the mascot box on the splash

Rgb = tuple[int, int, int]

# Design tokens (docs/DESIGN_SYSTEM.md)
BG: Rgb = (0x10, 0x19, 0x23)
PANEL: Rgb = (0x19, 0x2C, 0x3D)
FOCUS: Rgb = (0x23, 0x4B, 0x54)
ACCENT: Rgb = (0x5E, 0xEA, 0xD4)
TEXT: Rgb = (0xF4, 0xFA, 0xFF)
SECONDARY: Rgb = (0xA9, 0xC4, 0xCE)
WARNING: Rgb = (0xE9, 0xAD, 0x52)
# Mascot palette (approximate), used for the splash glow and sparkles
PURPLE_MID: Rgb = (0x47, 0x25, 0x6E)
PURPLE_LIGHT: Rgb = (0xA9, 0x73, 0xC4)
PEARL_LIGHT: Rgb = (0xE3, 0xBF, 0xE4)

ICON_PALETTE = [ACCENT, TEXT, SECONDARY, WARNING]
MAX_COLOURS = 16  # mascot logo / icon: colour count including the UI background token

MIN_ALPHA = 0.12  # flat icons: pixels explained by less opacity than this of any palette colour = background
MIN_COVER = 100  # of 255: flat-icon output pixels covered less than this stay transparent
ICON_INNER = 22  # flat icon artwork is fitted into this box inside the 24x24 tile
ICON_THICKEN = 0.012  # stroke growth (fraction of the artwork size) so thin lines survive 24 px

# Splash look
GLOW_CX, GLOW_CY, GLOW_RX, GLOW_RY, GLOW_MAX = 58.0, 48.0, 105.0, 85.0, 0.6
DITHER_K = 0.45  # ordered-dither amplitude in 565 steps; low enough that flat tokens stay flat
BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))
BUBBLES = ((118, 10, 1.6), (206, 11, 3.0), (226, 21, 1.8), (228, 44, 1.5), (224, 66, 2.4), (111, 84, 1.3))
SPARKLES = ((106, 22, 3.5), (232, 8, 2.5), (219, 83, 2.8), (206, 36, 2.0))
WORD_X, WORD_BASELINES, WORD_SIZE = 112, (50, 79), 27
# Loader: video deltas below TAU (compression noise) are ignored, the rest fades in over 1/RAMP
LOADER_TAU, LOADER_RAMP = 6, 16

# (IconId name in firmware/include/assets.h order, source sheet, row, column) on a 3x2 grid sheet
SHEET_COLS, SHEET_ROWS = 3, 2
ICON_SOURCES = [
    ("Security", "sheet_modules_v1.png", 0, 0),
    ("OysterAi", None, 0, 0),  # the mascot
    ("Linux", "sheet_modules_v1.png", 0, 2),
    ("ControlDeck", "sheet_modules_v1.png", 1, 0),
    ("SmartHome", "sheet_modules_v1.png", 1, 1),
    ("Apps", "sheet_modules_v1.png", 1, 2),
    ("Settings", "sheet_features_v1.png", 0, 0),
    ("Wifi", "sheet_features_v1.png", 0, 1),
    ("Ble", "sheet_alt_v1.png", 1, 0),
    ("Findings", "sheet_alt_v1.png", 0, 0),
    ("Targets", "sheet_features_v1.png", 1, 1),
    ("Assess", "sheet_features_v1.png", 1, 2),
]


def pixels(img: Image.Image) -> list:
    """Flat pixel list; get_flattened_data replaces the deprecated getdata in newer Pillow."""
    return list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())


def rgb565(r: int, g: int, b: int) -> int:
    v = (((r * 31 + 127) // 255) << 11) | (((g * 63 + 127) // 255) << 5) | ((b * 31 + 127) // 255)
    return 0xF81E if v == TRANSPARENT else v


def border_colour(img: Image.Image) -> Rgb:
    """Median colour of the image border = the flat source background."""
    w, h = img.size
    px = img.load()
    pts = [px[x, 0] for x in range(w)] + [px[x, h - 1] for x in range(w)]
    pts += [px[0, y] for y in range(h)] + [px[w - 1, y] for y in range(h)]
    return tuple(sorted(p[c] for p in pts)[len(pts) // 2] for c in range(3))  # type: ignore[return-value]


# ---- flat icons (AI-generated sheets) -----------------------------------------------------------------


def unmix(p: Rgb, bg: Rgb, palette: list[Rgb]) -> tuple[int, float]:
    """Explain pixel p as palette colour k at opacity a over the source background; returns (k, a).

    Anti-aliased edge pixels then keep their real colour instead of snapping to a wrong neighbour."""
    best_k, best_a, best_res = -1, 0.0, 1 << 60
    for k, f in enumerate(palette):
        d = [f[c] - bg[c] for c in range(3)]
        dd = sum(x * x for x in d)
        a = min(1.0, max(0.0, sum((p[c] - bg[c]) * d[c] for c in range(3)) / dd))
        res = sum((p[c] - bg[c] - a * d[c]) ** 2 for c in range(3))
        if res < best_res:
            best_k, best_a, best_res = k, a, res
    return (best_k, best_a) if best_a >= MIN_ALPHA else (-1, 0.0)


def class_maps(img: Image.Image, palette: list[Rgb], bg: Rgb) -> list[Image.Image]:
    """One 'L' coverage mask per palette class (255 = fully that colour); background belongs to no class."""
    w, h = img.size
    cache: dict[Rgb, tuple[int, float]] = {}
    cls, alpha = [], []
    for p in pixels(img):
        r = cache.get(p)
        if r is None:
            r = cache[p] = unmix(p, bg, palette)
        cls.append(r[0])
        alpha.append(r[1])
    maps = []
    for k in range(len(palette)):
        m = Image.new("L", (w, h))
        m.putdata([round(a * 255) if c == k else 0 for c, a in zip(cls, alpha, strict=True)])
        maps.append(m)
    return maps


def union(maps: list[Image.Image]) -> Image.Image:
    acc = maps[0]
    for m in maps[1:]:
        acc = ImageChops.lighter(acc, m)
    return acc


def fit_to_tile(maps: list[Image.Image], size: int, inner: int, thicken: float) -> list[Image.Image]:
    """Crop to the artwork, optionally thicken strokes, fit into an inner box, centre on the tile."""
    box = union(maps).getbbox()
    if box is None:
        raise ValueError("source cell is empty")
    r = round(thicken * max(box[2] - box[0], box[3] - box[1]))
    w, h = maps[0].size
    pad = (max(0, box[0] - r), max(0, box[1] - r), min(w, box[2] + r), min(h, box[3] + r))
    crops = [m.crop(pad) for m in maps]
    if r > 0:
        crops = [m.filter(ImageFilter.MaxFilter(2 * r + 1)) for m in crops]
    box2 = union(crops).getbbox()
    crops = [m.crop(box2) for m in crops]
    cw, ch = crops[0].size
    scale = inner / max(cw, ch)
    nw, nh = max(1, round(cw * scale)), max(1, round(ch * scale))
    ox, oy = (size - nw) // 2, (size - nh) // 2
    tiles = []
    for m in crops:
        t = Image.new("L", (size, size))
        t.paste(m.resize((nw, nh), Image.BOX), (ox, oy))
        tiles.append(t)
    return tiles


def compose(tiles: list[Image.Image], palette: list[Rgb]) -> list[int]:
    """Coverage maps -> RGB565 list; edge pixels are blended towards the panel colour."""
    data = [pixels(t) for t in tiles]
    out = []
    for i in range(len(data[0])):
        covs = [d[i] for d in data]
        total = min(255, sum(covs))
        if total < MIN_COVER:
            out.append(TRANSPARENT)
            continue
        base = palette[covs.index(max(covs))]
        col = [(base[c] * total + PANEL[c] * (255 - total) + 127) // 255 for c in range(3)]
        out.append(rgb565(*col))
    return out


def make_flat_icon(sheet: Image.Image, row: int, col: int) -> list[int]:
    w, h = sheet.size
    cell = sheet.crop((col * w // SHEET_COLS, row * h // SHEET_ROWS,
                       (col + 1) * w // SHEET_COLS, (row + 1) * h // SHEET_ROWS))
    maps = class_maps(cell, ICON_PALETTE, border_colour(sheet))  # background sampled on the whole sheet
    tiles = fit_to_tile(maps, ICON_PX, ICON_INNER, ICON_THICKEN)
    return compose(tiles, ICON_PALETTE)


# ---- mascot -----------------------------------------------------------------------------------------


def shrink(img: Image.Image, size: int) -> Image.Image:
    """Premultiplied-alpha LANCZOS downscale, so transparent edges do not bleed dark colours in."""
    return img.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")


def mascot_rgba() -> Image.Image:
    return Image.open(MASCOT).convert("RGBA")


def reduce_palette(colours: list[Rgb], n: int) -> list[Rgb]:
    """Palette of at most n colours (Pillow MAXCOVERAGE keeps small features such as the eyes)."""
    strip = Image.new("RGB", (len(colours), 1))
    strip.putdata(colours)
    q = strip.quantize(colors=n, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE)
    pal = q.getpalette() or []
    return [tuple(pal[i * 3:i * 3 + 3]) for i in sorted(set(pixels(q)))]  # type: ignore[misc]


def nearest(p: Rgb, palette: list[Rgb]) -> Rgb:
    return min(palette, key=lambda c: sum((p[k] - c[k]) ** 2 for k in range(3)))


def make_mascot_tile(size: int) -> list[int]:
    """Mascot at size x size: opaque pixels blended over the UI background token and reduced to
    MAX_COLOURS colours (the token itself is one of them); outside the silhouette = transparent key."""
    m = mascot_rgba()
    x0, y0, x1, y1 = m.getchannel("A").getbbox()
    side = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    sq = m.crop((cx - side // 2, cy - side // 2, cx - side // 2 + side, cy - side // 2 + side))
    small = shrink(sq, size)
    cells: list[Rgb | None] = []
    for r, g, b, a in pixels(small):
        cells.append(None if a < 128 else tuple((c * a + BG[i] * (255 - a) + 127) // 255
                                                for i, c in enumerate((r, g, b))))  # type: ignore[arg-type]
    opaque = [c for c in cells if c is not None]
    palette = [BG] + reduce_palette(opaque, MAX_COLOURS - 1)
    return [TRANSPARENT if c is None else rgb565(*nearest(c, palette)) for c in cells]


# ---- splash and loader ------------------------------------------------------------------------------


def overlay_layer(draw_fn) -> Image.Image:
    """RGBA overlay drawn at 4x and averaged down, for small antialiased shapes."""
    s = 4
    layer = Image.new("RGBA", (SPLASH_W * s, SPLASH_H * s), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), s)
    return layer.convert("RGBa").resize((SPLASH_W, SPLASH_H), Image.BOX).convert("RGBA")


def make_plate() -> Image.Image:
    """240x135 background: UI background token, soft purple glow behind the mascot, bubbles, sparkles."""
    data = []
    for y in range(SPLASH_H):
        for x in range(SPLASH_W):
            dx, dy = (x - GLOW_CX) / GLOW_RX, (y - GLOW_CY) / GLOW_RY
            g = GLOW_MAX * math.exp(-(dx * dx + dy * dy))
            data.append(tuple(round(BG[c] + (PURPLE_MID[c] - BG[c]) * g) for c in range(3)))
    plate = Image.new("RGB", (SPLASH_W, SPLASH_H))
    plate.putdata(data)

    def bubbles(d: ImageDraw.ImageDraw, s: int) -> None:
        for x, y, r in BUBBLES:
            d.ellipse((x * s - r * s, y * s - r * s, x * s + r * s, y * s + r * s),
                      outline=(*PURPLE_LIGHT, 150), width=max(1, round(0.5 * s)))
            hx, hy = x - r * 0.35, y - r * 0.35
            d.ellipse((hx * s - 0.5 * s, hy * s - 0.5 * s, hx * s + 0.5 * s, hy * s + 0.5 * s),
                      fill=(*PEARL_LIGHT, 190))

    def sparkles(d: ImageDraw.ImageDraw, s: int) -> None:
        for x, y, arm in SPARKLES:
            w = arm * 0.2
            pts = [(x, y - arm), (x + w, y - w), (x + arm, y), (x + w, y + w),
                   (x, y + arm), (x - w, y + w), (x - arm, y), (x - w, y - w)]
            d.polygon([(px * s, py * s) for px, py in pts], fill=(*PEARL_LIGHT, 200))

    rgba = plate.convert("RGBA")
    rgba.alpha_composite(overlay_layer(bubbles))
    rgba.alpha_composite(overlay_layer(sparkles))
    return rgba.convert("RGB")


def loader_frames(plate: Image.Image) -> list[Image.Image]:
    """8 opaque 80x80 RGB frames: the exact mascot over the plate, plus the video's changes per frame."""
    exact = shrink(mascot_rgba(), LOADER_PX)
    vids = []
    for i in range(max(LOADER_SEQUENCE) + 1):
        v = Image.open(LOADER_DIR / f"frame_{i}.png").convert("RGB")
        vids.append(v.resize((LOADER_PX, LOADER_PX), Image.LANCZOS))
    box = (BOX_X, BOX_Y, BOX_X + LOADER_PX, BOX_Y + LOADER_PX)
    base_rgb = pixels(exact.convert("RGB"))
    v0 = pixels(vids[0])
    frames = []
    for i in LOADER_SEQUENCE:
        vk = vids[i]
        if i == 0:
            rgb = base_rgb
        else:
            d = ImageChops.difference(vk, vids[0]).convert("L")
            gate = d.point(lambda x: 0 if x < LOADER_TAU else min(255, (x - LOADER_TAU) * LOADER_RAMP))
            gate = gate.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))
            rgb = [tuple(max(0, min(255, round(e[c] + (b[c] - a[c]) * m / 255))) for c in range(3))
                   for e, a, b, m in zip(base_rgb, v0, pixels(vk), pixels(gate), strict=True)]
        sprite = Image.new("RGB", exact.size)
        sprite.putdata(rgb)
        layer = Image.merge("RGBA", (*sprite.split(), exact.getchannel("A")))
        canvas = plate.crop(box).convert("RGBA")
        canvas.alpha_composite(layer)
        frames.append(canvas.convert("RGB"))
    return frames


def draw_wordmark(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=WORD_SIZE)  # Pillow's bundled font: no system font needed
    for text, y, fill in (("OYSTER", WORD_BASELINES[0], TEXT), ("OS", WORD_BASELINES[1], ACCENT)):
        d.text((WORD_X, y), text, font=font, fill=fill, anchor="ls", stroke_width=1, stroke_fill=fill)


def dither565(img: Image.Image, x0: int, y0: int) -> list[int]:
    """RGB -> RGB565 with a 4x4 ordered dither keyed on the absolute screen position (x0, y0 = origin)."""
    w = img.size[0]
    out = []
    for i, (r, g, b) in enumerate(pixels(img)):
        t = ((BAYER4[((i // w) + y0) & 3][((i % w) + x0) & 3] + 0.5) / 16 - 0.5) * DITHER_K
        r5 = min(31, max(0, int(r * 31 / 255 + 0.5 + t)))
        g6 = min(63, max(0, int(g * 63 / 255 + 0.5 + t)))
        b5 = min(31, max(0, int(b * 31 / 255 + 0.5 + t)))
        out.append((r5 << 11) | (g6 << 5) | b5)
    return out


# ---- banner -----------------------------------------------------------------------------------------


def make_banner() -> Image.Image:
    """README banner: AI-generated plate (background, wordmark, tagline) with the exact mascot on the left."""
    plate = Image.open(BANNER_PLATE).convert("RGBA")
    w, h = plate.size
    size = round(h * 0.6)
    sprite = shrink(mascot_rgba(), size)
    cx, cy = round(w * 0.215), round(h * 0.5)
    pos = (cx - size // 2, cy - size // 2)
    margin = size // 4  # the blurred halo needs room beyond the sprite box
    mask = Image.new("L", (size + 2 * margin, size + 2 * margin), 0)
    mask.paste(sprite.getchannel("A"), (margin, margin))
    halo = mask.filter(ImageFilter.GaussianBlur(size * 0.06)).point(lambda v: v * 6 // 10)
    glow = Image.new("RGBA", plate.size, (0, 0, 0, 0))
    glow.paste(Image.new("RGBA", mask.size, (*PURPLE_LIGHT, 255)), (pos[0] - margin, pos[1] - margin), halo)
    plate.alpha_composite(glow)
    plate.alpha_composite(sprite, pos)
    return plate.convert("RGB")


# ---- output -----------------------------------------------------------------------------------------


def render_cpp(icons: dict[str, list[int]], logo: list[int], splash: list[int], loader: list[int]) -> str:
    def array(name: str, vals: list[int]) -> list[str]:
        lines = [f"static const uint16_t {name}[{len(vals)}] = {{"]
        for i in range(0, len(vals), 16):
            lines.append("  " + ", ".join(f"0x{v:04X}" for v in vals[i:i + 16]) + ",")
        lines.append("};")
        return lines

    out = [
        "// GENERATED by scripts/gen_assets.py from assets/. Do not edit; rerun the script.",
        "// RGB565 (r<<11 | g<<5 | b), row-major. static const data lives in flash (.rodata), not in RAM.",
        "// 0xF81F (assets::kTransparent) marks pixels to skip. See assets/PROVENANCE.md for the art source.",
        '#include "assets.h"',
        "",
        "namespace oyster::assets {",
        "",
    ]
    for name, vals in icons.items():
        out.append(f"// icon {name} {ICON_PX}x{ICON_PX}")
        out += array(f"kIcon{name}", vals)
        out.append("")
    out.append(f"// logo {LOGO_PX}x{LOGO_PX} (mascot, <= {MAX_COLOURS} colours)")
    out += array("kLogoPx", logo)
    out.append("")
    out.append(f"// splash {SPLASH_W}x{SPLASH_H}; the mascot box ({BOX_X},{BOX_Y}) is loader frame 0")
    out += array("kSplashPx", splash)
    out.append("")
    out.append(f"// loader {LOADER_FRAMES} frames of {LOADER_PX}x{LOADER_PX}, back to back")
    out += array("kLoaderPx", loader)
    out.append("")
    out.append("static const Image kIcons[] = {")
    for name in icons:
        out.append(f"  {{{ICON_PX}, {ICON_PX}, kIcon{name}}},  // IconId::{name}")
    out.append("};")
    out += [
        "constexpr unsigned kIconCount = sizeof(kIcons) / sizeof(kIcons[0]);",
        "static_assert(kIconCount == static_cast<unsigned>(IconId::Count), \"icon table matches IconId\");",
        "",
        f"static const Image kLogo = {{{LOGO_PX}, {LOGO_PX}, kLogoPx}};",
        f"static const Image kSplash = {{{SPLASH_W}, {SPLASH_H}, kSplashPx}};",
        f"static const Animation kLoader = {{{LOADER_PX}, {LOADER_PX}, {BOX_X}, {BOX_Y}, {LOADER_FRAMES}, "
        f"{LOADER_FPS}, kLoaderPx}};",
        "",
        "// An out-of-range id returns the first icon instead of reading past the table.",
        "const Image& icon(IconId id) {",
        "  const auto i = static_cast<unsigned>(id);",
        "  return kIcons[i < kIconCount ? i : 0];",
        "}",
        "",
        "const Image& logo() { return kLogo; }",
        "",
        "const Image& splash() { return kSplash; }",
        "",
        "const Animation& loader() { return kLoader; }",
        "",
        "}  // namespace oyster::assets",
        "",
    ]
    return "\n".join(out)


def to_image(vals: list[int], w: int, h: int, behind: Rgb) -> Image.Image:
    img = Image.new("RGB", (w, h), behind)
    px = []
    for v in vals:
        if v == TRANSPARENT:
            px.append(behind)
        else:
            px.append((((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31))
    img.putdata(px)
    return img


def render_preview(icons: dict[str, list[int]], logo: list[int], splash: list[int],
                   loader: list[int]) -> Image.Image:
    """Contact sheet: splash 2x, logo 4x, loader frames 2x, icons 4x on root bg plus 1x on panel/focus."""
    font = ImageFont.load_default()
    z, pad, cols = 4, 12, 6
    cell = ICON_PX * z + 2 * pad
    row_h = ICON_PX * z + 14 + ICON_PX + pad
    top_h = SPLASH_H * 2
    rows = (len(icons) + cols - 1) // cols
    fw = LOADER_PX * 2
    width = max(cols * cell + pad, SPLASH_W * 2 + LOGO_PX * z + 3 * pad, 4 * (fw + pad) + pad)
    height = pad + top_h + pad + 12 + 2 * (fw + pad) + 12 + rows * row_h + pad
    sheet = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(sheet)

    def zoomed(vals: list[int], w: int, h: int, k: int) -> Image.Image:
        return to_image(vals, w, h, BG).resize((w * k, h * k), Image.NEAREST)

    sheet.paste(zoomed(splash, SPLASH_W, SPLASH_H, 2), (pad, pad))
    lx = SPLASH_W * 2 + 2 * pad
    sheet.paste(zoomed(logo, LOGO_PX, LOGO_PX, z), (lx, pad))
    d.text((lx, pad + LOGO_PX * z + 6), "logo 48x48 at 4x", fill=SECONDARY, font=font)
    d.text((pad, pad + top_h + 2), "splash 240x135 at 2x", fill=SECONDARY, font=font)
    y = pad + top_h + pad + 12
    caption = f"loader {LOADER_FRAMES} frames {LOADER_PX}x{LOADER_PX} at 2x, {LOADER_FPS} fps"
    d.text((pad, y - 12), caption, fill=SECONDARY, font=font)
    n_px = LOADER_PX * LOADER_PX
    for i in range(LOADER_FRAMES):
        fx = pad + (i % 4) * (fw + pad)
        fy = y + (i // 4) * (fw + pad)
        sheet.paste(zoomed(loader[i * n_px:(i + 1) * n_px], LOADER_PX, LOADER_PX, 2), (fx, fy))
    y += 2 * (fw + pad) + 12
    d.text((pad, y - 12), "icons 24x24: 4x, then 1x on panel and focus", fill=SECONDARY, font=font)
    for n, name in enumerate(icons):
        x = pad + (n % cols) * cell
        yy = y + (n // cols) * row_h
        sheet.paste(zoomed(icons[name], ICON_PX, ICON_PX, z), (x, yy))
        d.text((x, yy + ICON_PX * z + 2), name, fill=SECONDARY, font=font)
        for k, behind in enumerate((PANEL, FOCUS)):
            sheet.paste(to_image(icons[name], ICON_PX, ICON_PX, behind),
                        (x + k * (ICON_PX + 4), yy + ICON_PX * z + 14))
    return sheet


def build() -> tuple[dict[str, list[int]], list[int], list[int], list[int]]:
    sheets: dict[str, Image.Image] = {}
    icons: dict[str, list[int]] = {}
    for name, fname, row, col in ICON_SOURCES:
        if fname is None:
            icons[name] = make_mascot_tile(ICON_PX)
            continue
        if fname not in sheets:
            sheets[fname] = Image.open(RAW / fname).convert("RGB")
        icons[name] = make_flat_icon(sheets[fname], row, col)
    logo = make_mascot_tile(LOGO_PX)
    plate = make_plate()
    frames = loader_frames(plate)
    splash_img = plate.copy()
    splash_img.paste(frames[0], (BOX_X, BOX_Y))
    draw_wordmark(splash_img)
    splash = dither565(splash_img, 0, 0)
    loader: list[int] = []
    for f in frames:
        loader += dither565(f, BOX_X, BOX_Y)
    return icons, logo, splash, loader


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    ap.add_argument("--banner", type=Path, default=DEFAULT_BANNER)
    args = ap.parse_args()
    art = build()
    args.out.write_text(render_cpp(*art), encoding="utf-8", newline="\n")
    render_preview(*art).save(args.preview, optimize=True)
    make_banner().save(args.banner, optimize=True)
    total = sum(len(a) for a in (*art[0].values(), art[1], art[2], art[3])) * 2
    print(f"wrote {args.out.name}, {args.preview.name}, {args.banner.name}: {total} B of art in flash")


if __name__ == "__main__":
    main()
