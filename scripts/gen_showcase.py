"""Generate the showcase artwork: assets/brand/*.png and the landing-page copies in docs/site/.

Dev-only tool, not part of the product install. Needs Pillow:  pip install pillow
    python scripts/gen_showcase.py

Deterministic: the same inputs and Pillow version give byte-identical output (no timestamps, no metadata).
Inputs:
  assets/mascot/oysterOS-logo-512.png   the mascot (RGBA). Used pixel-exact, never redrawn.
  assets/readme/cardputer-home.png      the emulator page with the Home screen (a real render of the UI)
  assets/raw/hero_plate_v2.png          background plate: light rays, bubbles, circuit traces
  assets/raw/wave_plate_v2.png          background plate: flowing ribbons
  assets/raw/wordmark_v2.png            the OysterOS wordmark on a transparent background
  assets/raw/emblem_sheet_v2.png        six feature emblems, 3 x 2, on a transparent background
Outputs:
  assets/brand/hero.png                 README banner: plate + mascot + wordmark + tagline + the device
  assets/brand/social-preview.png       1280 x 640 card for link previews
  assets/brand/wordmark.png             the wordmark, trimmed
  assets/brand/emblem-<name>.png        the six emblems, cut from the sheet
  docs/site/*.png                       the same pieces, sized for the landing page (docs/index.html)
Where each source comes from is recorded in assets/PROVENANCE.md.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "assets" / "raw"
BRAND = ROOT / "assets" / "brand"
SITE = ROOT / "docs" / "site"
MASCOT = ROOT / "assets" / "mascot" / "oysterOS-logo-512.png"
DEVICE = ROOT / "assets" / "readme" / "cardputer-home.png"
HERO_PLATE = RAW / "hero_plate_v2.png"
WAVE_PLATE = RAW / "wave_plate_v2.png"
WORDMARK = RAW / "wordmark_v2.png"
EMBLEM_SHEET = RAW / "emblem_sheet_v2.png"

# Design tokens (docs/DESIGN_SYSTEM.md)
NAVY = (16, 25, 35)
SEAFOAM = (94, 234, 212)
LILAC = (183, 156, 255)
PEARL = (232, 224, 255)

PNG_BUDGET = 880_000  # tests/assets keeps every PNG under 900 kB
EMBLEMS = ("ai", "confirm", "recon", "home", "scripts", "engineering")  # sheet order, row by row
TAGLINE = ("A pocket operating system for", "security, AI and automation")
LABEL = "M5STACK CARDPUTER ADV  /  ESP32-S3  /  C++ AND PYTHON"


def rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def stripped(img: Image.Image) -> Image.Image:
    """A copy that carries pixels only: no text, colour-profile or provenance chunks."""
    return Image.frombytes(img.mode, img.size, img.tobytes())


def save_png(img: Image.Image, path: Path) -> None:
    """Write a metadata-free PNG; photographic RGB art that is over budget is reduced to 256 colours."""
    path.parent.mkdir(parents=True, exist_ok=True)
    out = stripped(img)
    out.save(path, optimize=True)
    if path.stat().st_size > PNG_BUDGET and out.mode == "RGB":
        out.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(
            path, optimize=True)
    if path.stat().st_size > PNG_BUDGET:
        raise SystemExit(f"{path.name}: {path.stat().st_size} bytes is over the {PNG_BUDGET} byte budget")


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and centre-crop so the image fills w x h."""
    s = max(w / img.width, h / img.height)
    im = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def fit_width(img: Image.Image, w: int) -> Image.Image:
    return img.resize((w, round(img.height * w / img.width)), Image.LANCZOS)


def fit_height(img: Image.Image, h: int) -> Image.Image:
    return img.resize((round(img.width * h / img.height), h), Image.LANCZOS)


def full_alpha(img: Image.Image) -> Image.Image:
    """The generated transparent art peaks just below full opacity; stretch its alpha to the full range."""
    a = img.getchannel("A")
    top = a.getextrema()[1] or 255
    out = img.copy()
    out.putalpha(a.point(lambda v: min(255, round(v * 255 / top))))
    return out


