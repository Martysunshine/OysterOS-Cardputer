[OysterOS](../README.md) / firmware

# Firmware

The PlatformIO project that runs on the M5Stack Cardputer ADV (ESP32-S3, 8 MB flash): C++17 on the Arduino
framework. It draws the 240 × 135 interface on the UI task, runs Wi-Fi, Bluetooth LE, IR and scripts on a
worker task beside it, keeps evidence on the microSD card, and talks TLS to the companion backend and
directly to the AI provider.

## What is where

| Path | What it holds |
|---|---|
| [`src/`](src/) | `main.cpp` (boot order), `app_controller.cpp` (screen state machine and navigation), `ui/widgets.cpp` (the UI toolkit, painted through one 240 × 16 band sprite), and the radio, TLS, evidence, provisioning and voice code |
| [`src/modules/`](src/modules/) | The module framework plus one file per module: Control Deck, Smart Home, IR remote, Scripts, Scratchpad, Original Apps, Hosts, Wi-Fi recon and Wi-Fi setup |
| [`include/`](include/) | Headers for `src/`, the design tokens in `ui_theme.h` and the pin map in `oyster_pins.h` |
| [`lib/oyster_core/`](lib/oyster_core/) | Portable logic with no Arduino headers (local policy, JSON writer and parsers, SHA-256, transfer state machine, input map, calculator, voice codec), shared by the device build and the host tests |
| [`lib/pikascript/`](lib/pikascript/) | The vendored PikaPython interpreter (MIT) with the generated bindings for the `Oyster` script module |
| [`test/`](test/) | Unity host tests run in the `native` environment: eleven suites, 426 cases |
| [`platformio.ini`](platformio.ini) | The pinned platform and libraries, and the three environments below |
| [`partitions_8mb_noota.csv`](partitions_8mb_noota.csv) | Default flash layout: one 5 MiB `factory` app slot |
| [`partitions_8mb_dualboot.csv`](partitions_8mb_dualboot.csv) | Two-slot layout: OysterOS in `ota_0`, the original firmware image in `ota_1` |

| Environment | Purpose |
|---|---|
| `cardputer-adv` | The device build (default): `espressif32@6.13.0`, Arduino framework, single app slot |
| `cardputer-adv-dualboot` | The same build on the two-slot partition table |
| `native` | Host-side unit tests of the portable core, with Unity |

## Run it

Build (no device needed):

```powershell
.\scripts\setup.ps1                        # once: Python environment, PlatformIO, pinned platform and libraries
.\scripts\firmware-build.ps1               # compile, link, report size and SHA-256 of firmware.bin
.\scripts\firmware-build.ps1 -Clean        # the same from a clean build directory
```

Host unit tests:

```powershell
.\scripts\setup.ps1 -InstallWinLibs        # once: the host GCC used by the native environment
. .\scripts\_env.ps1                       # puts pio and the host g++ on PATH for this window
cd firmware
pio test -e native                         # every suite
pio test -e native -f test_calc            # one suite
```

On the device:

```powershell
.\scripts\firmware-backup.ps1 -Port COM5   # full 8 MB backup of the chip; the flash script requires it
.\scripts\firmware-flash.ps1 -Port COM5    # build and upload over USB-C
.\scripts\firmware-monitor.ps1 -Port COM5  # serial log at 115200 baud; Ctrl+C exits
```

## Read next

- [Architecture](../docs/ARCHITECTURE.md) — firmware modules, radio phases, evidence and trust boundaries
- [Build matrix](../docs/BUILD_MATRIX.md) — every pinned version, the partition table and binary size
- [Screen map](../docs/UI_SCREEN_MAP.md) — the navigation tree, screens and keys
- [Dual boot and restore](../docs/DUAL_BOOT.md) — the two-slot layout and the original firmware
