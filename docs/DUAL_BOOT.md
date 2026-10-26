# Dual boot with the original firmware

> **Finding (2026-10-26, tested on the real device): in-device dual boot of the original DOES NOT WORK here.**
> The original firmware was built with ESP-IDF 5.4.2; Oyster's bootloader is the Arduino core's ESP-IDF 4.4.7.
> A 4.x second-stage bootloader cannot load a 5.x app, so selecting `ota_1` resets immediately and the
> rollback returns to Oyster (confirmed by booting `ota_1` directly: the device came straight back to Oyster).
> Two firmwares built on different IDF major versions need different bootloaders, and the chip has one
> bootloader slot. **The original is therefore run by restoring the full factory image from the PC**
> (`scripts/original-restore.ps1`), which brings its own bootloader; returning to Oyster is
> `scripts/firmware-flash.ps1`. The "Original Apps" tile now explains this and never attempts the boot.
> The `ota_0`/`ota_1` layout below still describes how the original app is kept on the flash for reference;
> the boot-selector part (`esp_ota_set_boot_partition`, rollback) is retained only as history.

**Status: the layout and scripts are tested on the PC; the in-device boot was tested and does not work (above).**
Decision record: [`adr/ADR-009-dual-boot-original-firmware.md`](adr/ADR-009-dual-boot-original-firmware.md).

## Running the original firmware (the supported way)

```powershell
.\scripts\original-restore.ps1 -Port COM5 -DryRun   # read-only: shows the backup it would restore
.\scripts\original-restore.ps1 -Port COM5           # writes the full factory image; the original then boots
.\scripts\firmware-flash.ps1 -Port COM5             # back to Oyster OS (a few minutes)
```

It restores the verified 8 MB backup (`scripts/firmware-backup.ps1` made it) to flash offset `0x0` and
verifies the chip against it. Oyster is replaced on the chip (reversible: it lives in this repository). While
the original runs, a restart does **not** return to Oyster; re-flash Oyster to come back.

The default build (`env:cardputer-adv`) is a single-slot image that replaces the original firmware; the Home tile
"Original Apps" is then only a message. The dual-boot build (`env:cardputer-adv-dualboot`) keeps **Oyster in
`ota_0` and the original firmware in `ota_1`**. Home > 6 Original Apps shows what is in `ota_1` and, after a
confirmation, restarts into it. The default env and its partition file are unchanged; switching a device to
dual boot is a deliberate step (below).

## Layout

[`firmware/partitions_8mb_dualboot.csv`](../firmware/partitions_8mb_dualboot.csv), 8 MB flash:

| Name | Offset | Size | Holds |
| --- | --- | --- | --- |
| (bootloader, partition table) | 0x0, 0x8000 | | Arduino second-stage bootloader (app rollback enabled), this table |
| `nvs` | 0x9000 | 20 KiB | settings and secrets of Oyster (token, Wi-Fi password, ids) |
| `otadata` | 0xE000 | 8 KiB | boot selector; PlatformIO writes `boot_app0.bin` ("boot `ota_0`") here on every upload |
| `ota_0` | 0x10000 | 0x3A0000 (3712 KiB) | **Oyster** (about 1.6 MiB today, more than 2x headroom) |
| `ota_1` | 0x3B0000 | 0x400000 (4 MiB) | **the original firmware** (about 2.5 MiB; 4 MiB is the size of its own slot) |
| `spiffs` | 0x7B0000 | 256 KiB | LittleFS, only the backend CA (<= 4 KiB) |
| `coredump` | 0x7F0000 | 64 KiB | |

There is no `phy_init` partition: this Arduino build links the PHY init data into the app
(`CONFIG_ESP32_PHY_INIT_DATA_IN_PARTITION` is off). The old single-slot table put `nvs` over 0xE000, where the
upload writes `boot_app0.bin`; the new table moves `nvs` below it.

## How it works

1. `boot_app0.bin` (written by every upload) holds one valid otadata entry, sequence 1, which selects the first
   OTA slot: `ota_0`. Oyster therefore has to live in `ota_0`.
