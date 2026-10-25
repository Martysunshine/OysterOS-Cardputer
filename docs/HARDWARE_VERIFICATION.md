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
| HV-27 | Backup before the first flash: `firmware-backup.ps1` dumps 8 MB with verification; restoring it brings the original firmware back | UNVERIFIED | | | |
| HV-28 | Flash gate: `firmware-flash.ps1` refuses without a verified backup of that chip, passes with one | UNVERIFIED | | | |
| HV-29 | Boot splash drawn once at start-up, then the Home screen (heading `OYSTER OS`, tile grid, greyed-out tiles) | UNVERIFIED | | | |

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
   modules start. Look for flicker, tearing, wrong colours (the art was converted to RGB565 on a PC) and
   how long it stays up.
2. The first screen is Home, headed `OYSTER OS`, with a two-column tile grid: Security, Oyster AI and Linux
   on the left; Control Deck, Smart Home and Original Apps on the right; Settings and Help in a fourth row
   that scrolls into view. Check icons, labels, the selection stripe and the number shortcuts `1` to `8`.
3. Open Linux, Control Deck, Smart Home and Original Apps. Each tile must look greyed out and open a
   "Not available in this build" screen that Back leaves. Nothing may pretend to connect or run.
4. Check the Oyster AI tile against the backend state (connected, or unavailable with a reason).

**Pass:** the splash appears once without artefacts, Home is readable at 240x135, and the four unbuilt
modules are visibly unavailable. Record anything clipped or hard to read, with photographs.

### HV-24 to HV-26 (after M4 exists)

These follow the reference demonstration in [`SETUP_WINDOWS.md`](SETUP_WINDOWS.md#10-full-demonstration-report-and-recovery):
vulnerable vs corrected fixture, success after legitimate authorisation, a verified hash of the transferred
object (compare with `.\scripts\fixture.ps1 synth`), interruption and recovery of a transfer, and the
on-device confirmation for the virtual-LED write (including a timeout and a rejection). They stay
**BLOCKED** until the M4 firmware is built.
