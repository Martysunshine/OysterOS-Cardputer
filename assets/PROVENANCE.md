# Asset provenance

The mascot is the project's own artwork. The loader animation's motion reference, the banner and showcase
backgrounds, the wordmark, the feature icons and the feature emblems are **AI-generated**; their use is subject
to the providers' usage terms (see the models listed below). All PNGs in this folder carry no embedded metadata (it was removed; the pixels are unchanged).

## The mascot (supplied artwork, not AI-generated here)

`assets/mascot/oysterOS-logo-512.png` (512x512 RGBA, transparent background, no metadata) is the project's own artwork: a glossy purple open oyster shell with a smiling pearl character. It is the source of truth for the mascot and was never redrawn, restyled or AI-edited. Everything derived from it is a mechanical resample of this file:

- logo 48x48 and the `OysterAi` icon 24x24: premultiplied-alpha LANCZOS downscale, edge pixels blended over the UI background token `#101923`, then colour-reduced to at most 16 colours including that token;
- the 80x80 mascot on the boot splash and in loader frame 0: the same downscale, full colour (plus an ordered dither when converted to RGB565);
- the banner: the file composited at about 60% of the banner height (a soft purple halo behind it).

## AI-generated parts

| Item | Date | Model id | Settings | Used for |
|---|---|---|---|---|
| `raw/sheet_modules_v1.png`, `raw/sheet_features_v1.png`, `raw/sheet_alt_v1.png` | 2026-10-24 | `recraft_v4_1` (Recraft V4.1) | 1k, flat background `#101923`, palette from the design tokens | the 11 flat feature icons |
| `loader/frame_0..5.png` | 2026-10-24 | `wan3_0` (Wan 3.0, image-to-video) | 4 s, 480p, 1:1, audio off, first and last frame identical | blink, smile and sparkle changes of the loader animation |
| `raw/banner_plate_v1.png` | 2026-10-24 | `gpt_image_2_5` (GPT Image 2.5) | quality high, 1k, 16:9, mascot as style reference only | banner background, wordmark and tagline |
| `raw/hero_plate_v2.png`, `raw/wave_plate_v2.png` | 2026-11-13 | `gpt_image_2_6` (GPT Image 2.6) | quality high, 2k, 21:9, mascot as style reference only | backgrounds of the showcase banner, the social preview and the landing page |
| `raw/wordmark_v2.png` | 2026-11-13 | `gpt_image_2_6` (GPT Image 2.6) | quality high, 2k, 21:9, transparent background, mascot as style reference only | the OysterOS wordmark |
| `raw/emblem_sheet_v2.png` | 2026-11-13 | `gpt_image_2_6` (GPT Image 2.6) | quality high, 2k, 3:2, transparent background, mascot as style reference only | the six feature emblems of the README |

Loader details. The start image was the mascot (the file above, scaled to 440 px) on a flat dark purple background (design-token navy with a soft purple centre glow). The video was sampled at 6 fps (24 frames); frames 0, 9, 10, 12, 15 and 19 are stored, cropped to the mascot area and downscaled to 160x160. The generator does not use their pixels directly: it adds only the frame-to-frame differences from frame 0 (blink, smile, sparkles; differences under a noise threshold are ignored) onto the exact mascot, so every static pixel of the mascot comes from the supplied artwork. The 8-frame sequence is: neutral x3, blink, smile, smile wide, sparkles, fading sparkles, at 6 fps. Frame 0 is identical to the mascot area of the splash.

Video prompt (the start image was the only reference):
> Static camera, seamless loop. The cute pearl mascot in the shell blinks once softly and smiles; tiny sparkles twinkle gently on the pearl. Nothing else moves: the shell, the background and the framing stay perfectly still.

