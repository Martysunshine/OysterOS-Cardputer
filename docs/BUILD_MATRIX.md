# Build matrix

Every version that a build depends on, pinned. Do not use floating versions or branches in release builds
Any dependency change is recorded here in the same change.
Decision record: [ADR-001](adr/ADR-001-firmware-toolchain.md).

Status: these pins come from `firmware/platformio.ini` and `constraints.txt`. The "Resolved" column was read
from the installed package metadata (`~/.platformio/packages/*/package.json`,
`firmware/.pio/libdeps/cardputer-adv/*/.piopm`) on 2026-10-24. **Hardware bring-up with this combination has
not happened yet** (no Cardputer attached).

## Firmware: device build (`env:cardputer-adv`)

| Component | Pinned as | Resolved |
| --- | --- | --- |
| PlatformIO platform | `espressif32@6.13.0` | 6.13.0 |
| Arduino-ESP32 core | via platform | 2.0.17 (`framework-arduinoespressif32` 3.20017.241212+sha.dcc1105b) |
| ESP-IDF (inside the core) | via core | 4.4.7 |
| Xtensa toolchain | via platform | `toolchain-xtensa-esp32s3` 8.4.0+2021r2-patch5 (GCC 8.4.0) |
| RISC-V toolchain (ULP, installed by the platform) | via platform | `toolchain-riscv32-esp` 8.4.0+2021r2-patch5 |
| esptool | via platform | `tool-esptoolpy` 2.41100.260830 |
| SCons | via PlatformIO | `tool-scons` 4.41101.0 |
| M5Cardputer | `m5stack/M5Cardputer@1.1.1` | 1.1.1 |
| M5Unified | `m5stack/M5Unified@0.2.27` | 0.2.27 (declares M5GFX >= 0.2.31) |
| M5GFX | `m5stack/M5GFX@0.2.34` | 0.2.34 |
| NimBLE-Arduino | `h2zero/NimBLE-Arduino@2.5.1` | 2.5.1 (2.x API) |
| ArduinoJson | `bblanchon/ArduinoJson@7.4.3` | 7.4.3 |
| IRremote | `z3t0/IRremote@4.7.1` (a transitive dependency of M5Cardputer, which declares `IRremote: *`; pinned here) | 4.7.1 |

> **Note:** IRremote comes in through M5Cardputer's floating `*` dependency, so it is pinned explicitly in
> `lib_deps` to keep the installed package set reproducible. In a build on 2026-10-24 it was installed but did
> **not** appear in the dependency graph (LDF chain mode), so it did not end up in that binary. Since the Smart
> Home module (`firmware/src/modules/ir_remote.cpp`, the only file that includes it, with the receiver code
> compiled out) it is used and appears in the link map of the module build. ArduinoJson is also used by the
> firmware now: the response parsers of the Hosts, Control Deck and Smart Home modules.

Board and build settings (`firmware/platformio.ini`):

| Setting | Value |
| --- | --- |
| `board` | `esp32-s3-devkitc-1` (the recipe M5Stack documents for the Cardputer ADV) |
| Flash | 8 MB (`board_build.flash_size`, `board_upload.flash_size`) |
| PSRAM | none (ESP32-S3FN8); not enabled |
| Filesystem | LittleFS (`board_build.filesystem = littlefs`) in the `spiffs` partition |
| C++ standard | `-std=gnu++17` (default gnu++11 removed) |
| USB serial | `-DARDUINO_USB_CDC_ON_BOOT=1 -DARDUINO_USB_MODE=1` (USB-C is the native USB-Serial-JTAG) |
| Log level | `-DCORE_DEBUG_LEVEL=2` (warnings) |
| ArduinoJson | `ARDUINOJSON_USE_DOUBLE=0`, `ARDUINOJSON_USE_LONG_LONG=1` |
| NimBLE roles | central + observer only; peripheral and broadcaster off; 1 connection; 4 bonds; 8 CCCDs; C++ wrapper log level 1 |
| Firmware version | `OYSTER_FIRMWARE_VERSION="0.1.0"` |
| Monitor | 115200 baud, `esp32_exception_decoder` |
| Upload speed | 921600 |

## Firmware: host unit tests (`env:native`)

| Component | Version |
| --- | --- |
| PlatformIO `native` platform | 1.2.1 |
| Test framework | Unity 2.6.1 |
| Host compiler | GCC 16.2.0 from WinLibs (`x86_64-ucrt-posix-seh`, build r3), winget id `BrechtSanders.WinLibs.POSIX.UCRT`, installed with `--source winget` |
| Flags | `-std=gnu++17 -DOYSTER_HOST_TEST -Wall -Wextra`; M5/NimBLE/ArduinoJson ignored (`lib_ignore`) |

`scripts\_env.ps1` puts the WinLibs `mingw64\bin` folder on `PATH` when it finds it under
`%LOCALAPPDATA%\Microsoft\WinGet\Packages` (or `OYSTER_GCC_BIN`).

