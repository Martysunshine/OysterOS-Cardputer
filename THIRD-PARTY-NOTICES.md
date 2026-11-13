# Third-party notices

OysterOS's own code — firmware, backend, simulator, emulator, lab tools, scripts and tests — is released under
the MIT licence in [`LICENSE`](LICENSE), Copyright (c) 2026 martin.matysek.

This file lists what is **not** covered by that licence: the project's name and artwork, and the third-party
components the project uses, each under its own licence.

## Name, mascot and artwork

- The name **OysterOS** and the oyster-and-pearl mascot (`assets/mascot/`, and every image derived from it,
  including the pixel art embedded in the firmware and the emulator) are Copyright (c) 2026 martin.matysek.
  You may use and redistribute them **unmodified** as part of this project and of forks of it, with
  attribution. They may not be used to brand other products or to suggest endorsement.
- The other images in `assets/` are AI-generated (icons, loader motion, background plates, wordmark and
  emblems). They are provided as they are, with no claim beyond what the law recognises, and their use is
  subject to the usage terms of the models that produced them. [`assets/PROVENANCE.md`](assets/PROVENANCE.md)
  records the model, date and prompt of each one.

## Third-party components

| Component | Licence | Where it is used |
|---|---|---|
| M5Cardputer, M5Unified, M5GFX (M5Stack) | MIT | firmware |
| NimBLE-Arduino (h2zero) | Apache-2.0 | firmware |
| ArduinoJson (Benoit Blanchon) | MIT | firmware |
| IRremote | MIT | firmware |
| Arduino-ESP32 core / ESP-IDF (Espressif) | LGPL-2.1 / Apache-2.0 | firmware toolchain |
| PikaPython / PikaScript (lyon, Gorgon Meducer and contributors; one adapter header also names Paul Sokolovsky and Damien P. George) | MIT; the upstream licence text is [`firmware/lib/pikascript/LICENSE`](firmware/lib/pikascript/LICENSE) | firmware (`firmware/lib/pikascript`, the vendored interpreter behind the Red Team tile) |
| Unity test framework (ThrowTheSwitch) | MIT | firmware host tests |
| FastAPI, Pydantic, httpx, uvicorn, jsonschema, cryptography | MIT / BSD / Apache-2.0 | backend |
| certifi | MPL-2.0 | backend (CA bundle) |
| pyserial | BSD-3-Clause | backend (USB serial provisioning) |
| truststore | MIT | backend (operating-system trust store) |
| psutil (Giampaolo Rodola) | BSD-3-Clause | backend (local host health adapter, Windows executor) |
| Bless, Bleak | MIT | lab fixture |
| esptool (Espressif, bundled with PlatformIO as tool-esptoolpy) | GPL-2.0-or-later | run by `scripts/firmware-backup.ps1`; not distributed here |
| Pillow | HPND | `simulator/`, `scripts/gen_assets.py`, `scripts/gen_showcase.py` (development tools only) |
| M5GFX `AsciiFont8x16` table and shape algorithms (M5Stack) | MIT, Copyright (c) 2021 M5Stack | `simulator/` and `docs/emulator/` (glyph tables extracted so that PC and browser renders match the device) |
| Adafruit GFX 5x7 font (`Font0`, Adafruit Industries) | BSD, Copyright (c) 2012 Adafruit Industries | `simulator/` and `docs/emulator/` (same reason) |

The full MIT text of M5GFX and the BSD terms of the Adafruit font are kept in
[`simulator/README.md`](simulator/README.md) (section "Licence attribution for the extracted data") and in a
comment at the top of the emulator page.

## Firmware binaries

This repository publishes source code only. A firmware binary built from it links the Arduino-ESP32 core
(LGPL-2.1) and Apache-2.0 components listed above; whoever distributes such a binary is responsible for
carrying those notices with it.
