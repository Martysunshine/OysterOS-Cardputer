[OysterOS](../../README.md) / [Docs](../README.md) / ADR-009

# ADR-009: Dual boot with the original firmware (two OTA slots)

**Status:** **rejected (2026-10-26) for the in-device boot; superseded by a PC full-image restore.** The
device trial failed: the original firmware (ESP-IDF 5.4.2) cannot be loaded by Oyster's bootloader (Arduino
core, ESP-IDF 4.4.7), so selecting `ota_1` resets and rolls back to Oyster. Two firmwares on different IDF
major versions need different bootloaders and the chip has one bootloader slot. The original is now run by
restoring the full factory image from the verified backup on the PC (`scripts/original-restore.ps1`); the
"Original Apps" tile explains this and no longer sets the boot partition. See
[`../DUAL_BOOT.md`](../DUAL_BOOT.md). The two-slot layout below is kept as the record of what was tried.

**Decision.** Add a second build, `env:cardputer-adv-dualboot`, with its own partition table
(`firmware/partitions_8mb_dualboot.csv`): Oyster in `ota_0`, the original firmware in `ota_1`, `otadata` at
0xE000. The default env and `partitions_8mb_noota.csv` stay as they are; a device is moved over deliberately with
`scripts/dualboot-setup.ps1` (copies the original app out of the verified full-flash backup into `ota_1`) followed
by `scripts/firmware-flash.ps1 -Env cardputer-adv-dualboot`. The Home tile "Original Apps" shows the app in
`ota_1` and, after a local confirmation, calls `esp_ota_set_boot_partition(ota_1)` and restarts. The way back is
the bootloader's app rollback (a reset drops an app that never marks itself valid), with
`scripts/oyster-return.ps1` (otadata reset over USB) and the full-flash backup as fallbacks.

**Why.** Design constraint 2 ("original apps stay accessible, no fake placeholders") is not met by the
single-slot build, and the backup alone is a PC-side restore, not a feature of the device. The original app is
small (about 2.5 MiB of a 4 MiB slot) and the flash is 8 MB, so both fit. Putting Oyster in `ota_0` matches the
initial otadata that every PlatformIO upload writes (`boot_app0.bin`: sequence 1 = first OTA slot), so the
existing upload path needs no special handling. The confirmation uses the same local-confirmation rule as
ADR-005: nothing is written to otadata before the user approves on the device.

**The rollback assumption.** The Arduino bootloader of the pinned framework is built with
`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, and Oyster's `esp_ota_set_boot_partition` writes the new otadata entry
in state NEW (both read from the compiled code). The bootloader then marks the entry PENDING_VERIFY on its first
boot and ABORTED on the next, and ignores ABORTED entries, so the older entry (`ota_0`) is booted again. This holds
only if the original never calls `esp_ota_mark_app_valid_cancel_rollback()`. That is unknown until it is tried;
the design does not depend on it for safety, only for convenience (`oyster-return.ps1` covers the other case).
`tests/scripts` pins the two config options so a framework upgrade cannot drop them silently.

**Alternatives considered.**
- *Keep the single slot and an information screen* (the state before this ADR): honest, but the device cannot
  start the original at all; rejected as the long-term answer, kept as the default build.
- *Original in `factory`, Oyster in `ota_0`* (the original stays where it is): `esp_ota_set_boot_partition(factory)`
  erases otadata, so leaving Oyster works, but the original knows nothing about Oyster and no rollback brings Oyster
  back; every return would need the PC. Rejected.
- *Oyster as an app inside the original launcher* (design Option A/B): needs an audit of what the installed
  launcher accepts; none was done. Not excluded for later, not available now.
- *Equal slots of about 3.8 MiB*: simpler to explain, but `ota_0` gains nothing from the extra space today and
  `ota_1` keeps the original's own slot size (4 MiB), so the table follows the sizes in use.

**Consequences.** Oyster's app slot shrinks from 5 MiB to 3.6 MiB (more than 2x headroom over today's image); `spiffs` shrinks
to 256 KiB (it holds one certificate); `nvs` is 20 KiB and the `phy_init` partition is gone; moving a device
needs provisioning again. A second partition table and two setup scripts are maintained. The original firmware
runs in an environment it was not built for (other table, Arduino bootloader, shared NVS, OTA slots it could write
to); the risks are listed in `DUAL_BOOT.md`. The setup refuses to run without a verified backup taken before the
first Oyster flash, because that backup is the only source of the original.
