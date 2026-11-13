[OysterOS](../README.md) / [Docs](README.md) / Screen map

# UI screen map: OysterOS design IDs to firmware screens

This table links the mockup screen IDs of the [UI/UX Design System](DESIGN_SYSTEM.md) (and the wireframes in
the [Engineering Plan](OysterOS_Project_Plan.md), section 11.4) to the `Screen` values of the Oyster Pocket
firmware (`firmware/include/app_controller.h`), the data each screen reads and its status.

**Nothing in this table has been checked on a Cardputer ADV yet.** "Implemented" means the screen is in the
firmware source, builds, and its handlers exist; colours, font sizes, legibility and every key binding still
need photographs and a key-tester run on the device (see [Hardware verification](HARDWARE_VERIFICATION.md)).

Sources: design tokens and layout constants in `firmware/include/ui_theme.h`, widgets in
`firmware/src/ui/widgets.cpp`, navigation and screens in `firmware/src/app_controller.cpp`, key mapping in
`firmware/lib/oyster_core/src/input_map.cpp` (host tests: `firmware/test/test_input`).

## Navigation tree (as built)

```text
Home [H01]  (2-column tile grid, numbers as in the H01 wireframe)
├── 1 Security [S10]
│   ├── Wi-Fi Survey [S11] ── Results [S12] ── Detail [S13]
│   ├── Bluetooth Explorer [S21] ── Results [S22] ── Detail [S23]
│   ├── Saved Findings (evidence log)
│   ├── Enrolled Targets ── Detail ── Assess
│   └── Assess (authorized GATT) ── GATT results ── Detail / Last read
├── 2 Oyster AI [A10] ── Ask (text entry) ── Run [A11 / A12]
├── 3 Linux (Hosts)  -> Module (Hosts) [L10] ── Overview [L11] ── Services [L12]; `l`: Logs page [L13]
├── 4 Control Deck   -> Module (Deck) [D10] ── Slots [D11] ── Confirm [D20] ── Result [D21]; History [D22]
├── 5 Smart Home     -> Module (SmartHome) [SH10] ── Devices [SH11] ── Commands [SH12]
├── 6 Original Apps-> "not available in this build" [O10]
├── 7 Settings [P10] ── Backend, Brightness, Storage, Diagnostics, Sync, New session,
│                       Keyboard, Key tester [P11], About
└── 8 Help (bindings + hint for the screen it was opened from)
```

Differences from the design navigation: Home has two extra tiles (7 Settings, 8 Help) in a fourth row that
scrolls into view, because the design reaches Settings from Home and the Help key is not yet verified (the
tile is a visible fallback). Targets and Assess are Oyster Pocket screens for authorized BLE checks; they sit
under Security, next to the design's Wi-Fi / BLE / Findings entries.

## Module pages (Home tiles 3 to 6)

Tiles 3 to 6 open `Screen::Module`: one screen whose content is the current page of a module
(`firmware/include/modules.h`, `firmware/src/modules/modules.cpp`). A page is a **List** (5 rows of at most 28
characters), a scrolling **Text**, or a **Confirm** page (operation, target, effect). Enter calls the module's
select handler, Back goes up one page (or rejects a confirmation), and the action strip shows `Key:Action` pairs
of at most 28 characters. Network calls run on the worker task (`RequestKind::Module`, through the generic
`BackendClient::call`), so the UI never waits for the network; a page shows `working` while a call runs. A
Confirm page ignores keys for the first 700 ms and is rejected by Back, Home and the emergency cancel. Each
module also reports a short status for its Home tile.

| Module | Source | Home tile text | Extra keys |
|---|---|---|---|
| Hosts (tile 3, "Linux") | `firmware/src/modules/hosts.cpp`, parsers `oyster_core/hosts_json.*` | `N hosts`, `no hosts`, `offline` | `r` refresh, `l` logs page |
| Control Deck (tile 4) | `firmware/src/modules/deck.cpp`, parsers `oyster_core/deck_json.*` | `N profiles`, `no profile`, `offline` | `r` reload, `1`-`9` run a slot |
| Smart Home (tile 5) | `firmware/src/modules/smart_home.cpp`, `ir_remote.cpp`, `oyster_core/ir_codes.*`, `oyster_core/home_json.*` | `IR+HA`, `IR only`, `offline` | `r` refresh, `1`-`6` send an IR key or run an action |

The first three modules are written, compiled and host-tested, and **none has run on a Cardputer ADV**.

## Screen table

