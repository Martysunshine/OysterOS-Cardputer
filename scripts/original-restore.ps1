<#
.SYNOPSIS
  Restore the original Cardputer firmware from the verified backup, so the device boots its own stock
  firmware again (with its own bootloader). To come back to Oyster OS, run scripts/firmware-flash.ps1.
  MANUAL STEP - unverified until run on a real device.

.DESCRIPTION
  The original firmware cannot boot from a second slot next to Oyster OS: it was built with a newer ESP-IDF
  than Oyster's bootloader, and the chip has only one bootloader slot (docs/DUAL_BOOT.md). To run it, the
  whole factory image is written back - bootloader, partition table and the original app together - so the
  original runs exactly as it shipped. Oyster OS is then gone from the chip until you flash it again; nothing
  is lost, because Oyster lives in this repository.

  Steps:
   1. Read the connected chip's MAC (read-only) and find a verified 8 MB backup for it
      (scripts/firmware-backup.ps1 makes one; this script refuses without it).
   2. Write the full backup to flash offset 0x0 with esptool, then verify the chip against the file.
   3. Restart the device. It now boots the original firmware.

  To return to Oyster OS afterwards:  .\scripts\firmware-flash.ps1 -Port <COM port>

.PARAMETER Port
  The device's COM port, for example COM5.

.PARAMETER Baud
  Serial speed passed to esptool (default 115200: the native USB port ignores the rate and a higher one has
  stalled reads on this device).

.PARAMETER DryRun
  Print the plan and run the read-only checks (chip MAC, backup match). Writes nothing to the chip.

.EXAMPLE
  .\scripts\original-restore.ps1 -Port COM5 -DryRun
  .\scripts\original-restore.ps1 -Port COM5
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^COM\d+$')]
    [string]$Port,
    [int]$Baud = 115200,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv

$esptool = Assert-OysterEsptool

Write-OysterStep "Reading the chip on $Port (read-only)"
$id = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList @('--chip', 'esp32s3', '--port', $Port, 'flash_id')
$info = Get-OysterEsptoolInfo -Text $id.Text
if ($id.ExitCode -ne 0 -or -not $info.Mac) {
    throw "Could not read the chip on $Port (esptool exit $($id.ExitCode)). Close any serial monitor, check the cable, or use download mode (docs/TROUBLESHOOTING.md). Nothing was written."
}

$backup = Find-OysterValidBackup -Mac $info.Mac
if (-not $backup) {
    throw "No verified 8 MB backup for this chip (MAC $($info.Mac)). The original firmware can only be restored from a backup made by scripts/firmware-backup.ps1. Nothing was written."
}

Write-Host "    chip:    $($info.Chip)  MAC $($info.Mac)"
Write-Host "    restore: $($backup.Path)"
Write-Host "    sha256:  $($backup.Sha256)"
Write-Host '    This replaces Oyster OS on the chip with the original firmware (reversible: scripts/firmware-flash.ps1).'

if ($DryRun) {
    Write-OysterNote '[dry run] would write the full backup to 0x0 and verify it. Nothing was written.'
    Write-OysterNote "Real run: .\scripts\original-restore.ps1 -Port $Port"
    return
}

if (-not (Wait-OysterPort -Port $Port)) {
    throw "$Port did not come back after reading the chip. Check the cable and run this again. Nothing was written."
}

Write-OysterStep 'Writing the original firmware to flash (several minutes; do not unplug)'
$write = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList @(
    '--chip', 'esp32s3', '--port', $Port, '--baud', "$Baud",
    'write_flash', '--flash_mode', 'keep', '--flash_freq', 'keep', '--flash_size', 'keep', '0x0', $backup.Path)
if ($write.ExitCode -ne 0) {
    throw "esptool write_flash failed (exit $($write.ExitCode)). The chip may be half-written: re-run this script, or restore Oyster with scripts/firmware-flash.ps1. "
}

Write-OysterStep 'Verifying the chip against the backup'
$verify = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList @(
    '--chip', 'esp32s3', '--port', $Port, '--baud', "$Baud", '--after', 'watchdog_reset',
    'verify_flash', '--flash_mode', 'keep', '--flash_freq', 'keep', '--flash_size', 'keep', '0x0', $backup.Path)
if ($verify.ExitCode -ne 0) {
    throw "verify_flash did not match (exit $($verify.ExitCode)). Re-run this script before relying on the device."
}

Write-OysterStep 'Done'
Write-OysterNote 'The device now boots the original firmware. Switch it off and on if the screen stays dark.'
Write-OysterNote "To return to Oyster OS:  .\scripts\firmware-flash.ps1 -Port $Port"
