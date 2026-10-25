# Capability matrix

This page says what each route can honestly claim. Read it before trusting any result.

> **Nothing in this project has been verified on a Cardputer.** "Written" means the firmware code exists
> (and is compiled in this pass), not that it works on the device. Whether each automated check passes
> right now is what `.\scripts\check.ps1` reports. Hardware results go in
> [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md). How these capabilities map to the OysterOS design
> modules: [`INTEGRATION.md`](INTEGRATION.md).

## Legend

| Label | Meaning |
| --- | --- |
| **SIM** | Implemented by the simulator and backend on the PC. Results are always `source_mode: simulated`. This checks orchestration, policy evaluation, data handling and reports. It does **not** check radios, pairing, SD timing or real BLE permissions |
| **FW written, unverified** | Firmware code exists for the real Cardputer; no hardware observation yet |
| **Host-tested logic** | Platform-independent logic in `firmware/lib/oyster_core`, covered by `pio test -e native` on the PC. This is not a device result |
| **M4+ / not built** | Not implemented on the device. The firmware answers `unsupported` with a reason, never a placeholder success |
| **Out of scope** | Will not be built in v1. The AI must not claim it |

## 1. Device operations (AI tool catalogue)

A real Cardputer in this pass reports the capabilities `wifi_survey`, `ble_discover`, `ble_inspect_gatt`
and `ble_read_profile_value`. The simulator reports all eight. The AI is only offered tools that the
session's device reports. On the device, job limits (`max_results`, `max_duration_s`, `max_bytes`, deadline)
are enforced and recorded in a `job_limits` object in the summary event; a hit makes the result `Partial`
(`complete: false`).

