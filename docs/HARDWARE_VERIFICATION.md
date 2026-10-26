# Hardware verification checklist

Every item here is a **manual step - unverified until run**. No Cardputer has been attached to this
project yet, so every result starts as **UNVERIFIED**. Only a result you observed on the real
device may be changed to PASS. A compiler, the simulator or a prerecorded fixture does not count.

Result values: `PASS`, `FAIL`, `PARTIAL` (works with a stated limitation), `BLOCKED` (a prerequisite is
missing; name it), `UNVERIFIED` (not run yet).

## Before the first flash: audit and backup (HV-27, HV-28)

The Oyster firmware replaces the whole flash. Before HV-01 complete the preservation audit
([`HARDWARE_AUDIT_TEMPLATE.md`](HARDWARE_AUDIT_TEMPLATE.md)) and run HV-27 and HV-28 below. Nothing else on
this page may be flashed before that.

## Before you start: record the context

Fill this in once per test campaign. It is needed for any published demonstration.

| Item | Value |
| --- | --- |
| Date | |
| Firmware SHA-256 (printed by `.\scripts\firmware-build.ps1`) | |
| Firmware version (`OYSTER_FIRMWARE_VERSION`; the provisioning `HELLO` reply includes it) | |
| Toolchain | as in [`BUILD_MATRIX.md`](BUILD_MATRIX.md), or note differences |
| Backend PC, Windows version, Bluetooth adapter | |
| Wi-Fi network (2.4 GHz channel, router model; no passwords here) | |
| microSD card (brand, size, FAT32) | |
| Settings changed from defaults | |
| Known limitations during this run | |

Useful tools:

- `.\scripts\firmware-monitor.ps1 -Port COMx -Timestamps -SaveLog`: serial log with PC timestamps,
  saved to `firmware\platformio-device-monitor-*.log`.
- `.\scripts\fixture.ps1 check` / `run -Mode ...` / `reset-state` / `synth`: the live BLE fixture.
- On the device: **Settings** (backend state, storage, diagnostics, keyboard help, **Key tester**) and
  **Security > Saved Findings** (events, failures, upload state). The screen structure is in
  [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md).

## Results