| Design ID | Design screen | Firmware `Screen` | Data source | Status |
|---|---|---|---|---|
| H01 | Home | `MainMenu` (tile grid) | static module list; Oyster AI tile: `g_backend.state()`; Security tile: `g_radio.active()`; heading: startup failures, `s_cfg.provisioned` | implemented |
| S10 | Security module | `Security` (tile list) | counts from `g_results` (`refresh_counts`), `s_targets` | implemented |
| S11 | Wi-Fi scan | `WifiSurvey` (progress view while a survey runs) | `g_runner` request `WifiSurvey`; operation events from the worker queue | implemented (passive only) |
| S12 | Wi-Fi results | `WifiResults` | `g_results.wifi_rows()` (sorted by RSSI or channel) | implemented |
| S13 | Wi-Fi detail | `Detail` (access point) | `g_results.wifi_rows(index)` | implemented |
| S21 | BLE scan | `BleExplorer` (progress view while a discovery runs) | `g_runner` request `BleDiscover` | implemented (passive and active) |
| S22 | BLE results | `BleResults` | `g_results.ble_rows()` | implemented |
| S23 | BLE detail | `Detail` (BLE advertiser) | `g_results.ble_rows(index)` | implemented |
| (Security > Findings) | Saved findings | `Evidence` (title "Saved Findings") | `g_evidence` (sequence, accepted, skipped, dropped, storage) | implemented |
| (flow B, GATT on owned devices) | Authorized GATT inspection | `Targets`, `Assess`, `GattTree`, `Detail` | `g_config` targets, `g_runner` `InspectGatt` / `ReadValue`, `g_results.gatt_rows()` / `latest_read()` | implemented; M4 tests shown muted as "not in build" |
| A10 | AI compose | `AskOyster` menu + `TextEntry` | `g_backend.state()` (refused with an explanation when not connected) | implemented |
| A11 | AI working | `RunView` | `g_backend.latest_run()` status | implemented |
| A12 | AI answer | `RunView` (scrolling pages, line counter in the footer) | `g_backend.latest_run().answer_excerpt` | implemented |
| (dialog) | Confirmation of a backend job | `ConfirmDialog` (modal) | `g_runner.pending_confirmation()`, bound to job id + nonce | implemented (700 ms key guard; Back, Home and Cancel reject) |
| L10 | Linux host selection | `Module` (Hosts), List page | `GET /v1/hosts` via the worker task; `hosts_json` parser | implemented (written, unverified on the device); read-only; Enter opens a host; when the list is empty or the call failed, a text page says why (`No hosts shared with this device`, `offline`, ...) |
| L11 | Linux host overview | `Module` (Hosts), List page | `GET /v1/hosts/{id}/health` (CPU, memory, disk, uptime, state, age of the reading) | implemented (written, unverified on the device); Enter opens the services list, `r` refreshes, `l` opens the logs page |
| L12 | Linux services | `Module` (Hosts), List page | the `services` of the same health report | implemented (written, unverified on the device); Enter shows `name: state` as a notice, `No services listed` when none is shared |
| L13 | Linux logs | `Module` (Hosts), Text page | none (fixed text) | implemented (written, unverified on the device); says plainly that there is no log source, because the backend serves no logs route ([`COMPANION_API.md`](COMPANION_API.md)) |
| D10, D11 | Control Deck profiles and slots | `Module` (Deck), List pages | `GET /v1/deck/profiles?limit=5` (up to 5 per device page, paged), slots 1-9 from the profile; `*` after the number marks an impactful slot | implemented (written, unverified on the device); number keys `1`-`9` run a slot directly, each key press with a fresh Idempotency-Key and never auto-retried |
| D20 | Control Deck confirmation | `Module` (Deck), Confirm page | `POST /v1/deck/jobs/{id}/confirm` and `/cancel`; the nonce is bound to the job id and stays in RAM | implemented (written, unverified on the device); only for impactful slots; Enter approves, Back rejects, keys ignored for the first 700 ms, expiry 120 s |
| D21, D22 | Control Deck result and history | `Module` (Deck), Text and List pages | `GET /v1/deck/jobs/{id}` polled every 1.5 s; `GET /v1/deck/history?limit=10` | implemented (written, unverified on the device); the result page says `Status unknown` when an answer was lost, history shows the last 10 finished jobs ([`CONTROL_DECK.md`](CONTROL_DECK.md)) |
| SH10 | Smart Home rooms | `Module` (SmartHome), List page | `IR Remote` first (no backend), then `GET /v1/home/rooms` | implemented (written, unverified on the device); one muted status row when Home Assistant is not set up, loading or offline |
| SH11, SH12 | Smart Home devices and commands | `Module` (SmartHome), List pages and Confirm page | IR: five TV families on the IR LED (GPIO44, an assumption); Home Assistant: `GET /v1/home/rooms/{id}/devices`, `POST /v1/home/actions` | implemented (written, unverified on the device); IR sends Power, Vol+, Vol-, Mute, Ch+, Ch- and says `Sent`, never `done`; locks, covers and alarm panels ask first ([`HOME_ASSISTANT.md`](HOME_ASSISTANT.md)) |
| O10 | Original Apps entry | `Unavailable` (Original Apps) | none | unavailable: no verified launcher integration |
| P10 | Settings | `Settings` | `backend_short()`, brightness, `g_board.storage()` | implemented |
| P11 | Key diagnostics | `KeyTest` | raw `KeySnapshot` from `g_board.read_keys()` and the resolved `NavEvent`s | implemented (needs the device to be useful) |
| (HelpOverlay) | Help | `Help` | screen hint table + proposed bindings | implemented |
| - | Boot / board mismatch / USB setup | `Boot`, `BoardError`, `Provisioning` | `g_board.info()`, `g_provisioning` | Oyster Pocket only |
| - | Messages and sub-details | `Message`, `Detail` (Backend, Storage, Diagnostics, Keyboard, About, Last read) | builders in `app_controller.cpp` | Oyster Pocket only |

