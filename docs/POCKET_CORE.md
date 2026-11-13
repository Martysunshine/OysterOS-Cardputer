[OysterOS](../README.md) / [Docs](README.md) / Pocket core

# Oyster Pocket: implementation overview

This page describes the working code in this repository: the firmware, backend and lab tools that
implement the Security and Oyster AI modules of OysterOS. The product overview is in the root
[`README.md`](../README.md); how the parts fit together is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Oyster Pocket is a handheld wireless assessment instrument built on **one M5Stack Cardputer ADV**
(ESP32-S3), with an AI backend on your own PC.

- Surveys **2.4 GHz** Wi-Fi networks (built-in radio only; 5 and 6 GHz networks are not visible to it).
- Explores the BLE GATT services of **your own enrolled test peripherals**. It never connects to
  devices you have not enrolled.
- Checks a peripheral's behaviour against its documented access policy, using a synthetic lab
  fixture that runs on your Windows PC.
- Stores evidence (on the microSD card and in the backend), and uses a real AI model (Groq, through
  an OpenAI-compatible API) to plan supported checks and explain the results with evidence references.

> **The firmware is a full-flash image.** Flashing it replaces the M5Stack factory firmware, the launcher
> and the original apps (custom partition table, no OTA). Back the device up first with
> `.\scripts\firmware-backup.ps1`; `.\scripts\firmware-flash.ps1` refuses to flash a chip without a verified
> backup. See [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md) step 0; restoring the original firmware is described in
> [`DUAL_BOOT.md`](DUAL_BOOT.md).

## What it does not do

These are outside the project's scope and are not implemented. The AI is not allowed to claim them:
Wi-Fi password cracking, raw packet sniffing, Bluetooth Classic (the ESP32-S3 supports only BLE),
connecting to non-enrolled devices, and generic phone or computer takeover. "Downloading" here means
transferring one documented synthetic object from the lab profile, not taking arbitrary files from a
nearby device.

## Supported hardware and hosts

| Item | Requirement |
| --- | --- |
| Handheld | **M5Stack Cardputer ADV** (Stamp-S3A, ESP32-S3FN8, 8 MB flash, no PSRAM). The original Cardputer is a different board and is not supported. |
| Cable | USB-C cable that carries **data** (many charge-only cables do not). |
| Storage | microSD card formatted FAT32 (optional; without it the device keeps a small volatile log and says so). |
| Backend PC | Windows 10/11, Python 3.12+ (`constraints.txt` was recorded with 3.13). Reachable from the Cardputer over a 2.4 GHz Wi-Fi LAN. |
| AI | A Groq API key that you supply (usage may be charged by the provider). |
| Live BLE fixture | A Windows PC whose Bluetooth adapter supports the LE **peripheral** role. Check it with `.\scripts\fixture.ps1 check`. |

A second Cardputer is never needed.

## Capabilities at a glance

| Area | Simulation route (PC only) | Real Cardputer firmware |
| --- | --- | --- |
| AI planning + explanation (real model) | real model, explicitly simulated results (`demo-m1.ps1`) | relays questions to the backend and shows the answer |
| Wi-Fi survey | simulated results | live radio (2.4 GHz) |
| BLE discovery, GATT inspection, profile reads | simulated results | live radio (inspection and reads on enrolled targets only) |
| Access-policy checks, reversible write, diagnostic transfer | simulated results | the device answers `unsupported`; these run on the simulation route only |
| Report export | backend | backend |

"Simulated" results check orchestration, policy evaluation and reporting. They say nothing about radios,
pairing or SD behaviour.

## The handheld screens

The firmware UI follows the project's design system ([`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md)). A boot splash is
drawn once at start-up, then the Home screen (heading `OYSTER OS`) shows a two-column tile grid. The number on
a tile is its shortcut key on the Home screen:

| Tile | Opens |
| --- | --- |
| 1 Security | Wi-Fi Survey, Bluetooth Explorer, Saved Findings, Enrolled Targets, Assess |
| 2 Oyster AI | Ask a question, Speak a question, Latest AI run, Cancel AI run, Sync evidence now |
| 3 Red Team | on-device Python scripts ([`SCRIPTS.md`](SCRIPTS.md)) |
| 4 Control | allowlisted PC actions with local confirmation ([`CONTROL_DECK.md`](CONTROL_DECK.md)) |
| 5 Smart Home | IR remote and Home Assistant ([`HOME_ASSISTANT.md`](HOME_ASSISTANT.md)) |
| 6 Original | what is stored of the original firmware and how to restore it from the PC ([`DUAL_BOOT.md`](DUAL_BOOT.md)) |
| 7 Settings | Wi-Fi, Backend, Brightness, Storage, Diagnostics, Sync, New session, Keyboard, Key tester, About |
| 8 Help | the key bindings and a hint for the screen it was opened from |
| 9 Scratchpad | offline calculator and notepad |