2. Original Apps calls `esp_ota_set_boot_partition(ota_1)`. That verifies the image in `ota_1` (checksum and
   SHA-256), writes a new otadata entry (sequence 2, state NEW) into the other otadata sector and the UI restarts.
3. The bootloader selects the entry with the larger sequence, changes its state to PENDING_VERIFY and boots
   `ota_1`.
4. If the original firmware does **not** mark itself valid and the device resets for any reason (power cycle, reset
   button, watchdog, the original restarting itself), the bootloader finds PENDING_VERIFY, marks the entry
   ABORTED and ignores it; the older entry (sequence 1) selects `ota_0` again: **Oyster is back**. Oyster itself
   needs no marking: its otadata state is "undefined", which the bootloader treats as bootable, and the Arduino
   startup marks a pending app valid anyway.
5. If the original **does** mark itself valid, resets keep booting it. Then
   [`scripts/oyster-return.ps1`](../scripts/oyster-return.ps1) (PC, USB) rewrites otadata with the standard
   `boot_app0.bin` image, which selects `ota_0` again. Nothing else on the flash is touched.

## Setup (once per device)

Needs a verified full-flash backup taken **before the first Oyster flash** (`scripts/firmware-backup.ps1`): the
original exists only there. Close serial monitors; use a data cable.

```powershell
.\scripts\dualboot-setup.ps1 -DryRun                 # file checks only, no chip: shows what would be copied
.\scripts\dualboot-setup.ps1 -Port COM5              # read-only checks of the chip, prints the plan
.\scripts\dualboot-setup.ps1 -Port COM5 -Apply       # writes the original app to ota_1 and verifies it
.\scripts\firmware-flash.ps1 -Port COM5 -Env cardputer-adv-dualboot   # bootloader, table, otadata, Oyster in ota_0
.\scripts\oyster-up.ps1                              # provision again
```

What `dualboot-setup.ps1` does: refuses without a verified backup for the connected chip's MAC; picks the newest
backup that still holds a non-Oyster factory app (a backup taken after an Oyster flash is skipped); cuts the
factory app out with exact bounds (partition offset from the dump's own table, length = last non-0xFF byte rounded
up to 4 KiB, image magic 0xE9, app descriptor magic, and the length is checked against the end the image's segment
table implies); prints project, version, IDF version, build date and SHA-256; writes
`_backups/<mac>/dualboot/original-app.bin`; with `-Apply` it checks the chip's partition table (only the Oyster
single-slot or the dual-boot layout is accepted), skips the write when `ota_1` already holds exactly this file,
else runs `esptool write_flash 0x3B0000` and `verify_flash`. It never writes without `-Apply`, and `-DryRun` opens
no serial port and creates no file.

Order: the original is written first. While the old single-slot table is still on the chip, that write overlaps
the first part of the old `spiffs` partition, so the stored backend CA is lost; the device needs provisioning
after the repartition anyway (NVS is resized, LittleFS moves). Oyster keeps running until the dual-boot flash.
The script can be repeated; flashing Oyster again later (`firmware-flash.ps1 -Env cardputer-adv-dualboot`) does
not touch `ota_1`.

## Coming back from the original

| Situation | What to do |
| --- | --- |
| Normal | Reset or power-cycle the Cardputer. By the rollback above Oyster should start |
| The original keeps booting | `.\scripts\oyster-return.ps1 -Port COM5` (use download mode if the port does not answer: switch OFF, hold G0, plug in the USB-C data cable, release G0) |
| Nothing boots / unknown state | Download mode, then restore the whole backup: `esptool write_flash 0x0 _backups\<mac>\<stamp>\factory-8MB.bin` (the exact command is in the help of `dualboot-setup.ps1` and in [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)). This brings back the original firmware **and removes Oyster**; flash Oyster again afterwards |
| Return to the single-slot layout | `.\scripts\firmware-flash.ps1 -Port COM5` (default env). The bytes in `ota_1` stay on the flash but nothing uses them |

`oyster-return.ps1` refuses unless the chip's table is the dual-boot layout, requires the same verified backup as
the flash script (`-SkipBackupCheck` overrides), reads otadata and writes nothing when it already selects `ota_0`
(`-Force` overrides). `-DryRun` prints the plan.

