# Integration: the OysterOS design package and the Oyster Pocket implementation

This repository holds two things that were developed separately:

- the **design package**: the root [`README.md`](../README.md) (product vision, seven modules),
  [`OysterOS_Project_Plan.md`](OysterOS_Project_Plan.md) (engineering plan, UI contract in sections 3, 4, 10 and 11),
  [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md), [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md) and
  [`HARDWARE_AUDIT_TEMPLATE.md`](HARDWARE_AUDIT_TEMPLATE.md);
- the **implementation**, called Oyster Pocket: `firmware/`, `backend/`, `lab/`, `schemas/`, `scripts/`, `tests/`
  and the other docs ([overview](POCKET_CORE.md)).

This page maps one onto the other. The design documents stay the specification and the target. For what
exists today, the code, [`API.md`](API.md) and [`CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md) are authoritative.
**Nothing here has been verified on a Cardputer: no device has been attached.**

| Design name | Where it is here |
| --- | --- |
| Companion service (`companion/`) | `backend/` (FastAPI, SQLite, HTTPS for the device, loopback HTTP for the owner) |
| Simulator (`simulator/`) | `simulator/` (`oyster_ui_sim`) is a 240x135 screen simulator ([`SIMULATOR.md`](SIMULATOR.md)); `lab/` (`oyster-sim`) simulates the device protocol (jobs, evidence) |
| Firmware (`firmware/`) | `firmware/` (PlatformIO, C++), host-testable core in `firmware/lib/oyster_core` |
| `.github/workflows/` | none: CI is not configured yet ([`CI_SUGGESTION.md`](CI_SUGGESTION.md)) |

## 1. Module map

| Design module | Status here | Notes |
| --- | --- | --- |
| Security: Wi-Fi Survey | Implemented in the firmware (`src/wifi_survey.cpp`). **Unverified on hardware.** | 2.4 GHz only (built-in radio). Passive scan first; no connection attempts to surveyed networks. Results sorted, detail view, evidence on SD and in the backend |
| Security: BLE Explorer | Implemented in the firmware (`src/ble_explorer.cpp`). **Unverified on hardware.** | Discovery never connects. GATT inspection only for **locally enrolled** lab targets after a fail-closed identity check: stricter than the design's "devices you own" |
| Security: Saved Findings | Evidence store (`src/evidence_store.cpp`) plus backend sessions and reports. **Unverified on hardware.** | Shown as Security > Saved Findings (the `Evidence` screen in the code); the AI cites evidence ids |
| Oyster AI | Through the companion backend: question, asynchronous AI run, evidence-linked answer. Groq adapter (OpenAI-compatible) tested against mocked HTTP and a fake provider, and in the lab demo (`scripts/demo-m1.ps1`) against a real Groq model with a **simulated** device; not yet with the Cardputer | Text only. Key stays on the backend. The AI calls typed catalogue tools; it cannot run commands, and writes need local confirmation on the device. The on-device Oyster AI tile (ask a question, latest run, cancel, sync) is written, unverified |
| Linux Console | Backend only: pairing, host registry and read-only health through **mock adapters** ([`COMPANION_API.md`](COMPANION_API.md)). No firmware screens (the tile stays greyed out) | No real host adapter, no logs route; nothing in it has touched a real host |
| Smart Control Deck | Backend only: profiles with slots 1-9, allowlisted typed actions, local confirmation, audit, **mock executor** ([`CONTROL_DECK.md`](CONTROL_DECK.md)). No firmware screens (the tile stays greyed out) | Nothing is executed on any host; the device-side screens D10-D22 exist only in the simulator's preview profile |
| Smart Home (IR, Home Assistant) | **Not built** | No IR code; no Home Assistant adapter |
| Original Apps | **Not built, and not possible with this build** | See section 5 |
| Settings & Device Tools | Partly: backend state, brightness, storage, diagnostics, keyboard help, key tester; USB serial provisioning instead of on-device pairing | Written, unverified |

The firmware Home screen (heading `OYSTER OS`) is a two-column tile grid of the design's six modules, numbered
1-6 as in the design's wireframe, plus Settings (7) and Help (8) in a fourth row. The four that are not built
(Linux, Control Deck, Smart Home, Original Apps) are greyed-out tiles that open a "Not available in this build"
screen that runs and connects to nothing (design constraint 5). Enrolled Targets and Assess are Oyster Pocket
screens for authorised BLE checks; they sit under Security, next to the design's Wi-Fi, Bluetooth and Findings
entries. The mapping between the design's screen identifiers and the firmware screens is in
[`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).

## 2. Milestone map