Banner plate prompt (the mascot image was attached as a reference; the model was told to leave the left third empty and the real mascot was added locally):
> Wide 16:9 README banner background for a pocket computer project, in the colour palette and glossy cute style of the reference mascot (deep navy, purple, lilac, soft pearl highlights). Deep navy background (#101923) with a soft purple underwater glow, a few tiny bubbles and small sparkles. The LEFT THIRD is left completely empty (only the soft glow): do NOT draw any shell, pearl, oyster or character anywhere, a mascot will be placed there later. On the RIGHT two thirds, vertically centred: the wordmark OysterOS in large clean bold sans-serif letters (light lilac-white, with the letters OS in seafoam #5EEAD4), and below it in a smaller lighter line the tagline: A pocket computer for security, Linux, AI and automations. Text must be spelled exactly, crisp and legible, no other text.

The plate was downscaled from 1344x752 to 1200x671 to keep the file small. The model added a full stop to the tagline. No personal data in any prompt.

## Showcase artwork (2026-11-13)

`scripts/gen_showcase.py` builds the README banner, the social preview, the wordmark and the six emblems in
`assets/brand/`, and the copies the landing page uses in `docs/site/`. It is deterministic for a given Pillow
version. The rule of the banner above still holds: the model never draws the mascot. Each plate was generated
without any character, and the exact mascot file is composited locally. The device in the banner is not
generated either: it is the drawn Cardputer of the emulator page (`readme/cardputer-home.png`, a render of
the firmware's UI code), cut out of its page background. The tagline is set in the font bundled with Pillow.

The four sources were generated at 2688x1152 (the sheet at 2048x1360) and are stored smaller to keep the
files small: the plates at 1600x686, the wordmark cropped to its letters at 1400x445, the sheet at 1200x797.
The mascot image was the only reference attached. No personal data in any prompt.

Background plate with light rays and circuit traces (`raw/hero_plate_v2.png`):
> Wide cinematic background plate for a software project banner. A calm underwater scene in the colour palette and glossy, cute material style of the reference image: deep navy (#101923) fading into rich violet, lilac and soft pearl highlights, with a few small seafoam (#5EEAD4) accents. Soft light rays fall from the top left; a gentle purple glow sits in the left third. A scattering of small glossy bubbles and a few tiny four-point sparkles drift across the scene. Along the bottom edge, very subtle thin seafoam circuit-board traces glow faintly like a seabed. BACKGROUND ONLY: do not draw any shell, pearl, oyster, creature, character, device, text, letters, numbers or logo anywhere. Keep the centre and the right half dark, calm and low in detail so that a title and a product picture can be placed on top later. Smooth clean gradients, no film grain, no noise, no border.

Background plate with flowing ribbons (`raw/wave_plate_v2.png`):
> Wide cinematic background plate for a software project banner. A deep, calm underwater scene in the colour palette and glossy material style of the reference image: deep navy (#101923) with rich violet and lilac, soft pearl highlights and a few small seafoam (#5EEAD4) accents. Two or three soft, flowing ribbons of violet light curve gently across the lower half like slow water currents. A scattering of small glossy bubbles of different sizes and a few tiny four-point sparkles. A soft purple glow in the left third, faint light rays from above. BACKGROUND ONLY: do not draw any shell, pearl, oyster, creature, character, device, text, letters, numbers or logo anywhere. Keep the centre and the right half dark, calm and low in detail so that a title and a product picture can be placed on top later. Smooth clean gradients, no film grain, no noise, no border.

Wordmark (`raw/wordmark_v2.png`):
> A logotype on a fully transparent background: the single word OysterOS in large, bold, rounded, friendly sans-serif letters with a soft glossy finish and a subtle inner highlight, matching the glossy material of the reference image. The six letters O-y-s-t-e-r are pearl lilac-white (#EDE4FF); the last two letters O-S are seafoam (#5EEAD4). One line, horizontally centred, filling about 80 percent of the width, with generous empty transparent padding around it. A very soft violet glow hugs the letters. No tagline, no other text, no mascot, no shell, no pearl, no shapes, no underline, no background. The text must be spelled exactly: OysterOS.

Emblem sheet (`raw/emblem_sheet_v2.png`):
> Icon sheet of exactly six separate glossy 3D emblems arranged in a grid of 3 columns by 2 rows on a fully transparent background. Equal size, generous equal spacing, each emblem alone in its own cell with a wide empty margin around it. No cards, no borders, no labels, no text, no letters, no numbers. Material and palette of the reference image: glossy deep-violet and lilac toy-like surfaces with soft pearl highlights and small glowing seafoam (#5EEAD4) accents, soft lighting from the top left, subtle rim light, no shadows cast on the background. Row 1, left to right: (1) a rounded speech bubble holding one glowing pearl with a small four-point sparkle; (2) a chunky keyboard keycap with a seafoam check mark on its top face; (3) a round radar screen with a seafoam sweeping beam and two small glowing blips. Row 2, left to right: (4) a small house with one glowing seafoam window; (5) a document page showing a pair of curly braces and a small play triangle; (6) a square microchip with pins on all four sides and a small shield with a check mark on top. No faces, no eyes, no characters. All six emblems in exactly the same style, scale and viewing angle: front view, seen slightly from above.

## Procedural parts (no AI)

- Boot splash background: the `#101923` token with a soft purple glow (the mascot's `#47256E`) behind the mascot, a few tiny bubbles and sparkles, ordered-dithered to RGB565. The lower third is left empty for the firmware's "Starting: ..." line.
- Wordmark `OYSTER` / `OS`: rendered by `scripts/gen_assets.py` with the font bundled in Pillow (Aileron), bold by a 1 px stroke; `OS` in the seafoam accent `#5EEAD4`.
- `brand/timeline.svg`, `brand/stats.svg` and `brand/divider.svg`: hand-written SVG in the design tokens, with text in the reader's system fonts.

## Files

| File | What |
|---|---|
| `mascot/oysterOS-logo-512.png` | the mascot (source of truth) |
| `raw/sheet_*_v1.png` | icon sheets, cropped into the 11 feature icons |
| `raw/banner_plate_v1.png` | banner background (the mascot is composited locally) |
| `loader/frame_*.png` | motion reference frames, cropped to the mascot area |
| `banner.png` | README banner (generated by `scripts/gen_assets.py`) |
| `preview.png` | contact sheet of the converted result (splash, logo, loader frames, icons) |
| `readme/screens/*.png` | README tile screens, rendered pixel for pixel by the browser emulator's port of the firmware's drawing code (`docs/emulator/src/lcd.js`) at 3x; no AI |
| `readme/cardputer-home.png`, `readme/cardputer-demo.png` | the emulator page (`docs/emulator/index.html`: a CSS drawing of the Cardputer ADV with the ported screen) captured in a headless browser; the walkthrough is an animated PNG with captions added by Pillow; no AI |
| `readme/icons/*.png` | the firmware's 24x24 tile icons exported from `scripts/gen_assets.py` at 2x for the README table; no AI |
| `raw/hero_plate_v2.png`, `raw/wave_plate_v2.png` | background plates of the showcase banner and the social preview |
| `raw/wordmark_v2.png`, `raw/emblem_sheet_v2.png` | the wordmark and the sheet the six emblems are cut from |
| `brand/hero.png`, `brand/social-preview.png` | composed by `scripts/gen_showcase.py`: plate, exact mascot, wordmark, tagline and the emulator's drawn device |
| `brand/wordmark.png`, `brand/emblem-*.png` | the wordmark trimmed and the emblems cut from the sheet by `scripts/gen_showcase.py` |
| `brand/timeline.svg`, `brand/stats.svg`, `brand/divider.svg` | hand-written SVG; no AI |

`scripts/gen_assets.py` turns these sources into `firmware/src/assets_gen.cpp` (RGB565, `0xF81F` = transparent) and
the preview and banner. It is deterministic for a given Pillow version. `scripts/gen_showcase.py` builds
`assets/brand/` and `docs/site/` the same way.

## Prompts

Module icon sheet (3:2):
> Icon sheet of exactly six separate flat pictogram icons arranged in a 3 columns by 2 rows grid, equal size, generous equal spacing, each icon alone in its own cell with a wide empty margin, no cards, no borders, no labels, no text. Row 1 left to right: a shield with a keyhole; an open oyster shell holding a pearl with a small four-point sparkle above it; a terminal window with a chevron prompt symbol and a cursor bar. Row 2 left to right: a 3 by 3 grid of rounded keypad keys; a house with a small round dot on its door; a 2 by 2 grid of four rounded squares. Bold thick strokes, simple geometric shapes, high contrast, readable at 24 pixels. Seafoam main color, pale white and slate-grey secondary, one small amber accent per icon. No gradients, no shadows, no textures.

Feature icon sheet (3:2): same wrapper as above with
> Row 1 left to right: a gear; a wifi symbol made of three arcs and a dot; a Bluetooth rune symbol. Row 2 left to right: a magnifying glass over a flag; a crosshair target with a bullseye; a clipboard with a checkmark.

Replacement sheet (3:2), after the first sheet's flag/magnifier and Bluetooth glyphs came out cluttered:
> Icon sheet of exactly six separate flat pictogram icons in a 3 columns by 2 rows grid, equal size, generous equal spacing, each icon alone in its own cell, no cards, no borders, no labels, no text. Row 1 left to right: a document page with three horizontal lines and a magnifying glass over its lower corner; a list of three rows each with a round bullet and a line, with a magnifying glass; a single flag on a vertical pole. Row 2 left to right: three different clean Bluetooth rune symbols, each a single seafoam stroke with one small amber dot on each side. Bold thick strokes, simple geometric shapes, high contrast, readable at 24 pixels. No gradients, no shadows, no textures.

## Conversion notes

- The 48 px logo and 24 px Oyster AI icon are colour-reduced (16 colours) and a little grainy; the eyes stay readable.
- The Oyster AI icon is purple while the other 11 icons use the teal, amber and white of the design tokens.
- The splash keeps its own mid-tones; only its background is the exact `#101923`.