def trim(img: Image.Image, floor: int, pad: int) -> Image.Image:
    """Crop to the pixels more opaque than `floor`, keeping `pad` pixels of soft edge."""
    x0, y0, x1, y1 = img.getchannel("A").point(lambda v: 255 if v > floor else 0).getbbox()
    return img.crop((max(0, x0 - pad), max(0, y0 - pad), min(img.width, x1 + pad), min(img.height, y1 + pad)))


def device_cutout() -> Image.Image:
    """The drawn Cardputer without the page around it.

    The emulator page is dark and the device body is light, so everything that a flood fill from the corner
    cannot reach belongs to the device (its dark screen bezel and keys included).
    """
    src = Image.open(DEVICE).convert("RGB")
    solid = src.convert("L").point(lambda v: 255 if v > 64 else 0)
    ImageDraw.floodfill(solid, (0, 0), 128)
    alpha = solid.point(lambda v: 0 if v == 128 else 255)
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.9))
    out = src.convert("RGBA")
    out.putalpha(alpha)
    return out.crop(alpha.getbbox())


def glow(size: tuple[int, int], box: tuple[int, int, int, int], colour: tuple[int, int, int], strength: int,
         blur: float) -> Image.Image:
    """A soft elliptical light, used behind the mascot and the device."""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse(box, fill=strength)
    layer = Image.new("RGBA", size, colour + (0,))
    layer.putalpha(mask.filter(ImageFilter.GaussianBlur(blur)))
    return layer


def shadow(img: Image.Image, blur: float, dy: int, opacity: float) -> tuple[Image.Image, int]:
    """The image on a larger transparent canvas with a soft drop shadow; returns it and the padding added."""
    pad = round(blur * 3) + abs(dy)
    canvas = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    mask = Image.new("L", canvas.size, 0)
    mask.paste(img.getchannel("A"), (pad, pad + dy))
    mask = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: round(v * opacity))
    dark = Image.new("RGBA", canvas.size, (5, 4, 18, 0))
    dark.putalpha(mask)
    canvas.alpha_composite(dark)
    canvas.alpha_composite(img, (pad, pad))
    return canvas, pad


def centred_text(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, size: int, fill: tuple[int, int, int],
                 tracking: float = 0.0, bold: bool = False) -> None:
    """One line of Pillow's bundled font (no system font needed), optionally letter-spaced."""
    font = ImageFont.load_default(size=size)
    stroke = 1 if bold else 0
    if not tracking:
        draw.text((cx, y), text, font=font, fill=fill, anchor="ma", stroke_width=stroke, stroke_fill=fill)
        return
    widths = [font.getlength(ch) for ch in text]
    x = cx - (sum(widths) + tracking * (len(text) - 1)) / 2
    for ch, w in zip(text, widths, strict=True):
        draw.text((x, y), ch, font=font, fill=fill, anchor="la", stroke_width=stroke, stroke_fill=fill)
        x += w + tracking


