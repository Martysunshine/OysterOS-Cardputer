# OysterOS UI simulator

A pixel-sized (240 x 135) desktop simulator of the OysterOS user interface, with a keyboard emulator, mock data and
automated navigation tests. It needs **no Cardputer, no backend and no network**, and it never flashes or talks to a
device. Design document: [docs/SIMULATOR.md](../docs/SIMULATOR.md). Issue: #2.

**What it is not.** It is not a copy of the firmware: it re-implements the firmware's widgets and navigation in Python,
reading the design tokens (`firmware/include/ui_theme.h`), the 24 x 24 icons (`firmware/src/assets_gen.cpp`) and the
glyph tables of the pinned M5GFX library from the repository, and tests check it against the firmware sources (screen
map, Home tiles, "Not available" texts, key table). **Nothing in it has been compared with a real display.** Pixel
parity with the device, the panel's colour rendering and every physical key binding are **UNVERIFIED** until photographs
and a key-tester run from a Cardputer ADV exist (see `docs/HARDWARE_VERIFICATION.md`).

## Run

Requirements: Python 3.12+, [Pillow](https://pypi.org/project/Pillow/) (`requirements.txt`) and tkinter for the window
(part of the standard Windows Python). The repo venv already has Pillow.

```powershell
.\scripts\ui-sim.ps1                                   # interactive window, scale 4
.\scripts\ui-sim.ps1 -Preview                          # + mock screens for Linux / Control Deck / Smart Home
.\scripts\ui-sim.ps1 -Headless -Out .\out\ui           # every route x state as PNG + contact sheets
.\scripts\ui-sim.ps1 -Script .\simulator\examples\navigation.keys.txt -Out .\out\nav
.\scripts\ui-sim.ps1 -Test                             # tests\simulator
```

Without the launcher (from the `simulator` folder): `python -m oyster_ui_sim [--headless --out DIR] [--script FILE
--out DIR] [--preview] [--scale N] [--routes] [--keys]`.

Keys in the window: the Cardputer keys as proposed by the firmware (`; . , /` move, `Enter` selects, `` ` `` or `Del` is
Back, `?` is Help, **Alt is Fn**: `Alt`+`Del` is Home, `Alt`+`` ` `` is Cancel, `Ctrl`+`C` is Cancel); desktop
shortcuts: arrow keys, `Esc`, `Backspace`, `Home`, `F1`. The side panel shows the legend, buttons for the logical
actions, the simulated connectivity, the state variant of the current screen, a route jump list and switches for
"scans find nothing" and "BLE did not start".

## Layout

| Path | Content |
|---|---|
| `oyster_ui_sim/theme.py` | parses `ui_theme.h` (colours, geometry, type sizes); fails loudly |
| `oyster_ui_sim/glyphs.py` | **generated** glyph bitmaps, extracted from M5GFX (see below) |
| `oyster_ui_sim/canvas.py`, `widgets.py` | 240 x 16 band sprite, shape and text primitives, port of `src/ui/widgets.cpp` |
| `oyster_ui_sim/routes.py` | the route table (id, parent, kind, module, availability, Enter targets, job, states) |
| `oyster_ui_sim/views.py`, `engine.py` | what each route shows per state; stack, modal layer, tasks, simulation clock |
| `oyster_ui_sim/keys.py` | port of `input_map.cpp`, logical actions, key table, chord parser |
| `oyster_ui_sim/fixtures.py` | mock data (Wi-Fi, BLE, hosts, macros, rooms, AI answers) |
| `oyster_ui_sim/script.py`, `render.py`, `tk_app.py`, `__main__.py` | replay, PNG export, window, command line |
| `tools/gen_glyphs.py` | regenerates `glyphs.py` from the PlatformIO library cache (`--check` verifies it) |
| `examples/navigation.keys.txt` | a replayable key script with checks |

## Glyphs: extracted from M5GFX

The text uses the two bitmap fonts of the firmware (`ui_theme.h`: `AsciiFont8x16` for body, headings and key hints,
`Font0` 6 x 8 for the status bar and notices). The bitmaps are **extracted from M5GFX 0.2.34** (the version pinned by
the firmware, `firmware/.pio/libdeps/cardputer-adv/M5GFX`) by `tools/gen_glyphs.py`; the generated module is committed
so the simulator runs without the PlatformIO cache. Cell metrics and glyph shapes are those of the library; how the real
panel renders them is still unverified. Circles and rounded rectangles are ports of the M5GFX algorithms.

Colours are the RGB565 constants of the firmware expanded to 8 bits by bit replication, so the on-screen panel colour is
`#182C39` where the design says `#192C3D`: that is the quantisation the firmware itself applies. The panel's real colour
response is unverified.

### Licence attribution for the extracted data

(To be added to `LICENSE-NOTICES.md`.)

- `fonts::AsciiFont8x16` (`src/lgfx/Fonts/Ascii8x16.h`, table `FontLib8x16`, file header "ASC16") and the M5GFX shape
  algorithms: M5GFX, Copyright (c) 2021 M5Stack, MIT licence. The table file carries no separate licence text.
- `fonts::Font0` (`src/lgfx/Fonts/glcdfont.h`): the classic 5 x 7 font of the Adafruit GFX Library, Copyright (c) 2012
  Adafruit Industries, BSD licence.

M5GFX licence:

```text
MIT License

Copyright (c) 2021 M5Stack

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Adafruit GFX Library licence (header of `glcdfont.h`):

```text
Software License Agreement (BSD License)

Copyright (c) 2012 Adafruit Industries.  All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.
- Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

(The Adafruit text is quoted from the header as shipped in the pinned M5GFX source; the authoritative text is that file.)
