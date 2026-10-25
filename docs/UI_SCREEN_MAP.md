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
├── 3 Linux        -> "not available in this build" [L10]
├── 4 Control Deck -> "not available in this build" [D10]
├── 5 Smart Home   -> "not available in this build" [SH10]
├── 6 Original Apps-> "not available in this build" [O10]
├── 7 Settings [P10] ── Backend, Brightness, Storage, Diagnostics, Sync, New session,
│                       Keyboard, Key tester [P11], About
└── 8 Help (bindings + hint for the screen it was opened from)
```

Differences from the design navigation: Home has two extra tiles (7 Settings, 8 Help) in a fourth row that
scrolls into view, because the design reaches Settings from Home and the Help key is not yet verified (the
tile is a visible fallback). Targets and Assess are Oyster Pocket screens for authorized BLE checks; they sit
under Security, next to the design's Wi-Fi / BLE / Findings entries.

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
| L10-L13 | Linux host selection, overview, services, logs | `Unavailable` (Linux) | none | unavailable on the device: the backend has read-only host health with mock adapters ([`COMPANION_API.md`](COMPANION_API.md)), no firmware client |
| D10, D11, D20-D22 | Control Deck profile, macro, confirm, running, history | `Unavailable` (Control Deck) | none | unavailable on the device: the backend side exists ([`CONTROL_DECK.md`](CONTROL_DECK.md), mock executor), the firmware screens do not |
| SH10-SH12 | Smart Home rooms, device, IR | `Unavailable` (Smart Home) | none | unavailable: no IR profiles, no Home Assistant |
| O10 | Original Apps entry | `Unavailable` (Original Apps) | none | unavailable: no verified launcher integration |
| P10 | Settings | `Settings` | `backend_short()`, brightness, `g_board.storage()` | implemented |
| P11 | Key diagnostics | `KeyTest` | raw `KeySnapshot` from `g_board.read_keys()` and the resolved `NavEvent`s | implemented (needs the device to be useful) |
| (HelpOverlay) | Help | `Help` | screen hint table + proposed bindings | implemented |
| - | Boot / board mismatch / USB setup | `Boot`, `BoardError`, `Provisioning` | `g_board.info()`, `g_provisioning` | Oyster Pocket only |
| - | Messages and sub-details | `Message`, `Detail` (Backend, Storage, Diagnostics, Keyboard, About, Last read) | builders in `app_controller.cpp` | Oyster Pocket only |

## Logical actions and proposed keys

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
| Shortcuts | `1`-`8` on Home only (advertised in the footer) | open a tile |

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
| No fake functional toggles | implemented (unavailable modules open an explanation, nothing else) |
| Long SSIDs / BLE names are safely clipped | implemented (`~` marks clipping; the detail view shows the full value) |
| Zero-scan results handled | implemented (empty-state text per list) |
| Clearly disabled state for unfinished modules | implemented (muted label, dimmed icon, "unavailable") |
| Legibility of fonts and colours | needs hardware (photographs) |
| Physical key bindings | needs hardware (key tester) |