def compose(plate_path: Path, w: int, h: int, k: float, brand_cx: float, device_cx: float,
            device_w: float) -> Image.Image:
    """Banner layout: mascot, wordmark and tagline on the left, the device on the right.

    `k` scales every size (1.0 suits a 1600 px wide canvas); the two centre positions and the device width
    are fractions of the canvas width.
    """
    img = cover(rgba(plate_path), w, h)
    # Calm the plate behind the device so the product reads first.
    shade = Image.new("L", (w, h), 0)
    ImageDraw.Draw(shade).rectangle((round(w * 0.42), 0, w, h), fill=90)
    veil = Image.new("RGBA", (w, h), NAVY + (0,))
    veil.putalpha(shade.filter(ImageFilter.GaussianBlur(w * 0.09)))
    img.alpha_composite(veil)

    cx = round(w * brand_cx)
    mascot = fit_height(rgba(MASCOT), round(238 * k))
    wordmark = fit_width(full_alpha(rgba(WORDMARK)), round(640 * k))
    block_h = mascot.height + round(wordmark.height * 0.62) + round(150 * k)
    top = (h - block_h) // 2

    my = top
    # ... and behind the tagline, so bubbles in the plate never sit under the words.
    text_top = my + mascot.height + round(wordmark.height * 0.50)
    img.alpha_composite(glow((w, h), (cx - round(330 * k), text_top, cx + round(330 * k),
                                      text_top + round(150 * k)), (30, 16, 70), 150, 34 * k))
    img.alpha_composite(glow((w, h), (cx - round(230 * k), my - round(30 * k), cx + round(230 * k),
                                      my + mascot.height + round(40 * k)), (190, 150, 255), 105, 70 * k))
    img.alpha_composite(mascot, (cx - mascot.width // 2, my))

    wy = my + mascot.height - round(wordmark.height * 0.24)
    img.alpha_composite(wordmark, (cx - wordmark.width // 2, wy))

    draw = ImageDraw.Draw(img)
    ty = wy + round(wordmark.height * 0.80)
    for i, line in enumerate(TAGLINE):
        centred_text(draw, cx, ty + round(i * 38 * k), line, round(29 * k), PEARL)
    centred_text(draw, cx, ty + round(98 * k), LABEL, round(15 * k), SEAFOAM, tracking=2.0 * k)

    device = fit_width(device_cutout(), round(w * device_w)).rotate(3.5, resample=Image.BICUBIC, expand=True)
    dcx, dcy = round(w * device_cx), h // 2
    hw, hh = device.width // 2, device.height // 3
    halo = (dcx - hw, dcy - hh, dcx + hw, dcy + hh)
    img.alpha_composite(glow((w, h), halo, (124, 92, 255), 70, 90 * k))
    card, pad = shadow(device, 26 * k, round(20 * k), 0.62)
    img.alpha_composite(card, (dcx - device.width // 2 - pad, dcy - device.height // 2 - pad))
    return img.convert("RGB")


def emblems() -> dict[str, Image.Image]:
    """Cut the 3 x 2 sheet into square emblems; the soft light around each fades out before its cell edge."""
    sheet = full_alpha(rgba(EMBLEM_SHEET))
    cw, ch = sheet.width / 3, sheet.height / 2
    side = round(max(cw, ch))
    out = {}
    for i, name in enumerate(EMBLEMS):
        r, c = divmod(i, 3)
        cell = sheet.crop((round(c * cw), round(r * ch), round((c + 1) * cw), round((r + 1) * ch)))
        tile = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        tile.alpha_composite(cell, ((side - cell.width) // 2, (side - cell.height) // 2))
        out[name] = tile
    return out


def main() -> None:
    save_png(compose(HERO_PLATE, 1440, 617, 0.9, 0.28, 0.752, 0.455), BRAND / "hero.png")
    save_png(compose(WAVE_PLATE, 1280, 640, 0.86, 0.27, 0.745, 0.46), BRAND / "social-preview.png")

    wordmark = trim(full_alpha(rgba(WORDMARK)), 24, 36)
    save_png(fit_width(wordmark, 900), BRAND / "wordmark.png")
    save_png(fit_width(wordmark, 720), SITE / "wordmark.png")

    for name, tile in emblems().items():
        save_png(tile.resize((320, 320), Image.LANCZOS), BRAND / f"emblem-{name}.png")
        save_png(tile.resize((200, 200), Image.LANCZOS), SITE / f"emblem-{name}.png")

    save_png(fit_height(rgba(MASCOT), 320), SITE / "mascot.png")
    save_png(cover(rgba(WAVE_PLATE), 1600, 686).convert("RGB"), SITE / "backdrop.png")
    for path in sorted([*BRAND.glob("*.png"), *SITE.glob("*.png")]):
        print(f"{path.relative_to(ROOT).as_posix():44s} {path.stat().st_size:>9,d} B")


if __name__ == "__main__":
    main()