The mapping from the design's screen identifiers to firmware screens is in
[`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md), with the key bindings (Home is `Fn`+`Del`, Help is `?`);
**Settings > Key tester** shows what a key press resolves to.

## Quick start (Windows, PowerShell)

Full beginner walkthrough: [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md). In short, from the
repository root:

```powershell
# 1. Tools, venv, CA bundle, backend\.env, pinned firmware packages (no device needed)
.\scripts\setup.ps1                 # add -InstallWinLibs for the host C++ compiler used by unit tests

# 2. Automated checks: each step reports PASS / FAIL / SKIPPED
.\scripts\check.ps1

# 3. Backend (window 1) and the simulated device (window 2)
.\scripts\backend.ps1
.\scripts\sim.ps1 -Scenario success -AutoConfirm

# 4. M1 demo: real AI model + simulated device + exported report (needs AI_API_KEY and AI_MODEL)
.\scripts\demo-m1.ps1

# 5. Firmware (build needs no device; backup, flash, monitor and provision need the Cardputer on USB)
.\scripts\firmware-build.ps1
.\scripts\firmware-backup.ps1 -Port COM5      # whole 8 MB flash -> _backups\<mac>\<stamp>\; do this BEFORE the first flash
.\scripts\firmware-flash.ps1 -Port COM5       # refuses without a verified backup of that chip
.\scripts\firmware-monitor.ps1 -Port COM5
.\scripts\provision.ps1 -Port COM5 --help

# 6. Live BLE lab fixture on this PC
.\scripts\fixture.ps1 check
.\scripts\fixture.ps1 run -Mode vulnerable
```

If PowerShell refuses to run scripts, run this once in the same window:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

## Execution modes

The handheld's status bar always shows one mode banner, and every event and export carries
`source_mode`. Simulated data is never mixed with live data without a label on both.

Banner on the Cardputer (the first one that applies is shown):

- **LIVE RADIO**: a radio phase (Wi-Fi survey, BLE discovery or a BLE session) is active.
- **START FAILED**: a firmware module did not start at boot. Settings > Diagnostics lists which ones, and
  the actions that depend on them are refused with a message.
- **AI CONNECTED**: the latest AI run actually used a provider (status `running`, `awaiting_*`,
  `succeeded` or `budget_exhausted`). A reachable backend alone does not show this.
- **BACKEND OK**: the backend is reachable and the device is authenticated.
- **ONLINE**: Wi-Fi is connected but the backend is not reachable. With a provider key stored on the device,
  Oyster AI still answers a typed question directly (question and answer only).
- **OFFLINE**: none of the above. The device still surveys and stores evidence. The AI says it is
  unavailable rather than inventing an answer.

The firmware never shows `SIMULATED`: everything it records is live data. **SIMULATED** is the label of the
PC simulator (`oyster-sim`): every event it produces has `source_mode: simulated`, and so does every finding
and report built from it.

## Repository layout

| Path | Contents |
| --- | --- |
| `firmware/` | Cardputer application (PlatformIO, Arduino-ESP32 2.0.17), `lib/oyster_core` host-testable core, partitions |
| `backend/` | FastAPI backend: device API, job lifecycle, AI orchestration (Groq), policy evaluators, reports, CLI `oyster` |
| `lab/` | Device simulator `oyster-sim` and the live WinRT GATT fixture `oyster-fixture` |
| `schemas/` | JSON Schemas, the lab GATT profile (`lab-profile.json`) and valid examples |
| `tests/` | Contract, policy, provider, auth/job, recovery and lab tests (pytest) |
| `examples/` | Sanitized, clearly labelled simulated sessions and sample report |
| `scripts/` | Windows PowerShell scripts listed above, plus `make_ca_bundle.py`, `gen_lab_profile_header.py` and `gen_assets.py` (converts the art in `assets/` for the firmware) |
| `assets/` | Art for the device UI (the mascot, icons, boot splash and loader) and the README banner; all but the mascot is AI-generated; see [`PROVENANCE.md`](../assets/PROVENANCE.md) |
| `docs/` | Design package (plan, design system, audit template) and the implementation docs: UI screen map, setup, architecture, BLE profile, hardware checklist, troubleshooting, build matrix |
| `_backups/` | Created locally by `firmware-backup.ps1` (flash dumps of your device). Not part of the repository; never commit or share it |

## Documentation

| Document | Use it for |
| --- | --- |
| [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md) | Step-by-step setup, backup, flashing, provisioning, demo, recovery |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How the parts fit together |
| [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md) | The design's screen identifiers mapped to firmware screens, with navigation tree and keys |
| [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md) | Lab GATT profile: UUIDs, policies, assertions, transfer and app-auth protocols |
| [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) | Your checklist for the real device, with a results table |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Certificates, COM ports, download mode, restoring a backup, firewall, Bluetooth |
| [`PRIVACY.md`](PRIVACY.md) | What the AI provider receives, what is masked, what is never sent |
| [`BUILD_MATRIX.md`](BUILD_MATRIX.md) | Exact pinned toolchain and library versions, partition table |
| [`API.md`](API.md), [`PROVISIONING.md`](PROVISIONING.md) | HTTP API and serial provisioning contracts |
| [`adr/`](adr/) | Architecture decisions |

## Secrets

`backend\.env` (AI key, admin token, fixture credential), device tokens, Wi-Fi passwords, raw surveys
SD dumps and flash backups must never be committed (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)). The AI key
lives in `backend\.env` and, for direct mode, in the Cardputer's internal NVS storage; it is never written to
the SD card or logged.

The owner/admin API is served only on the loopback listener (`127.0.0.1:8000`). The LAN HTTPS listener
serves device routes only and answers 403 to the owner token. `oyster serve` refuses an
`OYSTER_ADMIN_TOKEN` shorter than 24 characters (or a placeholder).

What the AI provider receives (SSIDs are sent unmasked; BSSIDs and BLE addresses are masked by default):
[`PRIVACY.md`](PRIVACY.md).

## Licence

The original code is under the MIT licence, see the root [`LICENSE`](../LICENSE); third-party components are
listed in [`THIRD-PARTY-NOTICES.md`](../THIRD-PARTY-NOTICES.md). Contribution rules:
[`CONTRIBUTING.md`](../CONTRIBUTING.md).