| ID | Check | Result | Date | FW SHA-256 (first 12) | Evidence / notes |
| --- | --- | --- | --- | --- | --- |
| HV-01 | Board id is `board_M5CardputerADV` | UNVERIFIED | | | |
| HV-02 | Flash 8 MB, PSRAM not found, reset reason logged | UNVERIFIED | | | |
| HV-03 | Keyboard navigation mapping | UNVERIFIED | | | |
| HV-04 | Emergency cancel combos on every screen | UNVERIFIED | | | |
| HV-05 | Display: status bar, lists, progress, dialogs | UNVERIFIED | | | |
| HV-06 | SD card mounted (FAT32), session files created | UNVERIFIED | | | |
| HV-07 | SD card missing -> volatile mode shown | UNVERIFIED | | | |
| HV-08 | SD card full / write error -> visible state, no silent loss | UNVERIFIED | | | |
| HV-09 | Serial provisioning, secrets never echoed | UNVERIFIED | | | |
| HV-10 | TLS connect to backend (valid trust) | UNVERIFIED | | | |
| HV-11 | TLS negative: wrong CA / wrong IP / revoked token | UNVERIFIED | | | |
| HV-12 | Clock: serial seed, SNTP, `clock_quality` | UNVERIFIED | | | |
| HV-13 | Wi-Fi survey (2.4 GHz), room label, export | UNVERIFIED | | | |
| HV-14 | BLE discovery sees `OysterLab`, never auto-connects | UNVERIFIED | | | |
| HV-15 | GATT inspect of the lab service, completeness flag | UNVERIFIED | | | |
| HV-16 | Profile read of a public value | UNVERIFIED | | | |
| HV-17 | Cancel latency: UI ack (< 1 s target) and radio stop | UNVERIFIED | | | |
| HV-18 | Heap telemetry vs goals (64 KiB free / 32 KiB block) | UNVERIFIED | | | |
| HV-19 | 20 scan / connect / sync cycles | UNVERIFIED | | | |
| HV-20 | 60-minute mixed-use soak | UNVERIFIED | | | |
| HV-21 | Backend disconnected during work -> evidence kept, resync | UNVERIFIED | | | |
| HV-22 | AI provider outage -> `ai_unavailable`, nothing invented | UNVERIFIED | | | |
| HV-23 | Factory firmware restore with M5Burner (recovery route) | UNVERIFIED | | | |
| HV-24 | M4: access-policy checks vulnerable vs corrected | BLOCKED (M4 not built) | | | |
| HV-25 | M4: diagnostic transfer hash + power interruption recovery | BLOCKED (M4 not built) | | | |
| HV-26 | M4: local confirmation for the virtual-LED write | BLOCKED (M4 not built) | | | |
| HV-27 | Backup before the first flash: `firmware-backup.ps1` dumps 8 MB with verification; restoring it brings the original firmware back | PASS (dump); restore not tried | 2026-10-25 | n/a | 8,388,608 bytes, `esptool verify_flash` digest matched, valid bootloader and partition table (factory app at 0x10000). A first read at 460800 baud stalled at 19 % and was stopped (nothing written); the default is now 115200. Restore: see HV-23 |
| HV-28 | Flash gate: `firmware-flash.ps1` refuses without a verified backup of that chip, passes with one | PARTIAL | 2026-10-25 | 0cc446875ad8 | Passed with the verified backup and the upload hash-verified. The refusal path was not tried on the device |
| HV-29 | Boot splash drawn once at start-up, then the Home screen (heading `OYSTER OS`, tile grid, greyed-out tiles) | PARTIAL (owner-observed) | 2026-10-25 | 0cc446875ad8 | Boot, splash, Home and the four greyed-out tiles were seen on the real display and looked right; no photographs, no flicker/colour measurements. Oyster AI showed unavailable while the device was not yet provisioned |
| HV-30 | Start screen waits for any key; the G0 button shows the same logo screen as a lock, any key returns to the same screen | UNVERIFIED | | | |
| HV-31 | Hosts (Home 3): host list, overview (CPU, memory, disk, uptime, state, age), services, Logs page, tile text `N hosts` / `no hosts` / `offline`, failure texts | UNVERIFIED | | | |
| HV-32 | Control Deck (Home 4) on the device: profiles and slots, run a slot, impactful confirmation (approve and reject), 700 ms key guard, emergency cancel, lost-answer text, history | UNVERIFIED | | | |
| HV-33 | Control Deck on the real PC (`OYSTER_DECK_EXECUTOR=windows`): media keys, lock, Wake-on-LAN, service restart | UNVERIFIED | | | |
| HV-34 | Smart Home IR (Home 5 > IR Remote): IR LED on GPIO44, each TV family against the owner's TV, one frame per key | UNVERIFIED | | | |
| HV-35 | Smart Home with Home Assistant: unconfigured text first, then a real instance (rooms, devices, an action, impactful confirmation, failure paths) | UNVERIFIED | | | |
| HV-36 | Status LED: green breathing online, amber blink offline, blue while a confirmation or unread answer waits, teal during a scan, announcements on finish/fail/notice; Help > Enter shows the legend with matching colours | UNVERIFIED | | | |

## How to run each check

### HV-01 / HV-02 Board identity

Flash (`.\scripts\firmware-flash.ps1 -Port COMx`), open the monitor, and press the reset button or switch
the device off and on. The boot log and the boot event should report the M5Unified board id
`board_M5CardputerADV`, about 8 MB of flash, `psram_found: false`, and a reset reason. A board mismatch
must show an error screen instead of the menu. **Pass:** ADV id reported and the menu appears.

### HV-03 Keyboard mapping

The mapping below is **proposed** in `oyster_core/input_map.h` and only host-tested. Try every row. In
the Notes column, record the mapping that actually works, then update `input_map.h` if needed. Use
**Settings > Key tester** to see what the firmware receives: it shows the physical keys of the last key
change and the logical action they resolve to, and it only displays the action (nothing is opened or run).
Pressing Back or Home twice within 2 s leaves the tester; Cancel still works there.

| Action | Proposed keys | Where to try |
| --- | --- | --- |
| Up / Down / Left / Right | `;` `.` `,` `/` (bare in menus), `Fn` + same keys anywhere | Home tiles, lists |
| Select | `Enter` | menus |
| Back | `` ` `` or `Del` in navigation mode (this keyboard has no plain `Esc` key; `Fn`+`` ` `` is Cancel) | any submenu |
| Home | `Fn`+`Del` (press `Fn` first), any mode. **PROPOSED, UNVERIFIED** | from a screen several levels deep, from a text entry, and with a confirm dialog open (it is rejected first, after its short key guard) |
| Help | `?` (`Shift`+`/`), navigation mode only. **PROPOSED, UNVERIFIED** | any list or detail; it must not open over the confirm dialog or in a text entry |
| Home shortcuts | `1` to `8` on the Home screen only (advertised in the footer) | Home |
| Type text | printable keys; `Del` erases and leaves when the field is empty; `Enter` confirms | room label (Security > Wi-Fi Survey > Room), Oyster AI > Ask a question |
| Arrows while typing | `Fn` + `;` `.` `,` `/` | text entry |
| Key tester | Settings > Key tester (screen P11): keys and resolved action shown; Back or Home twice leaves | Settings |

