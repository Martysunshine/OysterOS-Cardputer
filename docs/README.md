[OysterOS](../README.md) / Docs

# Documentation

Everything written about OysterOS in one place: how to set it up, how it works, what each feature does and
why it was built that way.

> **[Open the live emulator](emulator/index.html)** — the device UI running in a browser.

## Start here

| Page | What it covers |
|---|---|
| [Setup on Windows](SETUP_WINDOWS.md) | The full walkthrough in PowerShell: tools, backup, build and flash, microSD card, backend, AI provider, provisioning, first connection, simulation and the lab demonstration |
| [Troubleshooting](TROUBLESHOOTING.md) | Fixes for certificate errors, COM ports and drivers, download mode, restoring a backup, the firewall, Bluetooth checks and AI provider errors |

## How it works

| Page | What it covers |
|---|---|
| [Architecture](ARCHITECTURE.md) | Components, execution modes, firmware tasks, backend, trust boundaries, evidence, contracts and decisions |
| [Pocket core](POCKET_CORE.md) | Implementation overview of the Security and Oyster AI modules: capabilities, handheld screens, quick start, execution modes and secrets |
| [Backend API](API.md) | The API contract (v1): the two listeners, authentication, endpoints, job lifecycle and AI run lifecycle |
| [Companion API](COMPANION_API.md) | Pairing and read-only host health: endpoints, pairing codes, audit log, storage and host adapters |
| [Provisioning](PROVISIONING.md) | The USB serial provisioning protocol (v1): framing, commands and rules |
| [Privacy](PRIVACY.md) | What the AI provider receives, what is masked by default and what is never sent, including voice and web access |

## Features

| Page | What it covers |
|---|---|
| [Control Deck](CONTROL_DECK.md) | The PC shortcut deck: profiles with slots 1–9, allowlisted typed actions, on-device confirmation, the job state machine, idempotency, audit and history |
| [Smart Home](HOME_ASSISTANT.md) | The IR remote and the Home Assistant bridge: screens, backend setup, the allowlist and the API |
| [Coding-assistant approvals](CLAUDE_CODE_LINK.md) | Approve or deny an AI coding assistant's permission requests from the device |
| [Scripts](SCRIPTS.md) | On-device Python scripting on the Red Team tile: the sandboxed interpreter, the `Oyster` module and how to extend it |
| [Voice questions](VOICE_INPUT.md) | Push-to-talk questions: capture, streaming and transcription, what is sent where, formats and limits |
| [Web access](WEB_ACCESS.md) | The optional `web_search` and `web_fetch` tools for Oyster AI: how to enable them, what is sent where and the limits enforced in code |

## Hardware and flashing

| Page | What it covers |
|---|---|
| [Dual boot and restore](DUAL_BOOT.md) | The original firmware: the two-slot flash layout, the boot selector and restoring the factory image from the backup |
| [Build matrix](BUILD_MATRIX.md) | Every pinned version behind a build: platform, toolchain, libraries, partition table, binary size and Python packages |
| [Hardware checklist](HARDWARE_VERIFICATION.md) | The checks to run on a Cardputer ADV, how to run each one and where its result is recorded |
| [Pre-flash audit template](HARDWARE_AUDIT_TEMPLATE.md) | A form for recording the installed firmware, its apps and the recovery path before the first flash |

## Interface

| Page | What it covers |
|---|---|
| [Design system](DESIGN_SYSTEM.md) | Colour tokens, spacing and type, navigation rules, required components and the UI test checklist for the 240 × 135 display |
| [Screen map](UI_SCREEN_MAP.md) | The navigation tree, every screen with its design ID, the status LED colours and the key bindings |
| [UI simulator](SIMULATOR.md) | The desktop simulator: running it, profiles, routes, states, the navigation contract and key-script replay |

## Lab

| Page | What it covers |
|---|---|
| [BLE lab profile](BLE_LAB_PROFILE.md) | The synthetic lab peripheral `oyster-lab-v1`: characteristics, assertions, the application-authorisation and diagnostic transfer protocols, and how results are classified |

## Design record

| Page | What it covers |
|---|---|
| [Project plan](OysterOS_Project_Plan.md) | The original engineering plan: constraints, visual language, keyboard mapping, module wiring, companion service design, milestones and acceptance tests |

## Decisions

Architecture decision records live in [`adr/`](adr/).

| Record | Title |
|---|---|
| [ADR-001](adr/ADR-001-firmware-toolchain.md) | Firmware toolchain and build |
| [ADR-002](adr/ADR-002-ai-provider.md) | AI provider adapter |
| [ADR-003](adr/ADR-003-transport-tls.md) | Transport, TLS and time bootstrap |
| [ADR-004](adr/ADR-004-persistence.md) | Backend persistence and admin surface |
| [ADR-005](adr/ADR-005-local-confirmation.md) | Local confirmation gate |
| [ADR-006](adr/ADR-006-lab-profile.md) | Lab profile and diagnostic transfer |
| [ADR-007](adr/ADR-007-fixture-auth-state.md) | Fixture authorization state and capability limits |
| [ADR-008](adr/ADR-008-native-winrt-fixture.md) | Live GATT fixture uses the native WinRT API, not Bless |
| [ADR-009](adr/ADR-009-dual-boot-original-firmware.md) | Dual boot with the original firmware (two OTA slots) |
| [ADR-010](adr/ADR-010-web-access.md) | Web access for the assistant (web_search, web_fetch) |

## Folder guides

Each main folder has its own short guide: [firmware](../firmware/README.md) ·
[backend](../backend/README.md) · [lab](../lab/README.md) · [scripts](../scripts/README.md) ·
[tests](../tests/README.md) · [schemas](../schemas/README.md) · [simulator](../simulator/README.md).
