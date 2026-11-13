# Changelog

All notable changes to OysterOS are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - 2026-11-13

The first public release.

### On the device

- A Home screen with nine tiles, a lock screen with the animated mascot, a battery indicator and a status
  LED that shows the connection state in colour.
- **Security**: passive Wi-Fi survey with room labels, a live wardriving scan logged to the SD card, a
  Bluetooth LE explorer with GATT inspection of enrolled devices, saved findings.
- **Oyster AI**: questions answered over the device's own Wi-Fi with a key stored on the device, and through
  the companion's agent loop with typed tools; push-to-talk voice questions.
- **Red Team**: a sandboxed PikaPython interpreter with an `Oyster` module, a built-in example and scripts
  from the SD card.
- **Control**: a shortcut deck for the PC with confirmations, and approvals for an AI coding assistant's
  permission requests.
- **Smart Home**: an infrared remote for five TV families and Home Assistant entities behind an allowlist.
- **Original**: details of the factory firmware in the backup slot and the way back to it.
- **Settings** and **Help**: joining Wi-Fi from the keyboard, brightness, diagnostics, a key tester and the
  LED colour legend.
- **Scratchpad**: an offline calculator with a history tape and a quick notepad.

### Companion and tools

- A FastAPI backend with a device API, job lifecycle, evidence store, AI orchestrator, pairing and audit
  API, Control Deck executor, Home Assistant bridge, voice transcription and optional web search.
- Provisioning over USB serial and a one-command bring-up script.
- A 240 × 135 UI simulator with parity tests against the firmware, a simulated device and a Bluetooth LE
  lab fixture.
- A browser emulator built from the firmware's drawing code, fonts and icons.
- Scripts for setup, checks, building, flash backup, gated flashing and restoring the factory firmware.

### Project

- MIT licence, third-party notices, security policy, contribution guide and a documentation index.
- Showcase artwork, a project timeline and a landing page for the live emulator.

### History before the release

| Date | Milestone |
|---|---|
| 2026-10-24 | Design package: vision, UI wireframes, design system and engineering plan |
| 2026-10-25 | Contracts, backend, companion, simulators, firmware core and UI; first boot on the Cardputer ADV |
| 2026-10-26 | Status LED, voice, Wi-Fi join on the device, on-device recon, AI over the device's own Wi-Fi |
| 2026-10-27 | Scratchpad, on-device Python scripting, browser emulator |

[0.1.0]: https://github.com/Martysunshine/OysterOS-Cardputer/releases/tag/v0.1.0