## Status LED

The WS2812 on the StampS3 module shows the state at a glance (brightness capped at 100/255, nothing faster
than one blink per second; it is written only when its colour changes and never from a task or timer). The
same table is on the device under Home > Help > Enter.

| LED | Meaning |
| --- | --- |
| 🟢 green, slow breathing | Online: the backend is reachable |
| 🔵 blue, breathing | Attention: a confirmation or a new AI answer waits for you |
| 🟠 amber, one blink every 2 s | Offline: not provisioned, or the backend cannot be reached |
| 🟠 amber, breathing | Connecting: joining Wi-Fi or reaching the backend |
| 🔴 red, one blink every second | Error: certificate, token, clock or start-up problem |
| 🩵 teal, breathing | Scanning: a Wi-Fi survey or a Bluetooth operation is running |
| ⚪ white, slow breathing | Boot: the start screen, waiting for any key |
| 🟢 green, two pulses | Announcement: an operation finished |
| 🔴 red, two long blinks | Announcement: an operation failed |
| 🔵 blue, three quick blinks | Announcement: a request or an answer arrived |

## Logical actions and proposed keys

The **Go button (G0)** is not a keyboard action: it covers the current screen with the logo screen (`Any key:Unlock`)
until any key is pressed, then shows the same screen again. The unlocking key is consumed, work in progress
continues behind it, and the emergency cancel chord still works. It is not offered on the boot, board-error and
USB-provisioning screens. The screen simulator does not model it.

All bindings are **proposed and UNVERIFIED**; Settings > Key tester shows the physical keys and the resolved
action so they can be confirmed and recorded in [Hardware verification](HARDWARE_VERIFICATION.md).

| Action | Proposed key | Behaviour |
|---|---|---|
| Up / Down | `;` `.` (bare in menus), `Fn` + same anywhere | move focus, scroll text |
| Left / Right | `,` `/` (bare in menus), `Fn` + same anywhere | page, switch tile column, adjust |
| Select | `Enter` | open / run |
| Back | `` ` `` or `Del` (navigation mode) | a confirm dialog is answered "reject" first, otherwise pops the stack |
| Home | `Fn`+`Del` (press `Fn` first), any mode | clears the stack to Home; a confirm dialog is rejected first (after its key guard) |
| Help | `?` (`Shift`+`/`), navigation mode | opens Help for the current screen; not over the confirm dialog or in text entry |
| Cancel | `Fn`+`` ` `` or `Ctrl`+`C`, any mode | cancels the running task on every screen, rejects an open confirmation |
| Shortcuts | `1`-`8` on Home (advertised in the footer) | open a tile |
| Module keys | `r` refresh or reload (Hosts, Control Deck, Smart Home), `l` logs page (Hosts), `1`-`9` run a slot (Control Deck slots), `1`-`6` send an IR key or run an action (Smart Home commands) | only on the module page that offers them; the action strip shows them |

Key tester exception: there every action is only displayed; Back or Home pressed twice within 2 s leaves,
Cancel still works.

## Design UI test checklist

| Item | State |
|---|---|
| Every leaf route exits with Back | implemented (stack pop on every screen; Key tester: Back twice) |
| Every route exits with Home | implemented (global Home; not on boot, board-mismatch and USB-setup screens) |
| Scrolling never hides the focused item | implemented (list and tile scrolling keep the selection visible) |
| Text editor handles Backspace and Cancel | implemented (`Del` erases, `Del` on empty leaves, Cancel / Home leave) |
| All jobs have a failure state | implemented for the existing operations (outcome toast, Evidence, Run view) |
| Connected labels derive from actual connectivity | implemented (status banner, connection dot, Oyster AI tile from the backend state) |
| No fake functional toggles | implemented (Original Apps opens an explanation, nothing else; the Hosts, Control Deck and Smart Home pages act only through typed backend calls or the IR LED, ask before impactful actions, and report `Sent` or the failure instead of a made-up success) |
| Long SSIDs / BLE names are safely clipped | implemented (`~` marks clipping; the detail view shows the full value) |
| Zero-scan results handled | implemented (empty-state text per list) |
| Clearly disabled state for unfinished modules | implemented (a module that reports itself unavailable gets a muted label, a dimmed icon and a message with the reason; Original Apps is the one that is unavailable in this build) |
| Legibility of fonts and colours | needs hardware (photographs) |
| Physical key bindings | needs hardware (key tester) |