**Pass:** every action works and there are no stuck or duplicated keys. Mark Home and Help PASS only
after you have seen them work on the device; until then they stay **UNVERIFIED** and are described as
proposed everywhere. If a chord does not resolve, record the keys the tester did show and change
`input_map.h`.

### HV-04 Emergency cancel

Proposed combos: **`Fn+`` ` ``** (the firmware reports it as fn+esc; there is no plain Esc key) and **`Ctrl+C`**. Each must work on every screen, including
text entry and during a running operation, without opening another menu first. A cancel must not also
type a character or move the selection. **Pass:** all three combos cancel from every screen tried. List
the screens you tried.

### HV-05 Display

Check the status bar (mode banner, session id, backend state, battery), paginated lists, the progress view
(label, elapsed seconds, count, cancel hint), and the confirm dialog. Look for clipped text, flicker and
unreadable colours. The screens are drawn with one 240x16 sprite (7,680 B of RAM) band by band, so also
look for visible banding or tearing while a screen is redrawn. Take photographs of the main screens (Home,
Security, a list, a detail view, the progress view, the confirm dialog) and judge font size and the
palette against [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md); the sizes in `firmware/include/ui_theme.h` have not
been compared with a real display.

The banner shows the first value that applies: `LIVE RADIO` (during a survey, discovery or BLE session),
`START FAILED` (a module failed at boot; Settings > Diagnostics lists it and dependent actions are refused
with a message), `AI CONNECTED` (only while the latest AI run actually used a provider: `running`,
`awaiting_*`, `succeeded` or `budget_exhausted`), `BACKEND OK` (backend reachable and authenticated), then
`OFFLINE`. `SIMULATED` must never appear on the device. Note which banners you saw and when.

**Pass:** everything is readable at 240x135, the mode label is always visible, and each banner matched the
situation.

### HV-06 / HV-07 / HV-08 microSD

1. **Mounted:** switch off, insert a FAT32 card, and boot. Start a session and run a survey. Switch off, put
   the card in the PC, and check that `/oyster/sessions/<id>/events.jsonl` (one JSON object per line),
   `manifest.json` and `wifi.csv` exist and contain no token or Wi-Fi password.
2. **Missing:** boot without a card. The device must show reduced persistence (volatile mode) and
   still work. The session's events then live only in the 8 KiB RAM ring and are lost on reboot.
3. **Full / write error:** use a nearly full small card, or remove the card during a survey (only with
   test data). The device must show a full or write-error state. It must not claim the events were saved.
   After re-inserting the card, the last valid events must still be readable (a truncated last line is
   tolerated).

### HV-09 Serial provisioning

Close the monitor and run `.\scripts\provision.ps1 -Port COMx ...` ([`PROVISIONING.md`](PROVISIONING.md)).
Check that each reply names the field only (`OK token`, `OK wifi`, ...), that `CA-BEGIN`/`CA-LINE` reply
nothing and `OK ca <bytes>` follows `CA-END`, that `STATUS` shows no secret values, that `DONE` reboots,
and that the settings survive a power cycle. `SET-TIME` is staged: `STATUS` keeps showing the live
`clock_quality` until `DONE` applies it. Also try `ABORT` and the 60-second idle timeout (both discard the
staged time too), and optionally `FACTORY-RESET` (takes effect immediately; then provision again).
**Fail** if any secret appears in the serial log.

### HV-10 / HV-11 TLS connection

1. With a correct CA, IP and token, the Settings screen should reach `online` (`Connected`), the banner
   should show `BACKEND OK`, and the backend should show the device's session.
2. Negative tests (each must fail visibly, and there must be no way to skip verification):
   - certificate for a different IP (`-MakeDevCaIp` with a wrong IP), expect `cert-err`;
   - CA from another `make-dev-ca` run, expect `cert-err`;
   - re-enrol the device id (`oyster enroll-device --id <device-id> --write-token <file>` revokes the
     provisioned token), expect `auth-err`. Then provision again to recover.
   A certificate failure must always show as `cert-err`, never as a clock problem. Certificate validity
   dates are not checked by the device (see [ADR-003](adr/ADR-003-transport-tls.md)), so an expired
   certificate is not a meaningful negative test here.
3. Record the handshake time if the log shows it.

### HV-12 Clock

After provisioning (the serial seed is applied at `DONE`), events should carry `clock_quality: serial`.
Once SNTP succeeds they should carry `ntp`. With no time source, the device should show `no-clock`
(`ClockNotSet`: a firmware policy, it sends no authenticated request until it knows the time; the serial
log says "clock not set (firmware policy)") and record `timestamp_utc: null` with an uptime.

### HV-13 Wi-Fi survey

