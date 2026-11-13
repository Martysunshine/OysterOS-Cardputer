[OysterOS](../README.md) / scripts

# Scripts

The entry points for everything you do with the repository on Windows: setup, checks, building and flashing,
running the backend and the simulators, and regenerating derived files. The PowerShell scripts work in
PowerShell 7 and Windows PowerShell 5.1 and are run from the repository root. Every script opens with a
header comment that lists its steps, switches and examples.

## What is where

| Group | Script | What it does |
|---|---|---|
| **Setup and checks** | [`setup.ps1`](setup.ps1) | Creates `.venv`, installs the backend, lab and PlatformIO packages pinned by `constraints.txt`, builds the CA bundle, creates `backend\.env` and downloads the pinned firmware platform; `-InstallWinLibs` adds the host GCC |
| | [`check.ps1`](check.ps1) | Runs ruff, pytest, schema validation, the lab-profile header check, the firmware host tests and the firmware build, and reports each as PASS, FAIL or SKIPPED |
| | [`_env.ps1`](_env.ps1) | The shared environment every script dot-sources: paths, CA-bundle variables, the venv and the host g++ on `PATH` |
| | [`make_ca_bundle.py`](make_ca_bundle.py) | Builds the CA bundle from certifi plus the Windows certificate stores, so tools trust what the OS trusts |
| **Firmware** | [`firmware-build.ps1`](firmware-build.ps1) | Builds the `cardputer-adv` environment and prints the size and SHA-256 of `firmware.bin` |
| | [`firmware-backup.ps1`](firmware-backup.ps1) | Reads the complete 8 MB flash into `_backups\<mac>\<stamp>\` and checks it against the chip |
| | [`firmware-flash.ps1`](firmware-flash.ps1) | Builds and uploads over USB-C once a verified backup of the connected chip exists; `-Env` picks `cardputer-adv` or `cardputer-adv-dualboot` |
| | [`firmware-monitor.ps1`](firmware-monitor.ps1) | Opens the USB serial log at 115200 baud with the ESP32 exception decoder |
| | [`original-restore.ps1`](original-restore.ps1) | Writes the full backup back to the chip so the device boots its original firmware |
| | [`dualboot-setup.ps1`](dualboot-setup.ps1) | Cuts the original app out of the backup and, with `-Apply`, writes it to the `ota_1` slot of the two-slot layout |
| | [`oyster-return.ps1`](oyster-return.ps1) | Rewrites the boot selector so a device on the two-slot layout starts OysterOS from `ota_0` |
| **Backend and provisioning** | [`backend.ps1`](backend.ps1) | Runs `oyster init-db` and `oyster serve` in the current window; `-MakeDevCaIp` first creates the dev CA and server certificate |
| | [`provision.ps1`](provision.ps1) | Wraps `oyster provision-device`: device id, backend address, Wi-Fi, token, CA certificate, clock and enrolled targets over USB serial |
| | [`oyster-up.ps1`](oyster-up.ps1) | One command for the whole bring-up: LAN IP, certificate, backend in the background, firewall check, provisioning and demo data; `-Stop` stops the backend |
| **Simulators** | [`ui-sim.ps1`](ui-sim.ps1) | The 240 × 135 UI simulator: interactive window, headless PNG export, key-script replay and its tests |
| | [`sim.ps1`](sim.ps1) | Runs the device simulator (`oyster-sim`) against the local backend with a chosen scenario |
| | [`fixture.ps1`](fixture.ps1) | The live BLE lab fixture: `check`, `run -Mode vulnerable` or `corrected`, `reset-state`, `synth` |
| | [`demo-m1.ps1`](demo-m1.ps1) | End-to-end demonstration: a real AI model plans an operation, the simulated device executes it and a Markdown report is exported |
| **Generators** | [`gen_assets.py`](gen_assets.py) | Generates `firmware/src/assets_gen.cpp`, `assets/preview.png` and `assets/banner.png` from the art in `assets/` |
| | [`gen_emulator.py`](gen_emulator.py) | Builds the browser emulator `docs/emulator/index.html` from `docs/emulator/src` and the firmware's own fonts and icons |
| | [`gen_lab_profile_header.py`](gen_lab_profile_header.py) | Generates the firmware header `lab_profile_gen.h` from `schemas/lab-profile.json` |
| | [`gen_showcase.py`](gen_showcase.py) | Generates the showcase artwork in `assets/brand/` and its landing-page copies in `docs/site/` |

## Run it

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # once per window, if PowerShell blocks scripts
.\scripts\setup.ps1                                          # tools and Python environment
.\scripts\check.ps1                                          # every automated check, no device needed
.\scripts\ui-sim.ps1                                         # click through every screen on the PC
```

With a Cardputer on `COM5`:

```powershell
.\scripts\firmware-backup.ps1 -Port COM5                     # full 8 MB backup of the chip
.\scripts\firmware-flash.ps1  -Port COM5                     # build and upload
.\scripts\oyster-up.ps1                                      # certificate, backend, provisioning
.\scripts\original-restore.ps1 -Port COM5                    # back to the original firmware at any time
```

The generators, after changing their inputs:

```powershell
. .\scripts\_env.ps1                                         # puts the venv python on PATH for this window
python scripts/gen_assets.py
python scripts/gen_emulator.py
python scripts/gen_lab_profile_header.py
python scripts/gen_showcase.py
```

## Read next

- [Setup on Windows](../docs/SETUP_WINDOWS.md) — the scripts in the order a first run uses them
- [Troubleshooting](../docs/TROUBLESHOOTING.md) — certificates, COM ports, download mode, restoring a backup
- [Dual boot and restore](../docs/DUAL_BOOT.md) — what the backup, restore and two-slot scripts do to the flash
- [Provisioning](../docs/PROVISIONING.md) — the serial protocol behind `provision.ps1`