Design milestones are in [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Oyster Pocket has its own
milestones M0-M6, which do not carry the same names: our M0 is "Compatibility" (pinned toolchain, firmware
scaffold), not the design's M0 (inventory and preservation). Where our milestones stand:

| Our milestone | State |
| --- | --- |
| M0 Compatibility | Done on the PC: the toolchain is pinned and the firmware builds. **Hardware bring-up is pending**: board identity, flashing, display and keys have not been tried on a device |
| M1 AI vertical slice | Done on the PC with a **fake** AI provider and mocked HTTP. **The real Groq run is pending** (it needs a key; see open decision 5) |
| M2 Live Wi-Fi survey | Firmware written and compiled; the platform-independent logic is host-tested. **Unverified on hardware** |
| M3 Live BLE explorer | Same as M2. **Unverified on hardware** |
| M4 Assessment and collection | Backend and simulator side done; **device side not built** |
| M5 Agentic investigation | Backend orchestrator and a fake-provider end-to-end run done on the PC. The real Groq run and the device-side M4 are pending; the device only relays the question |
| M6 Reliability and release | Docs and scripts only so far; no reliability test has run (they need a device) |

How they line up with the design's milestones:

| Design milestone | Oyster Pocket counterpart | State |
| --- | --- | --- |
| M0 Know the device, protect the original software | none (our M0 is a different thing). New: audit step 0 in [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md), `scripts/firmware-backup.ps1`, the flash gate in `scripts/firmware-flash.ps1` | **Not done.** No inventory of a real unit, no coexistence proof, no tested restore. The gate "preservation strategy documented and tested" is not met |
| M1 240x135 UI simulator | `simulator/` ([`SIMULATOR.md`](SIMULATOR.md)): headless PNG export and a Tk window, 40 routes in 5 states, glyph tables taken from the pinned M5GFX; the key map is also host-tested (`firmware/test/test_input`) | Built and tested on the PC. **Not compared with a real display**; it re-implements the screen logic in Python, with drift tests against the firmware sources |
| M2 Firmware shell, Original Apps return path | The firmware shell exists (navigation stack, modals, mode banner, key mapping, the Home tile grid with icons and a boot splash in the design-system style, greyed-out tiles for the unbuilt modules). No Original Apps path | **Partial.** The roadmap allows "or fail gate with explicit limitation": that is where this stands. The look and the keys are unverified on a display |
| M3 Security tools | our M2 Live Wi-Fi survey, M3 Live BLE explorer | Firmware written and compiled, platform-independent logic host-tested; the deliverable "real scan data on device" is **not met** (unverified on hardware) |
| M4 Companion and Linux | our M1 AI vertical slice: authenticated backend, device API, jobs, simulator | Backend and simulator done on the PC, with a fake AI provider. Pairing is replaced by device tokens issued with the admin token and loaded over USB. Linux host health: read-only API with mock adapters ([`COMPANION_API.md`](COMPANION_API.md)); no real adapter, no firmware client |
| M5 Smart Control Deck | Backend: [`CONTROL_DECK.md`](CONTROL_DECK.md) | Backend and mock executor done on the PC; firmware screens and a real executor **not built** |
| M6 Home and automations | none | **Not built** |
| M7 Oyster AI and voice | our M1 and M5 (agentic investigation): orchestrator, budgets, evidence-linked answers, redaction | M1 part: done on the PC against a fake provider and mocked HTTP; the real Groq run is pending. M5 part: backend orchestrator and fake-provider end-to-end done on the PC; the real Groq run and the device-side M4 are pending. Explicit context sharing: the backend sends redacted, bounded evidence ([`PRIVACY.md`](PRIVACY.md)). Voice: **not built** |
| M8 Quality and documentation | our M6 Reliability and release | Docs and scripts only so far. Host tests and pytest run on the PC, but no reliability test has run: power-interruption, 20-cycle and soak tests need a device. CI: not configured yet |
| (beyond the design) | our M4 Assessment and collection: access-policy checks, reversible write, diagnostic transfer | Backend and simulator side done; **device side not built** (the device answers `unsupported`) |

## 3. Companion API: design plan section 6 against the implemented API

The plan marks its API v1 as "illustrative; not yet implemented". [`API.md`](API.md) describes what the backend
serves today.

| Design route | Implemented equivalent | Difference |
| --- | --- | --- |
| `GET /v1/health` | `GET /v1/health` | Same route. Returns `{"status":"ok","api":"v1"}`: no version or capability list. The device reports its own capabilities per session |
| `POST /v1/pair` | `POST /v1/pair` with a one-time code from `POST /v1/admin/pairing` ([`COMPANION_API.md`](COMPANION_API.md)); the USB route stays: `POST /v1/admin/devices` issues the token once and `scripts/provision.ps1` loads it ([`PROVISIONING.md`](PROVISIONING.md)) | The backend side exists; the firmware has no on-device pairing flow yet, so provisioning over USB is what the device uses |
| `GET /v1/hosts`, `/hosts/{id}/health`, `/hosts/{id}/logs` | `GET /v1/hosts`, `GET /v1/hosts/{id}/health` ([`COMPANION_API.md`](COMPANION_API.md)) | Read-only health from mock adapters; **no logs route**; no firmware client |
| `GET /v1/deck/profiles` | `GET /v1/deck/profiles` and the routes in [`CONTROL_DECK.md`](CONTROL_DECK.md) | Mock executor only; no firmware client |
| `POST /v1/jobs` (macro, idempotency key) | none from the device. The AI run worker creates typed jobs; the device fetches them with `GET /v1/device/jobs` | **Direction reversed:** design = device asks the companion to run a macro; here = backend asks the device to run a typed radio operation |
| `GET /v1/jobs/{id}` | `POST /v1/jobs/{id}/status`, `POST /v1/jobs/{id}/result` (device reports); `GET /v1/runs/{id}` (run and job outcome) | The device reports; there is no job-status read for the device |
| `POST /v1/jobs/{id}/cancel` | `POST /v1/runs/{id}/cancel` | Cancels the AI run; queued jobs of it become `cancelled`; a running job finishes per its own contract |
| `GET /v1/jobs` (history) | `GET /v1/sessions/{id}/report` (Markdown or JSON) | History is per session and evidence-based |
| `POST /v1/ai/query` | `POST /v1/sessions/{id}/assistant/messages`, then poll `GET /v1/runs/{id}` | Asynchronous run with budgets; answer cites evidence; never fabricated when the provider is missing (`ai_unavailable`) |
| `GET /v1/home/devices`, `POST /v1/home/actions` | none | Smart Home not built |

Implemented but not in the design: `GET /v1/time` (clock hint), `POST /v1/sessions` and `.../events` (evidence
upload, idempotent by event id), `POST/GET /v1/admin/targets` (enrolled BLE targets), `GET /v1/device/jobs`.

Security properties that match the design's rules: encrypted, authenticated device transport (HTTPS with a private
development CA, per-device bearer token stored as a salted hash); HTTP only on loopback; server decides what may run
(typed operations, no client-supplied command templates); idempotent result submission; unknown outcome is kept,
never retried automatically for target-changing operations. Not covered: of the design's SQLite tables
(`jobs`, `macros`, `profiles`, `audit`) only `jobs` exists here, shaped by the job contract in `schemas/`; there are no
`macros`, `profiles` or `audit` tables. The store has `devices`, `targets`, `sessions`, `events`, `jobs`, `runs` and
`findings` instead.

