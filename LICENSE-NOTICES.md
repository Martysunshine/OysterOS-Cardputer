# License notices

This repository does not yet declare a license for its original code.

Art in `assets/`: the mascot is the project's own artwork; the other pieces are AI-generated. See `assets/PROVENANCE.md`, which records the model, date and prompts.

Third-party components used (each under its own license; see the respective projects):

| Component | License | Where |
|---|---|---|
| M5Cardputer, M5Unified, M5GFX (M5Stack) | MIT | firmware |
| NimBLE-Arduino (h2zero) | Apache-2.0 | firmware |
| ArduinoJson (Benoit Blanchon) | MIT | firmware |
| Arduino-ESP32 core / ESP-IDF (Espressif) | LGPL-2.1 / Apache-2.0 | firmware toolchain |
| FastAPI, Pydantic, httpx, uvicorn, jsonschema, cryptography | MIT / BSD / Apache-2.0 | backend |
| certifi | MPL-2.0 | backend (CA bundle) |
| pyserial | BSD-3-Clause | backend (USB serial provisioning) |
| truststore | MIT | backend (OS trust store) |
| Unity test framework (ThrowTheSwitch) | MIT | firmware host tests |
| IRremote | MIT | firmware |
| Bless, Bleak | MIT | lab fixture |
| esptool (Espressif, bundled with PlatformIO as tool-esptoolpy) | GPL-2.0-or-later | run by `scripts/firmware-backup.ps1`; not distributed here |
| Pillow | HPND | `simulator/` and `scripts/gen_assets.py` (development tools only; not part of the firmware) |
| M5GFX `AsciiFont8x16` table and shape algorithms (M5Stack) | MIT, Copyright (c) 2021 M5Stack | `simulator/` (glyph tables extracted so that PC renders match the device) |
| Adafruit GFX 5x7 font (`Font0`, Adafruit Industries) | BSD, Copyright (c) 2012 Adafruit Industries | `simulator/` (same reason) |

The full MIT text of M5GFX and the BSD terms of the Adafruit font are kept in `simulator/README.md`
(section "Licence attribution for the extracted data").
