[OysterOS](../README.md) / [Docs](README.md) / UI simulator

# OysterOS UI simulator

A pixel-sized 240 x 135 desktop simulator of the OysterOS interface with a keyboard emulator, loading / error /
empty / disabled states, modal confirmations and automated navigation tests. Source:
[`simulator/`](../simulator/README.md), tests: [`tests/simulator/`](../tests/simulator), launcher:
`scripts/ui-sim.ps1`.

It runs on a PC without a Cardputer, without the backend and without a network; all data are built-in mock
fixtures, and it is checked against the firmware sources in this repository. What is exact and what is not:

| Aspect | State |
|---|---|
| Palette, spacing, row/tile/footer geometry, type cell sizes | read from `firmware/include/ui_theme.h` at start-up; the simulator refuses to start if parsing fails |
| 24 x 24 module icons | read from `firmware/src/assets_gen.cpp` (the firmware's own pixels) |
| Glyph bitmaps | **extracted from M5GFX** 0.2.34 (the pinned library): `AsciiFont8x16` and `Font0`, as committed in `oyster_ui_sim/glyphs.py` by `simulator/tools/gen_glyphs.py` |
| Widgets (status bar, list, tiles, footer, notice, progress, confirm, message, text entry) | line-by-line port of `firmware/src/ui/widgets.cpp`, drawn through the same 240 x 16 band, so clipping and tile overlap behave alike |
| Circles / rounded rectangles | ports of the M5GFX algorithms |
| Colours | RGB565 constants expanded to 8 bits by bit replication (panel `#192C3D` appears as `#182C39`, the quantisation the firmware sends) |
| Panel colour response, legibility on the physical display | not modelled: the frames are what the design and the firmware sources imply, not photographs of a Cardputer ADV |
| Physical key bindings | a port of the firmware's bindings (`input_map.cpp`); the Key tester (P11) shows them on the device |
| Linux (Hosts), Control Deck, Smart Home screens | implemented in the firmware (`firmware/src/modules/`); the simulator draws them from built-in mock data in both profiles, labelled `SIMULATED`, as an interpretation of the design wireframes. They are **not** read from the backend and are not the firmware's own texts or layouts |

## Running it

```powershell
.\scripts\ui-sim.ps1                                   # window, integer scale 4 (960 x 540), nearest neighbour
.\scripts\ui-sim.ps1 -Preview                          # also enables the Original Apps tile (its screen stays unavailable)
.\scripts\ui-sim.ps1 -Headless -Out .\out\ui           # 41 routes x 5 states x 2 profiles = 410 PNG + 2 contact sheets
.\scripts\ui-sim.ps1 -Script .\simulator\examples\navigation.keys.txt -Out .\out\nav
.\scripts\ui-sim.ps1 -Test                             # pytest tests\simulator (headless, about 3 s)
```

`python -m oyster_ui_sim --routes` and `--keys` print the tables below. Requirements: Python 3.12+, Pillow
(`simulator/requirements.txt`), tkinter for the window only.

## Profiles

- **firmware** (default): behaves like the firmware build described in [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md). Linux
  (Hosts), Control Deck and Smart Home are implemented in the firmware and open their screens here, drawn from mock
  data: L10 host picker with online/offline indicator, L11 overview, L12 services, L13 logs, D10 profile with
  `1: Server status` slots, D11 macro summary, D20 confirmation, D21 running job, D22 history, SH10 rooms, SH11
  devices, SH12 IR profiles. The mock data and the layouts are the simulator's interpretation of the design, not the
  firmware's output, so every frame shows the `SIMULATED` banner and the Home tiles say `mock data`. Original Apps
  is the one module that is unavailable in the build: its tile is greyed out with a dimmed icon and opens the
  "Not available in this build" screen (the firmware's text, checked by a test). Nothing there runs or connects.
- **preview** (`-Preview`, `Simulator(preview=True)`): the same screens, plus an enabled Original Apps tile whose
  screen (O10) still says "Original launcher integration unavailable in simulator", as the design requires.

The banner of the status bar is `SIMULATED` throughout, because every value is a fixture (`START FAILED` when the
"BLE did not start" fault is switched on). The right side of the status bar derives from the simulated connectivity
(`offline`, `connect`, `online`, `no-clock`, `cert-err`, `auth-err`, `init-err`), with the dot and the label the
firmware uses; the Oyster AI tile, Settings > Backend and the AI screens follow the same value. No label is fixed.

## Routes

Every screen ID of [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) ("Mockup screen IDs": 26 of them) has a route, plus the
Oyster Pocket screens that sit next to them (Saved Findings, Targets, Assess, GATT results, Scratchpad, Help, text
entries, message and generic confirmation, settings details): 41 routes. The table is data
(`oyster_ui_sim/routes.py`): id, title, parent (target of Back), kind (`menu`, `list`, `detail`, `progress`, `text`,
`confirm`, `message`), module, availability (as in the screen map), Enter targets, the job a route shows, the modal
flag; Home always goes to H01 and Help to the Help route. Item rows themselves come from the view builders, because
their text depends on mock data and state.

| Route | Title | Kind | Module | Availability | Back | Enter | Job |
|---|---|---|---|---|---|---|---|
| H01 | OYSTER OS | menu | home | implemented | (root) | security->S10, ai->A10, linux->L10, deck->D10, smart->SH10, original->O10, scratchpad->SC10, settings->P10, help->help | - |
| S10 | Security | menu | security | implemented | H01 | wifi->S11, ble->S21, findings->findings, targets->targets, assess->assess | - |
| S11 | Wi-Fi Survey | list | security | implemented | S10 | results->S12, room->room_entry | wifi |
| S12 | Wi-Fi results | list | security | implemented | S11 | detail->S13 | - |
| S13 | Access point | detail | security | implemented | S12 | - | - |
| S21 | Bluetooth Explorer | list | security | implemented | S10 | results->S22 | ble |
| S22 | BLE results | list | security | implemented | S21 | detail->S23 | - |
| S23 | BLE advertiser | detail | security | implemented | S22 | - | - |
| findings | Saved Findings | detail | security | implemented | S10 | - | - |
| targets | Targets (hint) | list | security | implemented | S10 | detail->msg, assess->assess | - |
| assess | Assess | list | security | implemented | S10 | gatt->gatt, msg->msg | gatt |
| gatt | GATT results | list | security | implemented | assess | - | - |
| A10 | Oyster AI | list | ai | implemented | H01 | ask->ask_entry, latest->A11 | - |
| ask_entry | Ask Oyster | text | ai | implemented | A10 | - | - |
| A11 | Ask Oyster run | message | ai | implemented | A10 | - | ai |
| A12 | Ask Oyster answer | message | ai | implemented | A10 | - | ai |
| L10 | Linux | list | linux | implemented | H01 | host->L11 | - |
| L11 | Linux host | list | linux | implemented | L10 | health->msg, services->L12, logs->L13 | - |
| L12 | Services | list | linux | implemented | L11 | detail->msg | - |
| L13 | Logs | message | linux | implemented | L11 | - | - |
| D10 | Control Deck | list | deck | implemented | H01 | macro->D11, history->D22 | - |
| D11 | Macro | message | deck | implemented | D10 | confirm->D20, run->D21 | - |
| D20 | Confirm macro | confirm (modal) | deck | implemented | D11 | - | - |
| D21 | Job | progress | deck | implemented | D11 | - | deck |
| D22 | Job history | list | deck | implemented | D10 | - | - |
| SH10 | Rooms | list | smart | implemented | H01 | room->SH11 | - |
| SH11 | Devices | list | smart | implemented | SH10 | ir->SH12 | - |
| SH12 | IR profiles | list | smart | implemented | SH11 | - | ir |
| O10 | Original Apps | message | original | unavailable | H01 | - | - |
| SC10 | Scratchpad | message | scratchpad | implemented | H01 | - | - |
| P10 | Settings | list | settings | implemented | H01 | backend, storage, diagnostics, keyboard, keytest->P11, about (settings_*) | - |
| P11 | Key tester [P11] | detail | settings | implemented | P10 | - | - |
| settings_backend, settings_storage, settings_diag, settings_keyboard, settings_about | detail screens | detail | settings | implemented | P10 | - | - |
| room_entry | Room label | text | security | implemented | S11 | - | - |
| help | Help | message | system | implemented | previous | - | - |
| msg | Message | message | system | implemented | previous | - | - |
| confirm | Confirm action | confirm (modal) | system | implemented | A12 | - | - |

"Availability" is the column of [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md); a test parses that document and fails if the
simulator disagrees about any design ID.

## States

Every route supports `normal`, `loading`, `error`, `empty` and `disabled` (the Back chain and the key behaviour are the
same in each). A state arises naturally (a task running, the backend offline, a scan that found nothing, an unavailable
module) or is forced (`goto <route> <state>` in a script, the State box of the window, `Simulator.goto(route, state)`).

| State | What is drawn |
|---|---|
| normal | the screen's content from mock data |
| loading | the `IN PROGRESS` view (label, elapsed seconds from the simulation clock, item count, activity bar, "Cancel: Fn+` or Ctrl+C"); a running task also shows its label and `Fn+`:Cancel` in the footer of every screen |
| error | an alert heading (error colour) with the cause; backend-bound screens name the connectivity state; Enter retries; Back and Home still work |
| empty | the list's empty text (`No networks seen (2.4 GHz)`, `Start a survey first`, `No advertisers seen`, ...); never a crash or an empty void |
| disabled | an unavailable or switched-off feature: muted tile/rows or the "Not available in this build" explanation; a disabled control never claims to work |

Jobs have their own failure outcomes (`failed`, `timed_out`, `cancelled`, deck jobs also `unknown`), set per job kind
with `Simulator.outcomes` / the window's "Next jobs fail" box / `outcome` in scripts.

## Navigation contract (Plan section 4)

Logical actions: `MOVE_UP/DOWN/LEFT/RIGHT`, `SELECT`, `BACK`, `HOME`, `HELP`, `CANCEL`, `CONFIRM` (plus `TEXT_INPUT`,
`ERASE`, `SHORTCUT`). The physical keys are mapped by a port of `input_map.cpp`.

- **Back** closes a modal first, otherwise pops the bounded stack (12 entries); on the root it does nothing.
- **Home** goes to H01 from every route and clears the stack, including from text entry; a modal is rejected first.
  *Documented exception, as in the firmware:* the Key tester (P11) only displays Home or Back; pressing either twice
  within 2 s leaves it. Cancel still works there.
- **Help** (`?`) opens the Help route for the current screen; it is not offered over a modal or in text entry (where `?`
  is a character); Back returns with the scroll position kept.
- **Cancel** (`Fn+`` ` or `Ctrl+C`) cancels the running task on every screen, rejects an open confirmation, leaves a
  text entry, and shows `CANCEL REQUESTED`.
- **Modal confirmation** (D20 for a macro, the generic dialog for a backend action): shown over the current screen with
  `Ent:Approve Del:Reject` and the remaining seconds; keys typed during the first **700 ms** (the firmware's guard) are
  ignored, Cancel is not guarded; Back or Home reject (never approve); it expires after its time-to-live instead of
  trapping the user. A rejected dialog executes nothing.
- A long task stays cancellable and visible: Back and Home leave its screen, the task continues, its label and
  `Fn+`:Cancel` stay in the footer until it ends.
- Lists keep the focused row visible while scrolling (selection wraps, `, /` page); long SSIDs and BLE names are clipped
  to the row with `~` and shown complete in the detail screen; non-ASCII names show `<non-ASCII>`, empty ones `<hidden>`.

### Keys

| Action | Physical keys | Scope | Desktop shortcut |
|---|---|---|---|
| MOVE_UP | ; (Fn+; anywhere) | bare in menus, Fn layer anywhere | Up arrow |
| MOVE_DOWN | . (Fn+. anywhere) | bare in menus, Fn layer anywhere | Down arrow |
| MOVE_LEFT | , (Fn+, anywhere) | page / switch column / adjust | Left arrow |
| MOVE_RIGHT | / (Fn+/ anywhere) | page / switch column / adjust | Right arrow |
| SELECT | Enter | open / run / approve | Return |
| BACK | ` or Del | navigation mode; a modal is answered first | Esc, Backspace |
| HOME | Fn+Del (Fn first) | any mode; a modal is rejected first | Home key |
| HELP | ? (Shift+/) | navigation mode; not in text entry or over a modal | F1 |
| CANCEL | Fn+` or Ctrl+C | any screen: cancels the task, rejects a modal | Ctrl+C |
| ERASE | Del | text entry; on an empty buffer it leaves | Backspace |
| TEXT_INPUT | printable keys | text entry (hotkeys in menus) | keyboard |
| SHORTCUT_1-9 | 1-9 | Home only, advertised in the footer | digits |

In the window, **Alt is Fn**, the legend is shown beside the screen and the logical actions are also buttons. A deck
slot number (D10) only opens the macro summary; it never runs anything (Plan 11.5).

## Replaying key sequences

`--script FILE` (or `-Script`) replays one command per line: `profile`, `conn`, `scan`, `fault`, `outcome`, `goto`,
`state`, `key <chord> ...`, `type <text>`, `wait <ms>`, `event confirm`, `shot <name>` and `expect route|modal|text|state
<value>`; a failed `expect` stops the replay with the line number and exit code 1. See
`simulator/examples/navigation.keys.txt` (NAV-01..03 and the key tester in 18 checks).

## How this maps to the acceptance criteria

The criteria: "Implement routes and design system documented in docs/DESIGN_SYSTEM.md. Navigation,
loading/error/disabled states, keyboard emulator, and automated NAV-01..03 tests. Must run without device or backend."

| Acceptance | Evidence (tests in `tests/simulator/`) |
|---|---|
| Routes documented in DESIGN_SYSTEM.md | `test_every_screen_id_of_the_design_system_has_a_route`, `test_every_route_renders_in_every_state[firmware/preview]`, `test_route_table_is_structurally_valid_and_acyclic` |
| Design system (tokens, spacing, type) | `test_palette_equals_the_design_tokens_of_the_design_system_document`, `test_layout_equals_the_spatial_sizing_of_the_design_system_document`, `test_theme_values_come_from_the_header_not_from_a_copy`, `test_theme_parsing_fails_loudly_*`, `test_cell_metrics_equal_the_firmware_fonts`, `test_body_glyphs_are_the_m5gfx_bitmaps`, `test_glyph_module_matches_the_pinned_m5gfx_source` |
| Pixel-sized 240 x 135, integer scale, no anti-aliasing | `test_frame_is_exactly_240_by_135_and_scales_by_integers`, `test_scaling_is_nearest_neighbour_without_anti_aliasing`, `test_only_palette_colours_are_drawn_on_text_screens`, `test_status_bar_heading_content_and_footer_regions`, `test_selected_row_has_stripe_marker_and_focus_background` |
| Loading / error / empty / disabled states | `test_every_route_renders_in_every_state`, `test_empty_state_shows_the_routes_empty_text`, `test_every_job_route_has_an_error_state_that_renders_as_an_alert`, `test_selecting_an_unfinished_module_explains_it_and_back_returns_home`, `test_the_loading_screen_shows_elapsed_time_from_the_simulation_clock` |
| Keyboard emulator, key legend, key diagnostics | `test_key_table_agrees_with_the_firmware_input_map_source`, `test_every_physical_chord_of_the_table_resolves_to_its_logical_action`, `test_key_tester_shows_keys_and_actions_and_needs_back_twice_to_leave`, `test_physical_keys_resolve_to_logical_actions_in_the_tester`, `test_desktop_window_translates_keys_and_renders_frames` |
| NAV-01 Back from a leaf returns to the parent | `test_nav_01_back_from_any_leaf_screen_returns_to_its_parent[firmware/preview]`, `test_nav_01_back_pops_the_overlays_to_the_screen_below` |
| NAV-02 Home returns to the root | `test_nav_02_home_from_any_screen_returns_to_the_root[firmware/preview]`, `test_nav_02_home_works_from_text_entry_and_from_a_running_task` |
| NAV-03 Back on a confirm modal dismisses it without execution | `test_nav_03_back_on_a_confirm_modal_dismisses_it_without_execution[firmware/preview]`, `test_modal_ignores_keys_for_700_ms_then_answers`, `test_rejecting_the_ai_pending_action_runs_nothing` |
| No action traps the user; tasks stay cancellable | `test_no_key_sequence_traps_the_user`, `test_a_modal_expires_instead_of_trapping_the_user`, `test_a_running_task_stays_visible_and_cancellable_on_every_screen`, `test_the_navigation_stack_is_bounded` |
| Runs without device or backend | every test is headless with mock fixtures and an injected clock; no network, no serial port, no backend import |
| Render determinism, speed | `test_render_is_deterministic_png_bytes_for_the_same_input`, `test_two_simulators_given_the_same_keys_render_identical_frames`, `test_headless_export_is_reproducible`, `test_a_screen_renders_in_under_50_ms` (about 25 ms for Home); the whole suite takes about 3 s |

### Design "UI test checklist"

| Item | Test |
|---|---|
| Every leaf route exits with Back | `test_nav_01_*` |
| Every route exits with Home | `test_nav_02_*` (key tester: twice, documented) |
| Scrolling never hides the focused item | `test_scrolling_never_hides_the_focused_list_item`, `test_scrolling_never_hides_the_focused_tile` |
| Text editor handles Backspace and Cancel | `test_text_entry_handles_typing_backspace_caret_and_cancel`, `test_text_entry_del_on_an_empty_buffer_leaves_and_enter_validates`, `test_cancel_also_leaves_the_room_label_editor`, `test_text_entry_is_bounded_and_filters_room_labels` |
| All jobs have a failure state | `test_wifi_survey_failure_is_reported_and_saves_nothing`, `test_ble_discovery_failure_is_reported`, `test_gatt_inspection_failure_is_reported`, `test_a_failed_ai_run_shows_status_failed_and_no_answer`, `test_deck_job_failure_timeout_and_unknown_are_shown_and_never_rerun`, `test_ir_failure_is_reported_and_success_never_claims_the_appliance_reacted` |
| Connected labels derive from actual connectivity | `test_status_bar_and_labels_follow_the_simulated_connectivity` (all 7 states, label and dot colour), `test_nothing_says_connected_unless_the_backend_is_online` |
| No fake functional toggles | `test_home_01_an_unavailable_device_is_not_toggled_and_no_success_is_shown`, `test_a_toggle_changes_state_only_through_an_acknowledged_change`, `test_unavailable_modules_have_no_controls_in_the_firmware_profile`, `test_brightness_is_the_only_toggle_and_it_changes_a_real_value` |
| Long SSIDs / BLE names are safely clipped | `test_long_ssids_and_ble_names_are_clipped_in_lists_and_complete_in_the_detail`, `test_ble_list_clips_long_names_and_falls_back_to_the_address`, `test_fit_clips_with_a_tilde_and_replaces_non_printable_characters` |
| Zero-scan results handled | `test_sec_02_no_access_points_shows_the_empty_state`, `test_no_discovery_and_zero_advertisers_each_have_their_own_empty_text`, `test_results_screens_before_any_scan_say_so` |
| Clearly disabled state for unfinished modules | `test_selecting_an_unfinished_module_explains_it_and_back_returns_home` |

Plan section 14 cases that do not need a device are covered too: SEC-01/02, NET-01, JOB-01, JOB-03 (status unknown is
reconciled with the same idempotency key, no second run), HOME-01, AI-01, ORIG-01.

### Agreement with the firmware sources

The tests parse the firmware and the design documents and fail on drift: `ui_theme.h` (tokens, geometry),
`UI_SCREEN_MAP.md` (implemented vs unavailable per design ID: `test_screen_map_and_simulator_agree_on_implemented_versus_unavailable_screens`),
`app_controller.cpp` (Home tiles, the "Not available in this build" texts, the static row labels),
`input_map.cpp` (arrow keys, Back, Help, Home and Cancel chords), `assets_gen.cpp` (icons) and the M5GFX font tables.

### Traceability (menu, route, action, result)

| Menu entry | Route | Action | Result view | Failure / empty view | Test |
|---|---|---|---|---|---|
| Home > Security > Wi-Fi Survey > Start | S11 | `start_wifi` (task, 4 s) | S12 results, then S13 detail | S11 error; S12 `No networks seen (2.4 GHz)` | `test_sec_01_*`, `test_sec_02_*` |
| Home > Security > Bluetooth Explorer | S21 | `start_ble` passive / active | S22 results, S23 detail | `BLE: no discovery yet`, `!Bluetooth: BLE off` | `test_ble_discovery_failure_is_reported` |
| Home > Oyster AI > Ask a question | A10, ask_entry | question (validated), A11, A12 | A12 answer pages | `!AI unavailable` (offline), `Status: failed` | `test_oyster_ai_refuses_*`, `test_a_failed_ai_run_*` |
| AI answer with a pending action | confirm modal | local confirmation | Inspect GATT runs only after approval | rejection runs nothing | `test_the_ai_pending_action_*` |
| Home > Linux (mock data) | L10, L11, L12, L13 | read-only, never confirmed | health, services, logs | `!Linux: offline`, offline host | `test_net_01_*`, `test_linux_health_*` |
| Home > Control Deck (mock data) | D10, D11, D20, D21, D22 | slot opens summary; Enter runs; D20 confirms | job id and final state | failed, timed out, unknown, cancelled | `test_job_01_*`, `test_job_03_*`, `test_deck_*` |
| Home > Smart Home (mock data) | SH10, SH11, SH12 | toggle only an available device; IR send | acknowledged state; `IR sent (no device feedback)` | `unavailable: not switched` | `test_home_01_*` |
| Home > Original Apps | O10 | none | explicit unavailable text | - | `test_orig_01_*` |
| Home > Settings > Key tester | P10, P11 | any key | physical key and logical action | - | `test_key_tester_*` |

## Limits of the simulator

- The headless frames are what the design and the firmware sources imply, not photographs: the colour response
  and legibility of the real display are not modelled (see the table at the top).
- The simulator re-implements the firmware's UI logic; it does not run the firmware code. A change in
  `app_controller.cpp` that the drift tests do not look at will not show up here until someone updates the simulator.
- Mock backends only: JOB-02 (unauthorized macro, audit entry), PRES-01/ORIG-02 (original-app preservation and switch)
  need a real backend or device and are not simulated. There is no voice or schedule screen.
- Linux (Hosts), Control Deck and Smart Home screens illustrate the design wireframes with invented fixtures: they
  are not the output of the firmware modules and not data from the backend. Their
  real texts and flows are in [`COMPANION_API.md`](COMPANION_API.md), [`CONTROL_DECK.md`](CONTROL_DECK.md) and
  [`HOME_ASSISTANT.md`](HOME_ASSISTANT.md), and the pages of the firmware are listed in
  [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).
- The Alt-as-Fn emulation in the window depends on how the desktop delivers Alt key events; the replay scripts and
  the on-screen buttons do not.