## 4. Keyboard: logical actions and `InputMapper`

The firmware separates the physical keys from the logical actions, as plan section 4 asks
(`firmware/lib/oyster_core/include/oyster_core/input_map.h`, host-tested). **Every physical binding is a proposal
and UNVERIFIED** until tried on a Cardputer ADV; record the result in
[`HARDWARE_VERIFICATION.md`](HARDWARE_VERIFICATION.md) (HV-03, HV-04). **Settings > Key tester** shows the
physical keys of the last key change and the logical action they resolve to; that is how a binding is
confirmed or rejected.

| Design logical action | `InputMapper` event | Proposed physical binding (UNVERIFIED) |
| --- | --- | --- |
| `MOVE_UP/DOWN/LEFT/RIGHT` | `Up`, `Down`, `Left`, `Right` | `Fn` + `;` `.` `,` `/`, or the bare keys in menus |
| `SELECT` | `Select` | `Enter` |
| `BACK` | `Back` | `` ` `` or `Del` in navigation mode. The design suggests Back=Escape, but this keyboard has no plain Esc key (`Fn`+`` ` `` is Cancel) |
| `HOME` | `Home` | `Fn`+`Del` (press `Fn` first): the plan proposes Home=Fn+Backspace "only once validated" |
| `HELP` | `Help` | `?` (Shift+`/`), navigation mode only, as proposed in `input_map.h`. The Home screen also has a Help tile (8) as a visible fallback |
| `TEXT_INPUT` | `Char`, `Erase` | printable keys type, `Del` erases |
| `CANCEL` | `Cancel` | `Fn`+`` ` `` or `Ctrl`+`C`, from every screen including modals; also stops running radio work |
| `CONFIRM` | `Select` inside the confirm dialog | `Enter`; the dialog ignores keys pressed just before it appeared |
| `SHORTCUT_1..9` | no action of its own | number keys `1`-`8` on the Home screen only (the six modules, Settings, Help); Control Deck shortcuts do not exist |

The global invariants (Back closes a modal first, Home goes to the root, nothing traps the user) are implemented in
the screen controller; per-screen behaviour is in [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).

## 5. Reconciling the design constraints

The six non-negotiable constraints of plan section 0, and where this build stands:

| Constraint | Status |
| --- | --- |
| 1. No flashing, erasing or repartitioning until inventoried, backed up where possible and approved | **Not met by the build, partly enforced by the tools.** See below |
| 2. Original apps stay accessible; no fake placeholders | **Not met while Oyster is installed.** The Home tile "Original Apps" is greyed out and opens an explanatory "Not available in this build" screen; nothing imitates the originals |
| 3. The Cardputer is not a Linux computer | Consistent: no shell, no Linux code on the device. Linux tasks would go through a companion (not built) |
| 4. Do not claim onboard GPS | Consistent: there is no GPS code here and none is claimed |
| 5. Every visible action has a lifecycle; incomplete features visibly unavailable | Implemented operations have handlers and failure views; operations not built answer `unsupported` with a reason; unbuilt modules are greyed-out Home tiles that open a "Not available in this build" screen |
| 6. Security tools scoped to passive scanning and authorised diagnostics | Consistent, and narrower: passive Wi-Fi survey; BLE connections only to targets enrolled locally over USB; no cracking, sniffing, deauthentication or takeover ([`POCKET_CORE.md`](POCKET_CORE.md)) |

**Constraint 1 and the "coexistence first" decision.** The design's preferred route is Option A (OysterOS as an
application inside the existing launcher), with Option B (a verified multiboot or launcher feature) as fallback and
Option C (a separate firmware, only after approval, backup and a proven recovery path) last. **This build is Option
C.** It is a full-flash image with a custom partition table without OTA (`firmware/partitions_8mb_noota.csv`: `nvs`,
`phy_init`, one `factory` app of 5 MiB, `spiffs`, `coredump`). Uploading it replaces the partition table and the
application, so the factory firmware, the launcher and the original apps no longer start. A launcher-compatible app
build (Option A or B) is **not built and not verified**, and Gate P0 (open OysterOS from the existing menu and
return without loss) has not been attempted.

What the repository does to make Option C defensible:

- `scripts/firmware-backup.ps1` dumps the whole 8 MB flash with a size check, a chip-side hash check and a SHA-256
  file (esptool bundled in PlatformIO; nothing is downloaded);
- `scripts/firmware-flash.ps1` refuses to upload unless a verified backup exists for the connected chip's MAC;
  `-SkipBackupCheck` overrides it with a loud warning;
- [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md) step 0 puts the preservation audit
  ([`HARDWARE_AUDIT_TEMPLATE.md`](HARDWARE_AUDIT_TEMPLATE.md)) and the backup before the first flash;
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) describes the restore (`write_flash 0x0`) and the M5Burner route.

What it cannot do: the scripts cannot see whether the audit was done, they cannot prove a dump restores a working
device, and none of them has run against a real unit (HV-27, HV-28 are UNVERIFIED). A dump taken after the first
Oyster flash contains Oyster, not the original software.

## 6. Open decisions

1. **Coexistence.** Keep the full-flash build (Option C) as the product, or invest in a launcher-compatible build
   (Option A or B)? That needs the hardware audit first: which firmware and launcher the unit has, and whether it
   loads third-party apps.
2. **Hardware audit.** It has not been done. Until it is, the backup is the only protection.
3. **Next module.** The design's next step after the security tools is the companion with read-only Linux host
   health (its M4). The implementation's open work is the device side of assessment (our M4). Which first?
4. **Key bindings.** Confirm or change Home, Help and Back on the real keyboard (Settings > Key tester) before
   they are documented as final.
5. **Real AI run.** Supply a Groq key and model and run `oyster ai-check` and `scripts\demo-m1.ps1`; until then
   the AI path is proven only with a fake provider and mocked HTTP.
6. **Licence and art.** The original code does not declare a license yet ([`../LICENSE-NOTICES.md`](../LICENSE-NOTICES.md)).
   The art in `assets/` (module icons, logo, boot splash, banner) is AI-generated; see
   [`../assets/PROVENANCE.md`](../assets/PROVENANCE.md).
7. **CI.** The design roadmap (M8) lists linting and tests in CI; none is configured yet
   ([`CI_SUGGESTION.md`](CI_SUGGESTION.md)).