## Risks

- **The original has never seen this partition table.** Its own table has `nvs` (16 KiB), `otadata`, `phy_init`
  and a 4 MiB `factory` app. Here it runs from `ota_1` with a different `nvs` and no `phy_init`. Its stored
  settings were overwritten when Oyster was first flashed, so it starts like a fresh device.
- **It may use the OTA slots itself.** If the original has an "install app" or update feature, it can write to
  another OTA slot, which here is `ota_0` = Oyster. Do not install or update apps from the original while it runs
  from `ota_1`; Oyster can be re-flashed from the PC, nothing else is lost.
- **It may mark itself valid** (then use `oyster-return.ps1`) or erase NVS (Oyster must be provisioned again).
- **Any reset counts** while the original is pending verification, including one the original triggers itself.
- **Two firmwares share `nvs`.** Oyster uses the namespace `oyster`; a factory reset in the original may erase it.
- **Bootloader mismatch.** The bootloader is the Arduino one (ESP-IDF 4.4.7); the original is an ESP-IDF 5.4.2
  build (on the unit this was prepared on). The header fields the bootloader checks (chip id, revision range) raise
  no conflict when read against its code, but that is not a boot.
- The setup writes into flash that the old layout still uses (see "Order"). With a verified backup nothing is
  unrecoverable; without one `dualboot-setup.ps1` refuses.

## What is verified (on the PC)

- The CSV passes ESP-IDF's `gen_esp32part.py` for an 8 MB flash (no overlaps, alignment, size); `ota_0` has
  more than 2x headroom over the current image; `ota_1` is larger than the original app; `otadata` is at 0xE000 where
  PlatformIO writes `boot_app0.bin`, which is exactly as long as `otadata` and selects `ota_0`
  (`tests/scripts/test_dualboot_layout.py`).
- On the owner's real backup: the factory app is at 0x10000 in a 4 MiB slot, ESP-IDF 5.4.2, about 2.5 MiB used,
  everything after it blank; the script's cut equals an independent Python cut byte for byte (same SHA-256).
- The firmware compiles for both envs without warnings; in the default env Original Apps just reports "not set up".
- The rollback chain was **read from the compiled code** of the pinned Arduino framework (disassembly, not a
  run): the bootloader turns NEW into PENDING_VERIFY on the first boot of an entry and PENDING_VERIFY into ABORTED
  on the next boot, entries in state INVALID or ABORTED are not selected, and `esp_ota_set_boot_partition` in the
  Oyster build writes state NEW. A test pins `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` and
  `CONFIG_APP_ROLLBACK_ENABLE` in the framework's sdkconfig, so a framework upgrade cannot drop them silently.
- `dualboot-setup.ps1 -DryRun`, `oyster-return.ps1 -DryRun` and `firmware-flash.ps1 -Env ... -DryRun` run clean
  without hardware.

## What is not verified (needs the device)

1. That the original firmware **boots from `ota_1`** under the Arduino bootloader with this table.
2. That **rollback returns to Oyster** on a reset, i.e. that the original does not mark itself valid. Unknown until
   tried; `oyster-return.ps1` exists because of it.
3. The esptool commands of both scripts against a real chip: `write_flash`/`verify_flash` at 0x3B0000, the
   `--after watchdog_reset` restart, USB re-enumeration, and reaching a running original over the native USB port.
4. How long the image check inside `esp_ota_set_boot_partition` blocks the UI (it reads the whole slot) and how the
   switching and confirmation pages look on the display.
5. Whether the original uses `phy_init`, NVS contents or the OTA slots in a way that breaks here.
6. The whole procedure on a second unit.

Device checklist to record in `HARDWARE_VERIFICATION.md`: (a) `dualboot-setup.ps1 -Apply` then `-Apply` again
(second run writes nothing); (b) dual-boot flash and provisioning; Original Apps shows project and version and
`ready` on the Home tile; (c) Enter shows the confirmation, Back rejects, Enter approves and the device restarts;
(d) the original's screen appears; (e) a power cycle brings Oyster back; (f) if (e) fails, `oyster-return.ps1`
brings it back; (g) a full restore of the backup in download mode.
