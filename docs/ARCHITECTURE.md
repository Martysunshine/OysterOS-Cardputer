# Architecture

This describes the design of Oyster Pocket as given by the ADRs and the interface definitions (`schemas/`,
`docs/API.md`, `docs/PROVISIONING.md`, firmware headers). It describes the design, not proof that it works.
What is implemented and verified is in [`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md). How these parts relate to
the OysterOS design package (Linux Console, Control Deck, Smart Home and Original Apps are not part of them) is in
[`INTEGRATION.md`](INTEGRATION.md).

## 1. Components

```
                      2.4 GHz Wi-Fi LAN                                   Internet
  +------------------+   HTTPS :8443 (dev-CA TLS,   +---------------------+   HTTPS    +-----------+
  |  Cardputer ADV   |   device bearer token)       |  Backend (FastAPI)  | ---------> | Groq API  |
  |  firmware        | ---------------------------> |  SQLite + files     |  AI key    | (model)   |
  |  UI, radios, SD  |   device polls; no inbound   |  AI orchestrator    |  only here +-----------+
  +------------------+   port on the device         +---------------------+
     |   ^      |                                       ^ HTTP 127.0.0.1:8000 (admin token / sim token)
 BLE |   | adv  | SPI                                   |
     v   |      v                                  +----+---------------------------+
  +------------------+   +---------+               | oyster CLI | oyster-sim         |
  | Lab GATT fixture |   | microSD |               | (owner)    | (virtual Cardputer,|
  | WinRT, on the PC |   | FAT32   |               |            |  source: simulated)|
  +------------------+   +---------+               +----------------------------------+
```

| Component | Runs on | Responsibility |
| --- | --- | --- |
| Handheld firmware | the single Cardputer ADV | UI, radio scheduling, live Wi-Fi/BLE operations, SD evidence, local policy and confirmation, device authentication |
| Backend API | Windows PC | sessions, device jobs, persistence, policies, deterministic evaluators, AI runs, reports |
| AI orchestrator | backend process | turns a question into typed tool calls, validates them, explains evidence |
| Model endpoint | Groq (OpenAI-compatible) | inference only. Never on the handheld |
| Simulator `oyster-sim` | PC | a contract-level virtual Cardputer. Every event is `source_mode: simulated` |
| Live fixture `oyster-fixture` | PC with a peripheral-capable BLE adapter | publishes the synthetic lab GATT profile (vulnerable or corrected mode) |

The backend does not need to be next to the Cardputer, only reachable on the network. Live BLE work
needs the Cardputer within radio range of the target.

## 2. Execution modes

| Mode | Radio | AI | Label |
| --- | --- | --- | --- |
| Live | real operations on the Cardputer | when the backend is reachable | `LIVE RADIO` / `BACKEND OK` / `AI CONNECTED` |
| Offline | real local operations, evidence queued on SD | explicitly unavailable, never invented | `OFFLINE` |
| Simulation | none; simulator fixtures | real model allowed (M1 demo) or fake provider in tests | `SIMULATED` (simulator events and exports) |

The Cardputer's mode banner shows the first value that applies:

1. `LIVE RADIO`: a radio phase (Wi-Fi survey, BLE discovery or a BLE session) is active.
2. `START FAILED`: a module failed at boot. The banner stays, Settings > Diagnostics lists the modules,
   and the actions that depend on them are refused with a message.
3. `AI CONNECTED`: only while the latest AI run actually used a provider (status `running`,
   `awaiting_device`, `awaiting_local_confirmation`, `succeeded` or `budget_exhausted`).
4. `BACKEND OK`: the backend is reachable and the device is authenticated.
5. `OFFLINE`.

`SIMULATED` is never shown for firmware data. The simulated label is carried in every simulator event
(`source_mode`), every finding built from one, and every export. A finding is `simulated` if any of its
input events is simulated.

## 3. Firmware

Toolchain: PlatformIO, Arduino-ESP32 2.0.17 / ESP-IDF 4.4.7, NimBLE-Arduino 2.x, M5Cardputer/M5Unified/M5GFX
([ADR-001](adr/ADR-001-firmware-toolchain.md), [`BUILD_MATRIX.md`](BUILD_MATRIX.md)).

### Modules

| Module (file) | Responsibility |
| --- | --- |
| `BoardSupport` (`src/board_support.cpp`) | M5 init, board-id check (`board_M5CardputerADV`), display, keyboard snapshot, battery, SD mount/full/write-error state |
| `AppController` + `ui::*` (`src/app_controller.cpp`, `src/ui/`) | screen state machine, session lifecycle, cancel, mode banner, Home tile grid, paginated lists, progress, confirm dialog. Design tokens and layout constants: `include/ui_theme.h`; icons and splash: `src/assets_gen.cpp` (generated from `assets/`). Screen-by-screen map: [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md) |
| `InputMapper` (`lib/oyster_core/src/input_map.cpp`) | pure key-to-navigation mapping, host-tested |
| `RadioScheduler` (`src/radio_scheduler.cpp`) | single owner of the shared 2.4 GHz RF path; one phase at a time; cooperative cancel |
| `WifiSurvey` (`src/wifi_survey.cpp`) | bounded passive-first AP survey; WPS/PMF recorded as `unknown` |
| `BleExplorer` (`src/ble_explorer.cpp`) | NimBLE observer/central: discovery (never connects), enrolled-target connection with a fail-closed identity check, GATT inspection, profile reads |
| `ResultsCache` (`src/results_cache.cpp`) | latest results for the UI, read in pages |
| Operations (`src/operations.cpp`) | one executor per operation, shared by UI actions and backend jobs; non-implemented ops return `unsupported` |
| `AssessmentRunner` (`src/assessment_runner.cpp`) | the single worker task: UI requests, evidence upload, job poll, LocalPolicy validation, local confirmation, execution, result |
| `EvidenceStore` (`src/evidence_store.cpp`) | append-only JSONL events, manifests, streamed artifacts with SHA-256, truncated-record recovery, volatile ring without SD |
| `DeviceConfig` (`src/device_config.cpp`) | NVS settings and secrets; CA PEM in LittleFS; locally enrolled targets |
| `SerialProvisioning` (`src/serial_provisioning.cpp`) | the USB serial protocol of [`PROVISIONING.md`](PROVISIONING.md) |
| `BackendClient` (`src/backend_client.cpp`) | device-initiated validated TLS, job polling, event batches, idempotent results, run status |
| Telemetry (`src/telemetry.cpp`) | free/min heap, largest block, stack high-water marks, queue occupancy, drops, reset reason |
| `oyster_core` (`lib/oyster_core`) | platform-independent: ids, types, JSON writer, JSONL recovery, SHA-256, transfer protocol, LocalPolicy, lab profile (generated) |

Boot order: `g_board.begin` -> `ui::begin` (the boot splash is drawn once here) -> `g_config.begin` -> `g_evidence.begin(Live)` -> `g_radio.begin` ->
`g_results.begin` -> `g_ble.begin` -> `g_backend.begin` -> `g_runner.begin` -> `g_app.begin`.
`loop()` runs `M5Cardputer.update(); g_provisioning.poll(); g_app.tick();`. Long work happens on the
runner task, so the UI loop never blocks on the network or radio.

### Radio phases

```
IDLE -> WIFI_SURVEY | BLE_DISCOVERY -> BLE_SESSION -> UPLINK_SYNC -> IDLE
```

Wi-Fi and BLE share the RF path, so exactly one phase is active. Discovery pauses during a connected
BLE session. Backend polling runs only in `IDLE`/`UPLINK_SYNC`. Every bounded loop polls the cancel flag.
The UI aims to acknowledge a cancel within 1 s; actual radio-stop latency is measured separately.

### BLE target identity (fail closed)

- **Choosing a device.** An enrolled address is the only candidate. Without an enrolled address, every
  discovery row that advertises the enrolled service UUID or carries the exact advertised name is a
  candidate; two or more candidates are refused as ambiguous ("enrol an address"). Name and UUID are hints,
  never identity.
- **Verifying it.** After connecting, the only first action is one read (ATT Read By Type, no service
  discovery) of the profile's public `device_info` value, with bonding off. Anything other than an exact
  `oyster-lab-v1;1` disconnects before any other read. Bonding is enabled for that link only after the
  match.
- The value is public, so this is a consistency check against the enrolled profile, **not
  authentication**. Enrolling the target's address is recommended
  ([`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#8-target-identity-and-remote-reads-on-the-cardputer)).

### Remote jobs: reads and limits

- A job from the backend/AI may read only characteristics whose read policy is public, excluding
  `app_auth` (reading it replaces the fixture's single outstanding challenge). Anything else is refused
  before the radio is used and recorded as `denied`. Reads chosen on the device itself are not restricted.
- Protected access happens only through the access-policy assertions, gated by local confirmation when the
  assertion writes or transfers. The device side of those is M4.
- Job limits (`max_results`, `max_duration_s`, `max_bytes`, and the job deadline) are enforced on the
  device and recorded in a `job_limits` object in the summary event (limits received, plus `applied`:
  the ones that changed the result). A hit makes the result `Partial` with `complete: false`.

### Flash layout and preservation

The image is a full-flash build with its own partition table and no OTA slot
(`firmware/partitions_8mb_noota.csv`). It covers the whole 8 MB (`0x800000`):

| Partition | Type | Offset | Size |
| --- | --- | --- | --- |
| `nvs` | data | `0x9000` | `0x6000` |
| `phy_init` | data | `0xF000` | `0x1000` |
| `factory` | app | `0x10000` | `0x500000` |
| `spiffs` (used as LittleFS) | data | `0x510000` | `0x2E0000` |
| `coredump` | data | `0x7F0000` | `0x10000` |

Uploading it replaces the partition table and the application, so the M5Stack factory firmware, the launcher and
the original apps can no longer start. Nothing in this repository starts them again; there is no launcher
integration. The safety net is a copy of the whole flash: `scripts/firmware-backup.ps1` reads `0x0..0x800000` with
the esptool bundled in PlatformIO, and `scripts/firmware-flash.ps1` refuses to upload to a chip without a verified
copy. Restore route: [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-your-own-backup-esptool). A build that
runs as an app inside the existing launcher would need a different partition layout and loading mechanism; it is
not built ([`INTEGRATION.md`](INTEGRATION.md)).

### Resource limits (`oyster_core/config.h`)

These are engineering defaults to measure and tune. They are not demonstrated performance.

| Limit | Value |
| --- | --- |
| AP observations / BLE adverts in memory | 32 / 64 (overflow counted) |
| GATT entries per page | 64 |
| BLE connections | 1 |
| Enrolled targets on the device | 8 |
| Device API page | 4 KiB |
| One JSONL event | 2 KiB |
| Streaming buffer | 2 KiB |
| Diagnostic object | 64 KiB |
| Volatile event ring (no SD) | 8 KiB |
| Heap goals (reported, not asserted) | min free heap >= 64 KiB, largest free block >= 32 KiB |

No PSRAM is assumed (ESP32-S3FN8). The UI paints every region through one resident 240x16 RGB565 sprite
(7,680 B of internal RAM), not a full-screen framebuffer; the boot splash is drawn once, band by band. If the
sprite cannot be allocated, screen output is switched off and the rest keeps running.

### Keyboard (proposed; must be confirmed on hardware)

All bindings are **proposed and UNVERIFIED**.

| Action | Keys |
| --- | --- |
| Emergency cancel (any screen) | `Fn+`` ` `` (reported by the library as fn+esc; this keyboard has no plain Esc key) or `Ctrl+C` |
| Navigate | arrow legends `;` up, `.` down, `,` left, `/` right (bare in menus, with `Fn` in text entry) |
| Select / Back | `Enter` / `` ` `` or `Del` (navigation mode) |
| Home | `Fn`+`Del` (press `Fn` first), any mode; the design plan allows it only once validated |
| Help | `?` (`Shift`+`/`), navigation mode |
| Home shortcuts | `1`-`8` open the tiles (Help is tile 8, a visible fallback for the unverified `?`) |
| Text entry | printable keys type, `Del` erases (and leaves when empty), `Enter` confirms |

Settings > Key tester shows the physical keys of the last key change and the action they resolve to. The
confirmed mapping goes in [`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) (HV-03).

## 4. Backend

Python 3.12+, FastAPI, Pydantic, HTTPX, stdlib `sqlite3` ([ADR-004](adr/ADR-004-persistence.md)).

| Package | Responsibility |
| --- | --- |
| `oyster/api` | HTTP routes of [`API.md`](API.md); HTTPS listener on the LAN for devices (device routes only), loopback HTTP for the owner CLI and simulator (every route) |
| `oyster/store.py`, `oyster/db` | SQLite metadata; artifacts under `backend/data/sessions/<id>/`; device tokens stored as salted hashes |
| `oyster/jobs` | job lifecycle, deadlines/expiry, idempotent results, stale-scope rejection |
| `oyster/policy` | deterministic assertion evaluators (`evaluate_assertion`), see [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#7-how-results-are-classified) |
| `oyster/ai` | provider adapter (Groq, OpenAI-compatible), tool catalogue, run worker, redaction |
| `oyster/reports` | Markdown/JSON reports that cite only evidence from the session or a linked baseline |
| `oyster/cli.py` | `oyster init-db, serve, make-dev-ca, enroll-device, enroll-target, list-targets, sessions, provision-device, ai-check, ask, export-report, validate-schemas` |

### Authentication and ownership

- Device: `Authorization: Bearer <device token>`, issued once by enrolment. Re-enrolment rotates it.
- Owner: `OYSTER_ADMIN_TOKEN` for `/v1/admin/*` and owner reads, accepted **only on the loopback HTTP
  listener**. The LAN HTTPS listener serves device routes only; an owner token presented there gets 403.
- `oyster serve` refuses an admin token that is empty, a placeholder, shorter than 24 characters or too
  uniform, and prints a generation command
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- Authentication failures are throttled per client address: more than 10 within a minute block that
  address for 60 s (429 with `Retry-After`).
- Every session/job/run/event id is bound to one device. A foreign id returns 404, so ids cannot be
  probed.

### Job lifecycle

```
queued -> delivered -> [awaiting_local_confirmation] -> running
       -> succeeded | failed | cancelled | expired | unknown_outcome
```

Result submission is idempotent and can be retried. Execution is never repeated automatically.
`unknown_outcome` is kept until reconciled, and target-changing operations are never retried
automatically.

### AI run

```
queued -> running -> (awaiting_device | awaiting_local_confirmation)*
       -> succeeded | failed | cancelled | expired | budget_exhausted | ai_unavailable
```

1. Load the session, the question, the target profile, allowed operations and bounded evidence.
2. Ask the provider. Everything goes through tool calls (catalogue tools + `final_answer`), because Groq cannot
   combine structured outputs with tools ([ADR-002](adr/ADR-002-ai-provider.md)).
3. Validate the call against `schemas/ai-action.schema.json`. One repair attempt is allowed, then the run fails visibly.
4. Check target, profile, prerequisites, limits and the session's `capabilities`. Device tools are only
   offered when the device reports them.
5. Create a bounded job. The device validates it again independently and asks for local confirmation if
   the operation type requires it.
6. The result and evidence ids go back to the model. Repeat within budget (12 actions, 6 provider requests,
   180 s, 12 000 in / 1 500 out tokens per request), then produce an evidence-linked answer.

Deterministic evaluators classify results. The model explains and proposes, and it cannot turn a timeout,
a public value or an unsupported check into a confirmed finding. SSIDs, BLE names, values and notes
are treated as untrusted data, never as instructions. Secrets are redacted before anything is sent to the
provider, and BSSIDs/BLE addresses are masked by default. What the provider receives is listed in
[`PRIVACY.md`](PRIVACY.md).

## 5. Trust boundaries

| Boundary | Control |
| --- | --- |
| Model -> backend | strict schema validation; only catalogue tools; no shell, code or firmware commands |
| Backend -> device | device re-validates every job (`LocalPolicy`): wrong device/session/target, stale scope, expired, oversize args, duplicate job id, unsupported op. Remote reads are limited to public characteristics (not `app_auth`); job limits are enforced on the device |
| Owner -> backend | owner/admin API on the loopback listener only; the LAN listener refuses the owner token (403); failed authentications are throttled |
| Write/transfer operations | local confirmation derived from the operation type (for `ble_check_access_policy`: from the selected assertion's write/transfer operation in the enrolled profile), bound to `{job_id, nonce}`, 120 s timeout. No backend or model flag can bypass it ([ADR-005](adr/ADR-005-local-confirmation.md)) |
| Device <-> backend transport | TLS validated against a provisioned private CA; certificate names the backend LAN IP; no "skip verification" path. Certificate validity dates are not checked by the device (amendment in [ADR-003](adr/ADR-003-transport-tls.md)); a certificate failure is always shown as `cert-err` |
| Clock | serial seed at provisioning (applied at `DONE`), then SNTP, then `/v1/time` hint; `clock_quality` on every event. Without a plausible clock the firmware sends no authenticated request (`ClockNotSet`, a firmware policy) |
| Targets | only targets enrolled locally over USB; the backend cannot add targets to the device remotely. A connected peer must pass the `device_info` identity check before anything else is read |
| AI provider | redacted, bounded evidence; identifiers masked by default; see [`PRIVACY.md`](PRIVACY.md) |
| Secrets | AI key only in `backend/.env`; device token/Wi-Fi password/CA never echoed, logged, or written to SD |

The device credentials are development credentials in flash. They are not tamper-proof storage.

## 6. Evidence

- Event envelope: `schemas/event.schema.json` (schema version, ids, sequence, boot id, `source_mode`,
  transport, target, operation, UTC time or null + `clock_quality`, uptime, status, details).
  A measurement that was not taken is stored as `null`/`unknown`, never as a default value.
- Findings: `schemas/finding.schema.json` with classification `observation`, `candidate`, `confirmed`,
  `passed`, `inconclusive` or `unsupported`.
- SD layout: `/oyster/sessions/<session-id>/events.jsonl`, `manifest.json`, `wifi.csv`,
  `artifacts/<object-id>.bin` (`.partial` while streaming). Recovery tolerates a truncated last JSONL line.
  FAT is not power-loss atomic, so this has to be tested on the device.
- Without an SD card, a session's events live only in the 8 KiB RAM ring and are lost on reboot.
- Upload state on the Saved Findings screen (Security > Saved Findings):
  - Skipped (refused by backend): N. Kept on the SD card; NOT acknowledged. Not counted as accepted.
  - "Acked by backend, highest seq: N" (the accepted-through value) is the highest accepted sequence; there
    can be holes (skipped or lost events) below it.
- Previous session: events of an earlier session (ended by New session, reboot or power loss) are NOT
  uploaded by this firmware. They stay on the SD card in `/oyster/sessions/<session-id>/events.jsonl`. At
  boot the newest earlier session is scanned and repaired (a truncated last line is terminated), and a
  recovery event in the new session records its id, valid and invalid line counts and the unacked range
  with `sync_status` `not_uploaded_milestone_m6`. Uploading earlier sessions is milestone M6.
- `wifi.csv` columns `survey_started_ms` (device uptime), `exported_at_utc` and `export_clock_quality`
  describe when rows were written, not when each AP was observed. The Wi-Fi summary event field
  `observed_ap_country` is a neighbour AP's advertised country, NOT this device's regulatory domain.

## 7. Contracts and decisions

| Contract | File |
| --- | --- |
| HTTP API | [`API.md`](API.md) |
| Serial provisioning | [`PROVISIONING.md`](PROVISIONING.md) |
| JSON Schemas + examples | `schemas/*.schema.json`, `schemas/examples/` |
| Lab GATT profile | `schemas/lab-profile.json`, rendered in [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md) |
| Firmware interfaces | `firmware/include/*.h`, `firmware/lib/oyster_core/include/oyster_core/*.h` |

ADRs: [001 toolchain](adr/ADR-001-firmware-toolchain.md), [002 AI provider](adr/ADR-002-ai-provider.md),
[003 TLS/time](adr/ADR-003-transport-tls.md), [004 persistence](adr/ADR-004-persistence.md),
[005 local confirmation](adr/ADR-005-local-confirmation.md), [006 lab profile](adr/ADR-006-lab-profile.md),
[007 fixture auth state](adr/ADR-007-fixture-auth-state.md), [008 WinRT fixture](adr/ADR-008-native-winrt-fixture.md).
