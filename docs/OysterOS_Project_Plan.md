# OysterOS Engineering Plan — v0.3

**Status:** specification; implementation not started. **Target:** M5Stack Cardputer ADV. **Priority:** preserve installed applications and existing launcher.

## 0. Non-negotiable constraints

1. **Do not flash, erase, repartition or replace existing firmware until inventoried, backed up where technically possible, and user-approved.**
2. The genuine original apps (Compass, voice, PikaScript, GPS-labelled tools, etc.) must remain accessible. Do not reproduce those interfaces as fake placeholders.
3. The Cardputer is an ESP32-S3 microcontroller, not a Linux computer. Linux tasks happen through an authenticated companion on another machine.
4. GPS capability is dependent on attached hardware and current firmware; do not claim the ADV has onboard GPS.
5. Each visible action must have a defined command, lifecycle, result, failure view and acceptance test. Incomplete features must be visibly marked unavailable.
6. Scope security tools to passive scanning and authorized diagnostics.

## 1. Architecture decision record: coexistence first

**Phase 0 inventory:** record exact board revision, installed firmware and version, boot sequence, visible apps, configuration, flash layout, launcher/app loading mechanism, USB serial behavior, and recovery options. Photograph menus. Research SDK and launcher APIs for installing an app/package. Capture any available verified official firmware source or backups. A raw flash dump may contain private settings/keys: keep local and out of Git.

**Preferred option A:** OysterOS as an application within the existing firmware/launcher, with clean return path.

**Fallback option B:** existing firmware-compatible multiboot / launcher feature, verified on this hardware with recoverable images.

**Fallback option C:** self-contained separate OysterOS firmware only after explicit approval, backup and a proven recovery path. In this case original firmware and apps may not be simultaneously present: label limitation plainly; do not claim otherwise.

**Gate P0:** an on-device demonstration opens OysterOS from existing menu and returns to original app menu without loss of programs or settings. Until this passes, develop desktop UI simulator + companion only.

## 2. Computer science learning outcomes

Modular embedded architecture; finite-state machines; input event processing; asynchronous I/O; API authentication and access control; persistence and schema versioning; test-driven UI flows; resource constraints; fault tolerance; secure remote execution.

## 3. Visual language and rendering contract