Run a survey from **Security > Wi-Fi Survey** in a room and save a room label (the **Room** row of the
same screen). Check: AP count, sorting by RSSI and by
channel, details of one AP, hidden SSIDs shown as hidden, the overflow indicator above 32 APs, the scan
mode recorded (passive or active), and the `wifi.csv` export. Only 2.4 GHz networks can appear. If
5 GHz networks are missing from the list, that is the expected result. In `wifi.csv`, `survey_started_ms`
(device uptime), `exported_at_utc` and `export_clock_quality` say when the rows were written, not when each
AP was observed. `observed_ap_country` in the summary event is a neighbour AP's advertised country, not
this device's regulatory domain.

### HV-14 / HV-15 / HV-16 BLE discovery, GATT, read

Start `.\scripts\fixture.ps1 run -Mode corrected` on the PC, after `fixture.ps1 check` says ready.

- Discovery: `OysterLab` appears with RSSI and the service UUID, and no connection is made (the
  fixture log shows no connection during discovery).
- Select the enrolled target (**Security > Enrolled Targets**, then `a` to assess it; **Security > Assess**
  also works) and inspect GATT. The evidence `connection` object should show
  `identity_ok: true` (the device's first action was the public `device_info` read). The lab service shows
  the 10 characteristics from [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md), and the completeness flag is true.
- Read `public_sensor` and `device_info` (`oyster-lab-v1;1`).
- Fail-closed checks (see [`BLE_LAB_PROFILE.md`](BLE_LAB_PROFILE.md#8-target-identity-and-remote-reads-on-the-cardputer)):
  with no enrolled address and two devices matching (for example a second advertiser with the name
  `OysterLab`), the device must refuse with "ambiguous target ... enrol an address"; a peer whose
  `device_info` is not exactly `oyster-lab-v1;1` must be disconnected with nothing else read and no bond
  stored.

### HV-17 Cancel latency

Start a long Wi-Fi survey or BLE discovery and press cancel. Measure separately:

- **UI acknowledgement** (target under 1 s): a phone video at 30 or 60 fps of the screen gives a
  reasonable measurement.
- **Radio stop latency**: from the `-Timestamps` serial log or the event timestamps, if this build logs
  the cancel request and the phase release. If it does not, write "not measurable with this build"
  instead of guessing.

Repeat five times and record the minimum, median and maximum.

### HV-18 Heap telemetry

The boot event and the periodic telemetry event (every 60 s) report free heap, minimum free heap,
largest free block, task stack high-water marks, queue occupancy and dropped events. Record the
minimum values during: idle, Wi-Fi survey, BLE discovery, connected GATT session, and uplink sync.
**Goals** (reported, never assumed): minimum free heap at least 64 KiB, largest free block at least
32 KiB. If the goals are not met, record the measured values. They become the documented budget.

### HV-19 Twenty cycles

Repeat survey -> discovery -> connect + inspect -> sync 20 times. Record resets (reset reason), BLE
disconnects, failed uploads, dropped events, and the lowest heap seen.

### HV-20 Sixty-minute soak

Run mixed use for 60 minutes with the monitor saving the log (`-Timestamps -SaveLog`). Record resets,
disconnects, dropped events, heap trend (does the minimum keep falling?), and battery drop.

### HV-21 Backend disconnect

During a survey or GATT session, stop the backend (Ctrl+C in its window). The device must finish or
stop the current operation, keep the evidence on SD, show `OFFLINE`, accept no new remote actions,
and upload the queued events after the backend returns, with no duplicates on the backend. This later sync
works only within the same session, before a reboot. Events of an earlier session (ended by New session,
reboot or power loss) are not uploaded by this firmware (milestone M6); check that they are still on the
card and that the new session has a recovery event with `sync_status` `not_uploaded_milestone_m6`.

### HV-22 Provider outage

Set a wrong `AI_API_KEY` (or block the network) and ask a question from **Oyster AI > Ask a question**. The run must end
as `ai_unavailable` or `failed` with a visible message. There must be no invented answer and no switch to
another provider.

### HV-23 Factory recovery

Optional, but it proves you can recover without your own backup. Restore the factory firmware with M5Burner
([`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-the-factory-firmware-m5burner)), then re-flash Oyster
with `.\scripts\firmware-flash.ps1` (it accepts the backup you made earlier for this chip). Restoring from
your own backup is HV-27.

### HV-27 Backup and restore of the original flash

Do this first, while the device still runs its original firmware. Complete the preservation audit
([`HARDWARE_AUDIT_TEMPLATE.md`](HARDWARE_AUDIT_TEMPLATE.md)), close every serial monitor, then:

1. `.\scripts\firmware-backup.ps1 -Port COMx -DryRun` shows the commands. Then run it without `-DryRun` (add
   `-VerifyByReread` if you want a second full read).
2. **Pass (backup):** the script ends with "Backup finished" and `_backups\<mac>\<stamp>\` holds
   `factory-8MB.bin` (8,388,608 bytes), `factory-8MB.bin.sha256` and `backup.json`; the chip check, the size check
   and `esptool verify_flash` all passed; the device restarts normally. Note the chip, MAC and esptool version
   from `backup.json` (no passwords here) and whether the first-byte warning appeared.
3. **Restore test (strongly recommended, but only once you accept losing what is on the device at that
   moment):** flash Oyster (HV-28), then restore the backup following
   [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md#restoring-your-own-backup-esptool). **Pass (restore):**
   `write_flash` and `verify_flash` succeed and, after a power cycle, the original launcher and apps start as
   they did before. Record what you see. The dump can contain Wi-Fi credentials or keys: do not attach it.

### HV-28 Flash gate

1. With no backup for this chip (rename the `_backups\<mac>` folder), `.\scripts\firmware-flash.ps1 -Port COMx`
   must print `REFUSING TO FLASH`, show the backup command and flash nothing.
2. With the backup in place it must print "Backup gate passed" with the MAC, the backup folder and the SHA-256,
   and then upload.
3. After changing one byte of a copy of the dump or of its `.sha256` file, the gate must refuse again.
4. `-SkipBackupCheck` must print the red warning and wait 5 seconds before uploading. Only try it with a device
   whose original software you do not need.

### HV-29 Boot splash and Home screen

The splash and the Home screen follow the project's design system ([`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md));
the structure is in [`UI_SCREEN_MAP.md`](UI_SCREEN_MAP.md). Judge them on the real display and photograph
them.

1. Switch the device off and on with the serial monitor open. A boot splash (the oyster and pearl art) is
   drawn once at start-up, before the first screen; short `Starting: <step>` messages may follow while the
   modules start. When start-up is done the footer reads `Any key:Start` and the logo stays up, with the slow
   mascot blink, until a key is pressed (USB provisioning is still served meanwhile). Look for flicker, tearing,
   wrong colours (the art was converted to RGB565 on a PC), and check that the key that starts the system does
   not also act on Home.
2. The first screen is Home, headed `OYSTER OS`, with a two-column tile grid: Security, Oyster AI and Linux
   on the left; Control Deck, Smart Home and Original Apps on the right; Settings and Help in a fourth row
   that scrolls into view. Check icons, labels, the selection stripe and the number shortcuts `1` to `8`.
3. Open Original Apps. In the default build the tile reads `not set up` and opens a page that says what is
   missing and which script sets it up (dual boot, docs/DUAL_BOOT.md); Back leaves, and nothing may pretend to
   boot or connect. (The Linux, Control Deck and Smart Home tiles used to be greyed out too; they now open
   module pages, checked in HV-31 to HV-35. The owner observation recorded for HV-29 was made on a build in which
   all four were greyed out.)
4. Check the Oyster AI tile against the backend state (connected, or unavailable with a reason).

**Pass:** the splash appears without artefacts, waits for a key, Home is readable at 240x135, and the one unbuilt
module (Original Apps) is visibly unavailable. Record anything clipped or hard to read, with photographs.


### HV-30 Start screen and G0 lock

1. After start-up the logo stays on screen with `Any key:Start` and the slow mascot blink. Press a key: Home
   opens, and that key must not also select or move anything on Home.
2. Open a screen (for example Security), then press the **G0** button once. The logo screen appears with
   `Any key:Unlock`. Press any key: the screen you left returns unchanged, and that key did not act on it.
3. Lock while an operation runs (a Wi-Fi survey): it keeps running behind the logo; after unlocking the
   progress or result is shown. Lock while the keyboard is in text entry: the text is still there afterwards.
4. Emergency cancel while locked: press Ctrl+C (or Fn+Esc). The footer reads `Cancelled`; the operation stops.
5. G0 held while resetting or powering up puts the chip into download mode (dark screen): that is the hardware,
   not a bug. Do not hold it at power-on.

**Pass:** all of the above behave as described; note anything unclear. The lock is a screen cover, not an
authentication: any key opens it.

### HV-31 Hosts

Home tile 3 ("Linux", the Hosts module, screens L10-L13). Written and host-tested (28 parser and text cases), never
run on a Cardputer. Read-only: nothing is run on any host.

Prerequisites: the backend runs (`.\scripts\backend.ps1`), the device is provisioned and online (HV-10), and the
backend PC is enrolled as a host that this device may see, for example
`python -m oyster.cli seed-demo --id <device-id> --no-profile` (adapter `local`; add `--services <name>` with the
name of a Windows service on your PC to test the services page). Details: [`COMPANION_API.md`](COMPANION_API.md).

1. **Tile.** Home tile 3 reads `1 host` (or `N hosts`) once the backend has answered. With no host shared with this
   device it reads `no hosts`; with the backend stopped or the device not configured it reads `offline` (or
   `error` if the backend refused the list). It must never show a count it did not receive.
2. **List and overview.** Enter on the tile opens the host list; Enter on a host opens the overview: CPU %, memory %,
   disk %, uptime, `State ok, N s ago`. Compare the numbers with Task Manager on the backend PC (same order of
   magnitude, not identical: they are samples). Wait ten seconds and press `r`: the age restarts and the values
   update.
3. **Services.** With a service enrolled, Enter on the overview opens the services list; Enter on a row shows
   `name: running` (or `inactive`, `unknown`) and agrees with the Windows service list. Without services the
   overview says `No services listed`.
4. **Logs.** `l` on the overview opens a page that says `No log source.` and that only health is shared; there is no
   log text and no input. Back returns.
5. **Back.** Back goes up exactly one level (services > overview > host list > Home) and never exits the module
   from a deeper level.
6. **Failure texts.** On the overview stop the backend and press `r`: a plain text names the problem (offline,
   connection lost or backend error) and Back still works; restart the backend and `r` recovers. Share no host with
   the device: the list page says `No hosts shared with this device`. On Windows the load average does not exist: if
   `load_1m` is enrolled, the overview names it as missing and the state reads `partial`.

**Pass:** every page above shows what the backend reports (no invented value), each failure is a clear text rather
than a hang, no key runs anything on a host. Record failures with the text that was shown.

### HV-32 Control Deck on the device

Home tile 4 (screens D10, D11, D20, D21, D22). Written and host-tested (35 parser and text cases; 486 backend tests),
never run on a Cardputer. Run this first with the default `OYSTER_DECK_EXECUTOR=mock`: nothing is executed on the PC,
and every result says so. The real PC is HV-33.

Prerequisites: backend running, device online, and the seed profile for the device:
`python -m oyster.cli seed-demo --id <device-id>` (profile `desk`: 1 PC health, 2 Play/Pause, 3 Next, 4 Prev,
5 Vol+, 6 Vol-, 7 Mute, 8 Lock PC). Details and states: [`CONTROL_DECK.md`](CONTROL_DECK.md).

1. **Profiles and slots.** The tile shows the number of profiles (`no profile` when none, `offline` when the backend
   cannot be reached). Enter opens the profile list (Desk, then History); Enter on Desk lists slots `1` to `8`; only
   slot 8 has the `*` that marks an impactful slot.
2. **Run a slot.** Press `2`. A working page appears, then the result page with state `succeeded` and, for the mock
   executor, `Mock executor: nothing was run on the PC.` The owner history on the backend
   (`GET /v1/deck/history`, loopback) lists one job. A second press of `2` makes a second job (a fresh
   Idempotency-Key per key press); neither press is retried by the device.
3. **Confirm.** Press `8`. A confirmation page names `Lock PC`, the target `this PC` and the effect. Press Enter
   right away (within 0.7 s of the page appearing) or press other keys: the page must not answer. After a second,
   Enter approves: the result is `succeeded` (mock) and the backend job passed `awaiting_confirmation` ->
   `running` with a nonce it did not log.
4. **Reject.** Press `8`, wait a second, press Back: the job is `cancelled` on the backend and nothing ran. Repeat
   with Home (`Fn`+`Del`) and with the emergency cancel (`Fn`+`` ` `` or `Ctrl`+`C`): each must reject, never
   approve.
5. **Expiry.** Press `8` and leave the page alone: the page counts down and after about 117 s (the backend's 120 s
   window minus 3 s) it rejects by itself. The backend job ends `cancelled` (or `expired` at 120 s if the
   cancellation could not be delivered) and nothing ran; the device never approves by default.
6. **Lost answer.** On the working page press Back: the device stops waiting, and History shows the job exactly once
   (the action may have run). If you can cut the connection while the working page shows (for example switch the
   access point off for a second), the result page must say `Status unknown`; Enter then repeats the same request with
   the same key, and the backend must end up with one job, not two. An `unknown_outcome` job is never retried by the
   device.
7. **History and failures.** History lists the last 10 finished jobs, newest first; Enter opens one in the result
   page. With the backend stopped, pressing a slot says nothing was sent. A rejected device token (re-enrolled
   device) says so.

**Pass:** a non-impactful slot runs at once, an impactful slot never runs without the on-device approval, keys in
the first 0.7 s never answer the page, Back, Home and the emergency cancel reject, an expired page runs nothing,
and a lost answer never creates a second job.

### HV-33 Control Deck on the real PC

The backend part of the Control Deck acting on the PC it runs on (`executor_windows.py`). Only read-only probes have
run on a real PC (host health, service state, a service name that does not exist); **media keys, the lock,
Wake-on-LAN and service restart have only run against a fake OS layer.** Set `OYSTER_DECK_EXECUTOR=windows` in
`backend/.env` and start the backend yourself in your own signed-in session, not elevated and not as a service
(otherwise Windows reports `input_blocked`). Variables: `.env.example`.

1. **Health.** Slot 1 returns a summary such as `cpu 12% mem 63% disk 71%` that agrees with Task Manager. Jobs are
   labelled `executor: windows`.
2. **Media keys.** With any media player playing: slot 2 pauses and resumes it, slots 3 and 4 skip, slots 5 and 6
   change the volume by one Windows step (about 2 %) per press, slot 7 mutes and unmutes. The job text is
   `key sent; player reaction not checked`: which player reacts is Windows' choice; record which one did.
3. **Lock.** Slot 8 asks on the Cardputer first; after approval the PC locks. Unlock it; the job is `succeeded`
   (`lock request accepted by Windows`). Without approval nothing locks.
4. **Wake-on-LAN (optional).** Set `OYSTER_DECK_WOL_HOSTS=<host_id>=<MAC>` for a machine you own that is switched
   off with Wake-on-LAN enabled, and define a profile slot with a `wake_on_lan` action for that host (owner route
   `PUT /v1/deck/profiles/{id}`). After approval the machine should start; the job text only says
   `magic packet sent; wake-up not verified`.
5. **Service restart (optional).** Only for a harmless service of your own, listed in
   `OYSTER_DECK_RESTART_SERVICES` and in the profile. Not elevated, the job must end `failed` with
   `needs administrator rights; nothing was restarted`; with the backend started elevated by you, the service
   restarts, or the job ends `unknown_outcome` if a state was not reached in time.

**Pass:** each action above does what is written or fails with the stated text; nothing runs for a host, key or
service that is not allowlisted. Mark each action PASS, FAIL or BLOCKED separately in the notes.

### HV-34 Smart Home: IR remote

Home tile 5 > `IR Remote` (screens SH10-SH12). Written and host-tested (24 cases, shared with the Home Assistant
parsers); **no IR code has been tried on a real TV and the IR LED pin is an assumption.** Needs no backend.

1. **Rooms and families.** Tile 5 opens the rooms list with `IR Remote` first; Enter lists `TV Samsung`, `TV LG`,
   `TV Sony`, `TV NEC (Toshiba type)`, `TV Philips (RC5)` and `About IR codes`; Enter on a family lists `1 Power`,
   `2 Vol+`, `3 Vol-`, `4 Mute`, `5 Ch+`, `6 Ch-`.
2. **LED pin (GPIO44).** Point the Cardputer's IR LED at a phone camera (some front cameras filter infrared; try the
   rear one) and press Power: the LED should flicker while the strip says `sending`. No light: GPIO44 is not the
   LED on this unit (`ir::kTxPin` in `ir_remote.h`); stop here and record it, because no family can work then.
3. **Each family.** Against the owner's TV, try Power, Vol+, Vol-, Mute, Ch+ and Ch- on the family that matches the
   brand, then on the others. Record per family and key: reacted, did not react, or reacted wrongly. A family that
   does not react needs its own code row (`kBrands` in `ir_codes.cpp`).
4. **One frame per key.** One key press sends one burst: the TV toggles power once and moves volume one step per
   press; holding Enter or a number key does not repeat. On a phone video the LED shows one burst of about 70 ms
   (about 140 ms for the Sony family, which sends three frames).
5. **Honest text.** The notice reads `Sent: Power (IR, no reply)`, never `done`, and no state of the TV is shown. While
   a Wi-Fi survey runs, a key press says `Radio busy; try again` and sends nothing. `1`-`6` send the same frames as
   the rows.

**Pass:** the LED flickers on a key press, at least one family drives the owner's TV (state which keys), one press is
one burst, and the screen never claims more than `Sent`. A family that never reacts stays UNVERIFIED or FAIL for that
TV; it does not make the module PASS by itself.

### HV-35 Smart Home with Home Assistant

Home tile 5, Home Assistant rooms. The backend bridge is tested against a fake Home Assistant only (170 backend and
24 host tests); **Home Assistant is not configured yet (no URL, no token) and nothing has run against a real
instance.** Setup, allowlist and the full checklist: [`HOME_ASSISTANT.md`](HOME_ASSISTANT.md) (its items 1 and 2 are
HV-34; items 3 to 6 are this check).

Part A, not set up (no `HOME_ASSISTANT_URL` or `HOME_ASSISTANT_TOKEN` in `backend/.env`):

1. The tile status reads `IR only`. The rooms list shows `IR Remote`, then one muted status row (`not set up`).
2. Enter on that row explains which names are missing (URL, token or allowlist) without printing any value.
   `GET /v1/home/status` (owner, loopback) lists the same problems and never shows the URL or the token.
3. The IR keys still work (HV-34) and Back and Home work.

Part B, a real instance (use a test light or switch first):

1. Set `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN` and an allowlist with real entity ids (start from
   `examples/home_allowlist.example.json`), restart the backend. The tile reads `IR+HA`; the rooms and their device
   states match Home Assistant.
2. Toggle a light from the device: the light changes and the notice shows the state Home Assistant reports.
3. Impactful entity (lock, cover; alarm panels can only be armed): the Confirm page appears; Back (or Home, or the
   emergency cancel) sends nothing; after 0.7 s Enter acts. `GET /v1/home/audit` shows the request with
   `confirmed: true` after a `pending` row, and the rejected attempt leaves no action.
4. Failure paths, none of which may block the IR keys: stop Home Assistant (rooms show `offline`); use a wrong token
   (`rejected the token`); stop the backend (`No answer from the PC backend`); a lost answer to an action reads
   `No answer; result unknown. Check it` and the action is not repeated by the device.
5. The URL and the token appear nowhere: not on the screen, not in the serial log, not in `GET /v1/home/status`.

**Pass:** Part A and every step of Part B behave as written. Until Part B has been run against a real instance, the
Home Assistant half of this module stays UNVERIFIED even if Part A passes.

### HV-24 to HV-26 (after M4 exists)

These follow the reference demonstration in [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md#10-full-demonstration-report-and-recovery):
vulnerable vs corrected fixture, success after legitimate authorisation, a verified hash of the transferred
object (compare with `.\scripts\fixture.ps1 synth`), interruption and recovery of a transfer, and the
on-device confirmation for the virtual-LED write (including a timeout and a rejection). They stay
**BLOCKED** until the M4 firmware is built.

### HV-36 Status LED

1. Boot: white breathing on the start screen; after the key press and provisioning, green breathing once the
   status bar reads online. Unplug the backend PC from the network (or stop the backend): amber blink every 2 s
   within a minute, green again after it is back.
2. Start a Wi-Fi survey: teal breathing while it runs, then two green pulses (or two long red blinks when it
   was cancelled or failed) and the state colour again.
3. Ask Oyster AI a question and leave the run screen: when the answer arrives the LED blinks blue three
   times and then breathes blue until the answer is opened.
4. A confirmation dialog (backend job or a module page) turns the LED blue while it is open.
5. Home > Help > Enter: the legend shows one coloured dot per row; the colours must match the LED.

**Pass:** every colour and rhythm above is seen; nothing flickers faster than one blink per second.

### HV-37 Direct AI (Oyster AI without the backend)

Oyster AI tile 2 answering a question with no laptop and no backend, by calling Groq directly over the
device's own Wi-Fi (`firmware/src/direct_ai.cpp`, CA bundle `firmware/include/groq_ca.h`). The wire format
has 16 host tests; the live request is **unverified** until this is seen on the device. A key must be stored
first (`SET-AI-KEY`, loaded from `backend/.env` by `oyster-up`, or `provision-device --ai-key-file`).

1. With the backend running, tile 2 reads `Oyster AI`. Stop the backend (`.\scripts\oyster-up.ps1 -Stop`) and
   wait until the status bar leaves online: tile 2 now reads `Oyster AI (direct)`.
2. Ask a question (`Ask a question`, type, Enter). The Run view shows `Oyster AI (direct, no backend)`, the
   model id, and `Asking the provider over Wi-Fi...`. Within a few seconds the answer appears.
3. Leave the Run view before the answer arrives: the LED blinks blue three times and breathes blue until the
   answer is opened (as HV-36 step 3, but for the direct run).
4. Failure paths, each shown as a short message, never a hang: turn the device's Wi-Fi network off (`Wi-Fi
   lost`/`not connected`); store a wrong key (`server error (HTTP 401)`); the key never appears on the screen
   or in the serial log.
5. `STATUS` over USB reports `ai_key_set: true`; the key itself is never printed.
6. Voice (tile 2, `Ask a question (voice)`) still reports the backend is needed while offline: direct mode is
   text Q&A only.

**Pass:** steps 1 to 6 behave as written and no key or token is ever shown. The bundled CA is unverified
against the live host; if step 2 fails with a certificate error, the correct root must be swapped into
`groq_ca.h` or stored in LittleFS, and this check stays UNVERIFIED until an answer is seen.