## Flash partition table (`firmware/partitions_8mb_noota.csv`)

Single factory app, no OTA (v1). The bootloader sits at `0x0` and the partition table at `0x8000`
(ESP32-S3 defaults).

| Name | Type / subtype | Offset | Size | Use |
| --- | --- | --- | --- | --- |
| `nvs` | data / nvs | `0x9000` | `0x5000` (20 KiB) | settings, device token, Wi-Fi credentials, bonds |
| `otadata` | data / ota | `0xE000` | `0x2000` (8 KiB) | written by every upload (`boot_app0.bin`); selects nothing without OTA slots |
| `factory` | app / factory | `0x10000` | `0x500000` (5 MiB) | the firmware image |
| `spiffs` | data / spiffs | `0x510000` | `0x2E0000` (2944 KiB) | LittleFS: backend CA (`/ca/backend_ca.pem`) |
| `coredump` | data / coredump | `0x7F0000` | `0x10000` (64 KiB) | crash dumps |

End of the last partition: `0x800000` = 8 MiB.

## Binary size

Measured in clean builds of `env:cardputer-adv` on a Windows host on 2026-10-25 (PlatformIO 6.2.0, no compiler
warnings). These are **build measurements, not hardware results**: nothing has run on a device.

With the Hosts, Control Deck and Smart Home modules (and the module framework) the firmware is **about** 33.2 % of the
5 MiB factory partition (flash) and **about** 42.6 % static RAM. The module state is static: about 5.8 KB of `.bss`
for Hosts, about 4.7 KB for the Control Deck and about 3.0 KB for Smart Home. The detailed rows below are from the
earlier clean build, before those modules; run `scripts/firmware-build.ps1` for the exact current numbers and hash.

| Item (earlier clean build, before the modules) | Value |
| --- | --- |
| `firmware.bin` size | 1673616 bytes |
| Share of the 5 MiB factory partition | 31.9% (1,673,257 bytes of 5,242,880 reported by PlatformIO) |
| Static RAM (`.data` + `.bss`) | 38.1%: 124,952 of 327,680 bytes |
| Heap left at boot, minimum free heap, largest free block | **Not measured.** The project goals (>= 64 KiB / >= 32 KiB) are checked from the on-device telemetry; see HARDWARE_VERIFICATION.md |
| `firmware.bin` SHA-256 (of that earlier build; the module build has another) | `edf4f78fbbff17b1c8298b79a01fd6fc9eefb07a3187020dc9d804ec402f36a9` |
| Host tests (`pio test -e native`) | 173 cases pass in the core and input suites (core logic, delivery/ledger rules, identity, paging, input mapping incl. Home/Help); the modules add their own suites: `test_hosts` 28 cases, `test_deck` 35, `test_smarthome` 24 |
| Backend and tool tests (`pytest tests`) | 1958 passed, 3 skipped at the time of writing; among them `tests/deck` 486, `tests/home` 170 and `tests/simulator` 165 |

The SHA-256 changes with any source change; `scripts/firmware-build.ps1` prints the current one.
The assessment worker task uses a 16 KiB stack (its TLS handshake shares it; the 9.5 KB discovery result no longer lives on it:
`op_ble_discover`'s compiled frame went from 11,728 to 128 bytes, a static estimate, not a runtime measurement);
shrink it only after reading the real stack high-water mark from telemetry on hardware.

## Backend, lab and tools (Python)

| Component | Version |
| --- | --- |
| Python | 3.13.5 (CPython, Windows x64); `requires-python >= 3.12` |
| PlatformIO Core | 6.2.0 |
| All Python packages | pinned in [`constraints.txt`](../constraints.txt) (installed with `pip install -c constraints.txt ...` by `scripts\setup.ps1`) |
| Key packages | fastapi 0.162.0, starlette 1.10.0, pydantic 2.14.1, httpx 0.28.1, uvicorn 0.54.0, jsonschema 4.26.0, cryptography 50.0.2, python-dotenv 1.2.4, pyserial 3.5, pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.17.2, bleak 3.0.2, winrt-* 4!10.0.28000.2705 (winrt-runtime 4.0.1), certifi 2026.10.23, pillow 13.0.0 (screen simulator and asset converter only), psutil 7.2.2 (local host adapter and Windows executor) |
| truststore | 0.10.5 (backend declares `truststore>=0.10`); pinned in `constraints.txt` (`truststore==0.10.5`) and by `scripts\setup.ps1` |

## How to reproduce

```powershell
.\scripts\setup.ps1             # venv + constrained packages + pio pkg install (pinned platform/libs)
.\scripts\firmware-build.ps1    # compile, size, SHA-256
.\scripts\check.ps1             # ruff, pytest, schema validation, header freshness, native tests, firmware build
```