Target physical viewport **240x135 landscape**. Safe margins 4–6 pixels; 12–14px key labels when font permits; selected list row emphasized with seafoam accent (#5EEAD4). Colors: shell #101923, panels #192C3D, primary #F4FAFF, muted #A9C4CE, warning #E9AD52, error #F17A7A.

**UI primitives:** StatusBar; Header; MenuRow; ScrollList; DetailRow; LoadingView; EmptyState; Toast; ConfirmModal; KeyboardHelp; ResultPanel; ProgressView; ErrorPanel. Use dirty-rectangle or bounded redraws to limit flicker; don't block the event loop for network calls.

**Layout:** persistent 12–16px status bar; remaining space for title and 3–5 visible focusable rows; bottom help strip as space permits. Long results paginate/scroll. Always show *Disconnected* / *No results* / *Permission denied* states.

### Screen map
| Route | Subscreens | Primary action |
|---|---|---|
| /home | list/dashboard | select destination |
| /security | /wifi, /ble, /findings | start passive scan |
| /security/wifi | /scan, /results, /detail | scan then inspect SSID/RSSI/channel/security |
| /security/ble | /scan, /results, /detail | BLE advertisements and authorized characteristics |
| /ai | /compose, /pending, /answer | type question, send to companion, read paged response |
| /linux | /hosts, /host/:id, /tasks, /result | get health/logs, run approved tasks |
| /deck | /profiles, /profile/:id, /macro/:id, /run/:id | choose macro, parameterize, confirm, execute |
| /home-control | /rooms, /device/:id, /scenes | display device state; authorized control |
| /original | /unavailable | invoke verified original launcher adapter |
| /settings | /network, /pairing, /keyboard, /about | configure and diagnose |

### UI interaction states

`IDLE → LOADING → SUCCESS | EMPTY | ERROR | OFFLINE`

Dangerous operations: `READY → CONFIRM → QUEUED → RUNNING → SUCCESS | FAILED | TIMED_OUT | CANCELLED`.
UI should never report success before a backend acknowledgement.

## 4. Keyboard mapping & Back/Home

Separate **physical key mapping** from **logical actions**:
- `MOVE_UP`, `MOVE_DOWN`, `MOVE_LEFT`, `MOVE_RIGHT`
- `SELECT`, `BACK`, `HOME`, `HELP`, `TEXT_INPUT`, `SHORTCUT_1..9`
- `CANCEL`, `CONFIRM`

Exact `Fn` / `Esc` combos must be experimentally verified on device; avoid hardcoding untested key assumptions. Proposed Back=Escape, Home=Fn+Backspace **only once validated**; provide Settings > Keyboard with displayed and remappable bindings if feasible.

Input architecture: poll/interrupt → debounce → map to logical event → global navigation handler → active screen handler → redraw.

**Global invariants:** BACK closes modal first, otherwise pops navigation stack; HOME goes to OysterOS root; no action traps user; long-running task remains cancellable; original launcher switch uses verified firmware mechanism only.

## 5. Module wiring

### Wi-Fi Surveyor
`ENTER` on scan → hardware scan adapter → results sorted by RSSI → select network → detail view → save findings cache → optionally send sanitized scan summary to AI. Never require packet injection or connection attempts for MVP.

### BLE Explorer
Start scan → BLE advertisement callback → bounded deduplicated list → detail → optionally enumerate authorized GATT services on devices the user owns → disconnect cleanly. Explicit “permission/authorized use” warning.

### Oyster AI
Compose text on Cardputer → validate length → HTTPS authenticated POST to companion → AI provider proxy → show answer in paged view. Backend holds API key. For scan interpretation: user chooses **Explain this result**; context shared is shown first. Voice = later milestone with mic recording, transfer, transcription, answer and optional playback.

### Linux Console
Host list from companion → health (CPU/memory/disk/service status) → details and logs; task menu from backend allowlist. Avoid arbitrary shell input as default. Backend connects via SSH with host key verification or local OS adapters; non-root service account; command arguments validated and encoded safely, no shell-string concatenation. Action jobs return IDs and status.

### Smart Control Deck
A profile contains configurable slots (`1..9`) bound to `macro_id`. Macro contains ordered or conditional approved steps such as GET_SERVER_STATUS, START_DEV_STACK, RUN_BACKUP, HOME_SCENE. Every step is a typed backend action, with permissions, timeout, retry policy and an optional confirmation requirement.

Flow: select profile → shortcut → summary/preflight → confirmation if impactful → POST /jobs → poll /jobs/{id} → final result/history. No automatic blind repeats of dangerous actions. Network failures must show whether execution status is **unknown**, to prevent double runs.

### Smart Home
IR emitter: verified IR codes/protocols for owned devices; selectable profiles. Companion: Home Assistant API/MQTT via credentials stored only on companion; read device state before showing toggles; handle unavailable integrations without fake UI success.

### Original Apps
Adapter interface: `available(): bool`, `launch_original(): Result`, `return_supported(): bool`. Implement only after current firmware is documented. If unsupported, show explanatory disabled state; never overwrite flash as implicit navigation.

## 6. Companion service design

Suggested stack: Python 3.12+, FastAPI, SQLite, typed models, APScheduler or equivalent. Run under dedicated unprivileged account.

### API v1 (illustrative; not yet implemented)
| Method | Route | Request | Response |
|---|---|---|---|
| GET | /v1/health | — | online, version, capabilities |
| POST | /v1/pair | short-lived pairing proof | scoped device credential |
| GET | /v1/hosts | — | filtered host list |
| GET | /v1/hosts/{id}/health | — | CPU/memory/disk/service states |
| GET | /v1/hosts/{id}/logs | cursor, source, count | redacted entries |
| GET | /v1/deck/profiles | — | profile, slot and macro summaries |
| POST | /v1/jobs | macro_id, input, idempotency_key | job_id, state |
| GET | /v1/jobs/{id} | — | status, sanitized output, timestamps |
| POST | /v1/jobs/{id}/cancel | — | accepted or not cancellable |
| GET | /v1/jobs | cursor | bounded job history |
| POST | /v1/ai/query | question, optional reviewed context | answer |
| GET | /v1/home/devices | — | configured targets and state |
| POST | /v1/home/actions | target_id, approved action | job/result |

Authentication: unique device identity, short-lived pairing setup, transport encryption, token rotation/revocation, backend authorization per macro; don't trust client-supplied command templates. HTTP-only development may be restricted to loopback emulator; device deployments require encrypted, authenticated transport or separately documented secure tunnel.

Jobs table: `id, type, macro_id, requested_by, idempotency_key, state, started_at, completed_at, result_redacted, error_code`.
Macros table: `id, name, steps_json, capabilities, confirm_policy, enabled, updated_at`.
Profiles table: `id, name, slot_mapping_json, updated_at`.
Audit table: `timestamp, actor, target, action, decision, job_id`.

### Automation scheduler
Scheduled trigger → authorization/allowlist validation → job queue → step execution → persisted outcome. Schedules run on companion even when Cardputer is off. Editing/deleting schedules requires authentication, and the UI distinguishes **scheduled** from **executed**. Duplicate prevention via idempotency key. No arbitrary AI-generated commands run without user review.

## 7. Repository layout (target, not implemented)
```text
Cardputer-OysterOS/
├── README.md
├── docs/
│   ├── OysterOS_Project_Plan.md
│   ├── hardware-inventory.md       # after firmware audit
│   ├── architecture-decisions/
│   └── ui-screens.md
├── firmware/
│   ├── platformio.ini              # only after SDK selection
│   ├── src/
│   │   ├── app/
│   │   ├── input/
│   │   ├── ui/
│   │   ├── modules/
│   │   ├── drivers/
│   │   └── services/
│   └── test/
├── companion/
│   ├── app/
│   │   ├── api/
│   │   ├── auth/
│   │   ├── jobs/
│   │   ├── adapters/
│   │   └── persistence/
│   └── tests/
├── simulator/
└── .github/workflows/
```

## 8. Delivery milestones, definition of done

**M0 — Inventory and preservation:** documented existing firmware and apps, demonstrated backup/recovery options, validated launcher coexistence method. **No device flashing before this.**

**M1 — Functional shell in simulator:** pixel-appropriate UI, navigation stack, focus, keyboard mapping, Back/Home/Help, mock adapters clearly labelled; unit tests for escape from every route.

**M2 — Original launcher + device navigation:** integrate supported launcher mechanism; verify real original apps still launch and return; measure keyboard latency and reboot behavior.

**M3 — Hardware scanners:** Wi-Fi and BLE lists/details/saved findings; test with radios disabled, zero results, duplicate results, limited memory, scans interrupted.

**M4 — Companion:** explicit pairing, offline recovery, read-only host health/logs and capability negotiation.

**M5 — Deck + Linux jobs:** profile slots, confirmed allowed task execution, idempotency, running/failure/cancel/history screens; command injection and authorization tests.

**M6 — Home + automation:** IR profile, home provider adapter, scheduled workflows, history; test unavailable devices and companion reboot.

**M7 — AI:** question/answer, bounded contextual explanations; optional voice only after robust text interface.

### Acceptance criteria
- No application leaves user unable to return Home.
- Original firmware/apps remain intact according to the chosen verified coexistence strategy.
- Every displayed control has a tested event handler or visible unimplemented state.
- No unverified success toast.
- No secrets embedded in firmware image or Git repository.
- Tests cover no Wi-Fi, companion offline, pairing expiry, repeated button press, power interruption, malformed API data, and cancellation.
- RAM/flash/build footprint tracked against actual Cardputer ADV board revision.

## 9. First coding agent task

1. **Do not flash the hardware.** Inspect this repository and produce a desktop simulator for 240x135 screen routes matching the README.
2. Implement reusable UI widgets, focus/navigation stack, global Back/Home, loading/error/modals and keyboard test cases.
3. Stub controllers behind clean interfaces, and label unimplemented capabilities.
4. Create a read-only local mock companion API for host status and macros; no command execution.
5. Request an **actual screenshot/photo of existing firmware home screen** before writing launcher integration code.
6. Publish screenshots and a traceability matrix: each menu → route → action → handler → result view → automated test.

**Success means functioning architecture and preserved software, not an attractive menu full of dead buttons.**