| Operation | Simulator | Real Cardputer | Notes |
| --- | --- | --- | --- |
| `wifi_survey` | SIM | FW written, unverified | 2.4 GHz only. Passive scan first; the scan mode is recorded. WPS/PMF recorded as `unknown`. RSSI is not distance |
| `ble_discover` | SIM | FW written, unverified | Bounded; never connects. Random addresses can change; identity is not inferred from a MAC or a name |
| `ble_inspect_gatt` | SIM | FW written, unverified | Enrolled targets only, one connection at a time. Fail-closed identity check first (enrolled address, else a single UUID/name candidate; then an exact public `device_info` match before anything else; a consistency check, not authentication). Completeness recorded. Properties are not proof of permissions |
| `ble_read_profile_value` | SIM | FW written, unverified | Profile-approved characteristics only. A remote (backend/AI) job may read only public characteristics, not `app_auth`; reads chosen on the device are not restricted. Only public values may carry `value_hex`; others record length + SHA-256 |
| `ble_observe_notifications` | SIM | **M4+ / not built** | Subscribing writes the CCCD; treated as its own operation |
| `ble_check_access_policy` | SIM | **M4 / not built** | Event contract in [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#6-access-check-event-details-ble_check_access_policy) |
| `ble_write_lab_value` | SIM | **M4 / not built** | Local confirmation gate: host-tested logic ([ADR-005](adr/ADR-005-local-confirmation.md)); on-device dialog unverified |
| `ble_collect_diagnostics` | SIM | **M4 / not built** | Transfer protocol state machine: host-tested logic (`oyster_core/transfer.h`) |
| `compare_sessions`, `explain_evidence`, `generate_report` | backend only (no radio) | backend only | Cite only evidence from the session or an explicitly linked baseline |

## 2. Lab assertions on the live fixture route

Route: `oyster-fixture` (native WinRT GATT server on the Windows PC, [ADR-008](adr/ADR-008-native-winrt-fixture.md))
plus a real Cardputer as the central.

| Assertion | Fixture side (PC) | Device side (Cardputer) | Route status |
| --- | --- | --- | --- |
| `public_sensor_is_public` | served; always an observation | read: FW written, unverified | observation only, never a finding |
| `restricted_value_requires_link_encryption` | `GattProtectionLevel.ENCRYPTION_REQUIRED`, enforced by Windows | M4 / not built | **link encryption** is available on the fixture; the end-to-end result is unverified |
| `restricted_value_requires_authenticated_pairing` | `ENCRYPTION_AND_AUTHENTICATION_REQUIRED`, enforced by Windows | M4 / not built. Needs authenticated (MITM) pairing from the Cardputer **and** a readable authentication state from NimBLE | **authenticated pairing** is available on the fixture; device side unverified. If the device cannot read the link's authentication state, the result is `inconclusive`, never a pass |
| `restricted_diagnostics_requires_app_authorization` | application logic; vulnerable/corrected mode | M4 / not built | fixture ready; device side not built |
| `diagnostics_available_after_app_authorization` | HMAC challenge/response, 120 s TTL | M4 / not built | fixture ready; device side not built |
| `virtual_led_write_requires_app_authorization` | ATT `0x08` before authorisation, both modes | M4 / not built | fixture ready; device side not built |
| Per-connection application authorisation | **unsupported**: WinRT gives no reliable per-peer identity ([ADR-007](adr/ADR-007-fixture-auth-state.md)) | - | global state with TTL + `reset-state`; one client at a time |

Whether the live fixture can run depends on your Bluetooth adapter and driver. `oyster-fixture check`
(read-only) reports adapter presence, LE and peripheral-role support, LE Secure Connections, the WinRT GATT
server API, the available protection levels (authenticated pairing is marked "unverified end-to-end") and
notification support. It also reports the radio state; "radio reports off" means the fixture is **not ready**.
The usual causes are Bluetooth switched off in Windows Settings, or airplane mode. Switch it on and run
`.\scripts\fixture.ps1 check` again before `fixture.ps1 run`.

## 3. Handheld platform

| Capability | Status | Notes |
| --- | --- | --- |
| Board check `board_M5CardputerADV` | FW written, unverified | Board mismatch shows an error screen |
| Keyboard navigation + emergency cancel | mapping is host-tested logic; hardware unverified | Proposed: arrows = Fn + `;` `.` `,` `/` (bare in menus); Back = `` ` `` or `Del`; cancel = Fn+`` ` `` (this keyboard has no plain Esc key) or Ctrl+C. Confirm on the device |
| Home (`Fn`+`Del`) and Help (`?`) keys | mapping is host-tested logic; **PROPOSED, UNVERIFIED** | The Home screen also has a Help tile (8) as a visible fallback. Number keys `1`-`8` open the Home tiles |
| Settings > Key tester | FW written, unverified | Shows the physical keys of the last key change and the logical action they resolve to; only useful on the device. Back or Home twice leaves |
| 240x135 display UI (paged lists, progress, confirm dialog, mode banner) | FW written, unverified | Banner, first that applies: `LIVE RADIO`, `START FAILED`, `AI CONNECTED` (latest run used a provider), `BACKEND OK`, `OFFLINE`. Never `SIMULATED` |
| OysterOS look: design-system palette and layout, module icons, boot splash | FW written, unverified | Constants in `firmware/include/ui_theme.h`; static art (the mascot is the project's own; the icons are AI-generated) converted to RGB565 ([`../assets/PROVENANCE.md`](../assets/PROVENANCE.md)). One 240x16 16-bit sprite (7,680 B) paints every region, no full-screen buffer. Colours, font sizes and legibility have not been seen on a real display. Screen map: [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md) |
| Boot loader animation (mascot, 6 stored frames at 6 fps, flash-resident) | FW written, unverified | Advances only while `setup()` runs: the logo stays up (at least 1.5 s) until any key is pressed, with `Any key:Start` in the footer and USB provisioning still served; no task, timer or framebuffer; it stops when the first screen opens |
| Module framework: Home tiles 3 to 6 open `Screen::Module` pages (List, Text, Confirm) | FW written, unverified | `firmware/include/modules.h`, `firmware/src/modules/modules.cpp`. Network calls run on the worker task (`RequestKind::Module`, generic `BackendClient::call`), never on the UI task. A Confirm page ignores keys for 700 ms, Enter approves, Back rejects. Screen map: [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md) |
| Hosts (tile 3, "Linux"): host list, overview, services, logs note | FW written, unverified | Read-only. Host list > overview (CPU, memory, disk, uptime, state, age) > services list > a page that says there is no log source. Keys: Enter opens, Back goes up one level, `r` refreshes, `l` opens the logs page. Tile text `N hosts`, `no hosts`, `offline`. `firmware/src/modules/hosts.cpp`, parsers `oyster_core/hosts_json.*` (28 host tests). Backend: section 4. Checks: HV-31 |
| Control Deck (tile 4): profiles, slots 1-9, confirmation, result, history | FW written, unverified | Profiles (up to 5 per page) > slots (`*` marks an impactful slot; number keys run directly) > one fresh Idempotency-Key per key press, never auto-retried > local confirmation page bound to the job id and nonce for impactful slots (keys ignored 700 ms, expiry 120 s) > result page polled every 1.5 s > history (last 10). `firmware/src/modules/deck.cpp`, parsers `oyster_core/deck_json.*` (35 host tests). Details: [`CONTROL_DECK.md`](CONTROL_DECK.md). Checks: HV-32, HV-33 |
| Smart Home IR remote (tile 5, "IR Remote" room) | FW written, unverified | IR LED on GPIO44 (an assumption from the Cardputer example; not confirmed on this unit). Five TV families (Samsung32, LG NEC, Sony SIRC-12, a Toshiba-type NEC, Philips RC5); keys Power, Vol+, Vol-, Mute, Ch+, Ch-, number keys 1-6; the screen says `Sent`, never `done`. No learning (no IR receiver), no air-conditioner codes. No IR code has been tried on a real TV. `firmware/src/modules/smart_home.cpp`, `ir_remote.cpp`, tables in `oyster_core/ir_codes.*` (24 host tests, shared with the Home Assistant parsers `home_json.*`). Checks: HV-34 |
| Smart Home with Home Assistant (tile 5, Home Assistant rooms) | FW written, unverified | Rooms, devices and commands through the backend and an owner-written allowlist; locks, covers and alarm panels always ask on the device first. Tile text `IR+HA`, `IR only`, `offline`. Home Assistant is **not configured** yet (no URL or token): the screen says so, and IR works without it. Nothing has run against a real Home Assistant. Details: [`HOME_ASSISTANT.md`](HOME_ASSISTANT.md). Checks: HV-35 |
| Original Apps tile | FW written, unverified | Greyed-out tile that opens "Not available in this build". Nothing connects or runs behind it |
| microSD evidence (JSONL, manifests, artifacts, hashes) | truncated-record recovery is host-tested logic; device unverified | FAT is not power-loss atomic; needs the interruption tests |
| No SD card: volatile 8 KiB ring with reduced-persistence label | FW written, unverified | |
| Serial provisioning ([`PROVISIONING.md`](PROVISIONING.md)) | FW written, unverified | Secrets are never echoed |
| TLS to the backend (pinned private CA, host/IP match, no insecure mode) | FW written, unverified | **Known limitation:** certificate validity dates (notBefore/notAfter) are **not** checked, because the prebuilt Arduino-ESP32 2.0.17 mbedTLS is built without `MBEDTLS_HAVE_TIME_DATE`. Chain and host name are checked; the clock gate is firmware policy. A certificate failure is always shown as `cert-err`, never as a clock problem. See the amendment in [ADR-003](adr/ADR-003-transport-tls.md) |
| Clock bootstrap (serial seed, SNTP, `/v1/time` hint) | FW written, unverified | `clock_quality` recorded on every event |
| Offline capture and later sync | FW written, unverified | Later sync works only within the same session, before a reboot. Cross-reboot sync (earlier sessions) is M6; those events stay on the SD card. Without an SD card events are lost on reboot |
| Startup failure handling | FW written, unverified | `START FAILED` banner, Settings > Diagnostics lists the module, dependent actions refused with a message |
| Local policy (job re-validation on the device) | host-tested logic | |
| Heap/stack/queue telemetry | FW written, unverified | Goals: min free heap >= 64 KiB, largest block >= 32 KiB. Reported, never asserted |
| PSRAM | not used | ESP32-S3FN8 has none |
| Flash backup before the first flash (`scripts/firmware-backup.ps1`) | script written, unverified on hardware | Dumps the whole 8 MB flash with a size check, a chip-side `verify_flash`, a SHA-256 file and a JSON record. Tested only with `-DryRun`, the absent-port refusal and a stand-in for esptool's output, never with a device |
| Flash gate (`scripts/firmware-flash.ps1`) | script written, unverified on hardware | Refuses to flash a chip that has no matching, verified backup; `-SkipBackupCheck` overrides it with a warning. Same test limits as the backup |

## 4. Backend and AI

| Capability | Status | Notes |
| --- | --- | --- |
| Real provider (Groq, OpenAI-compatible, tool calls) | backend; needs your key. Ran end to end in the lab demo with a simulated device | `oyster ai-check` picks and records a working model ([ADR-002](adr/ADR-002-ai-provider.md)); it does not prove a whole run completes, so run `scripts/demo-m1.ps1` with the model you choose ([`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)). Free-tier rate limits are retried within a bound |
| Provider missing or unreachable | run ends `ai_unavailable` | No fabricated answer, no silent provider switch |
| Fake provider | tests and simulation only | Always labelled `is_fake` |
| Budgets (12 actions, 6 requests, 180 s, token caps) | enforced in code | |
| Device/owner auth, id binding, idempotent uploads | backend | Foreign ids -> 404. Owner/admin API on the loopback listener only; the LAN listener serves device routes and answers 403 to the owner token. Admin token >= 24 characters. Auth failures throttled (429) |
| Data sent to the AI provider | backend | Redacted; BSSIDs/BLE addresses masked by default, SSIDs not masked. See [`PRIVACY.md`](PRIVACY.md) |
| Reports (Markdown/JSON) with evidence validation | backend | |
| Docker route | **not provided** | No Dockerfile yet; would not give BLE access anyway |
| Pairing and read-only host health ([`COMPANION_API.md`](COMPANION_API.md)) | backend; a mock adapter and a read-only `local` adapter for the backend PC | The `local` adapter (psutil) reads CPU, memory, disk, uptime and allowlisted Windows services; the load average does not exist on Windows, so such a host reports `partial`. `adapter: "local"` is accepted at enrolment; `oyster.companion.seed.ensure_local_host` enrols it idempotently. Read on one Windows 11 PC for types and ranges only. No remote (SSH) adapter, no logs route; the Hosts module on the device is written, unverified |
| Control Deck: allowlisted jobs with local confirmation ([`CONTROL_DECK.md`](CONTROL_DECK.md)) | backend; mock executor (default) and a Windows executor for the backend PC | `OYSTER_DECK_EXECUTOR=mock\|windows` (default `mock`: nothing is executed, jobs say `executor: mock`). The Windows executor does media keys (`media_key`: play_pause, next, prev, vol_up, vol_down, mute; any media player), `lock_workstation` (asks first), Wake-on-LAN to allowlisted MACs, service status, and service restart only when the backend is elevated and the service is allowlisted. Unit-tested against a fake OS layer (486 deck tests); **media keys, lock, Wake-on-LAN and service restart are unverified on a real session**. Only the backend PC is controlled |
| Home Assistant bridge ([`HOME_ASSISTANT.md`](HOME_ASSISTANT.md)) | backend; tested against a fake Home Assistant; **not configured, never run against a real instance** | `GET /v1/home/rooms`, `GET /v1/home/rooms/{id}/devices`, `POST /v1/home/actions` (device token, Idempotency-Key required), owner `GET /v1/home/status` and `GET /v1/home/audit`. Allowlist file, lock/cover/alarm panels always impactful (alarm panels can only be armed), append-only audit with a pending row before each call, 20 actions and 60 reads per minute per device, URL and token never in responses or logs. 170 backend tests |

## 5. Simulators

The 240x135 screen simulator (`simulator/`, [`SIMULATOR.md`](SIMULATOR.md)) renders 40 routes in five states from the design tokens. It is a PC tool: pixel parity with the real panel and the key bindings are **unverified** until compared on the device.

The device protocol simulator `oyster-sim` runs these scenarios:

`oyster-sim scenarios` listed these on 2026-10-24 (`.\scripts\sim.ps1 -List`): `success`, `denied_access`,
`timeout`, `malformed_advertisement`, `unexpected_pairing_transition`, `address_change`,
`transfer_interrupted`, `vulnerable_target`, `corrected_target`, and `withheld`. `withheld` is a blind pick for
false-positive evaluation; the choice is written only to a `*.withheld.sealed` file next to the session file.
That file is plain text: it only keeps the choice off the console and is not secret. Whether
each scenario passes its tests is what `.\scripts\check.ps1` reports.

## 6. Hard limits of this hardware and design

| Not available | Why |
| --- | --- |
| 5 GHz / 6 GHz Wi-Fi visibility | the built-in radio is 2.4 GHz only |
| Bluetooth Classic (BR/EDR) | the ESP32-S3 supports BLE only |
| Raw packet sniffing, monitor-mode capture | out of scope |
| Wi-Fi password cracking | out of scope |
| Connecting to non-enrolled devices | out of scope; LocalPolicy rejects it |
| Generic phone/computer takeover, arbitrary file download | out of scope; the transfer profile serves one synthetic object |
| Live fixture on Linux/macOS | not supported in v1 (WinRT only); the simulator runs anywhere |
| WSL Bluetooth access | unverified; not used |
| Encrypted SD evidence | not provided; a hash shows integrity, not confidentiality |
| Voice (push-to-talk, speech) | post-v1 extension, not built |
| OTA firmware update | not built (no-OTA partition table) |
| Coexistence with the factory firmware, launcher and original apps; an "Original Apps" return path | not built. The firmware is a full-flash image that replaces them. Back up first; see [`INTEGRATION.md`](INTEGRATION.md) |
| Host logs, remote hosts (SSH), Control Deck actions on other machines, Home Assistant scripts / automations / disarming alarm panels, MQTT, IR learning, air-conditioner IR codes, scheduled automations | not built. The Hosts, Control Deck and Smart Home modules cover read-only health of the backend PC, typed actions on the backend PC, IR for five TV families and allowlisted Home Assistant entities; all of it is written, unverified on the device |
| GPS | no GPS code exists here and none is claimed |
