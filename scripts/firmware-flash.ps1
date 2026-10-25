<#
.SYNOPSIS
  Build and flash the firmware to a Cardputer ADV over USB-C.  MANUAL STEP - unverified until run.

.DESCRIPTION
  Runs "pio run -e cardputer-adv -t upload --upload-port <Port>" in firmware\.

  Flash-safety gate: this image replaces the whole flash (factory firmware, launcher, original apps; custom
  partition table without OTA). Before uploading, the script reads the connected chip's MAC and requires a
  verified backup for exactly that MAC: _backups\<mac>\<stamp>\factory-8MB.bin must be 8388608 bytes and match
  its factory-8MB.bin.sha256. Without one it refuses and prints the command that creates it
  (.\scripts\firmware-backup.ps1). -SkipBackupCheck overrides the gate and accepts that the factory firmware
  and original apps are lost.

  Before running: close any serial monitor (only one program can open a COM port), use a USB-C cable
  that carries data, and find the port with "pio device list" or Device Manager > Ports (COM & LPT).
  If the upload cannot connect, put the board into download mode: power switch OFF, hold G0, plug in
  the USB-C data cable, release G0 - then run this again and afterwards power-cycle the device
  (docs/TROUBLESHOOTING.md). Restoring from your own backup or with M5Burner is described there too.

.PARAMETER Port
  The device's COM port, for example COM5.

.PARAMETER SkipBackupCheck
  Flash without a verified backup. Prints a warning and waits 5 seconds (Ctrl+C aborts).

.PARAMETER DryRun
  Print the gate and the upload commands. Touches nothing: no serial port, no flash.

.EXAMPLE
  .\scripts\firmware-backup.ps1 -Port COM5
  .\scripts\firmware-flash.ps1 -Port COM5
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^COM\d+$')]
    [string]$Port,

    [switch]$SkipBackupCheck,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterPio -Hint 'Run .\scripts\setup.ps1 (installs PlatformIO).'

$uploadArgs = @('run', '-e', $OysterFirmwareEnv, '-t', 'upload', '--upload-port', $Port)
$backupCmd = ".\scripts\firmware-backup.ps1 -Port $Port"

if ($DryRun) {
    Write-OysterStep 'DRY RUN - nothing is read, written or flashed'
    if (-not (Test-OysterPortPresent -Port $Port)) {
        Write-OysterNote "$Port is not present now; a real run would refuse to start."
    }
    if ($SkipBackupCheck) {
        Write-OysterWarn '-SkipBackupCheck: the backup gate would be skipped (factory firmware and original apps would be lost).'
    } else {
        $tool = Find-OysterEsptool
        if (-not $tool) { $tool = '<PlatformIO packages>\tool-esptoolpy\esptool.py' }
        Write-Host '    gate 1: read the chip MAC (read-only)'
        Write-Host ('      python ' + (ConvertTo-OysterArgString -ArgumentList @($tool, '--chip', 'esp32s3', '--port', $Port, 'flash_id'))) -ForegroundColor DarkGray
        Write-Host "    gate 2: require $(Join-Path $OysterBackupRoot '<mac>\<stamp>\')$OysterBackupFileName ($OysterFlashBytes bytes) whose SHA-256 matches $OysterBackupFileName.sha256"
        Write-Host "    otherwise refuse and tell you to run: $backupCmd"
    }
    Write-Host '    upload:'
    Write-Host ('      pio ' + ($uploadArgs -join ' ') + '   (in firmware\)') -ForegroundColor DarkGray
    return
}

# Refuse early when the port is absent (skipped only if .NET cannot enumerate ports at all).
Assert-OysterPort -Port $Port

if ($SkipBackupCheck) {
    Write-Host ''
    Write-Host '################################################################################' -ForegroundColor Red
    Write-Host '# -SkipBackupCheck: NO VERIFIED BACKUP OF THIS DEVICE IS REQUIRED OR CHECKED.' -ForegroundColor Red
    Write-Host '# Flashing replaces the M5Stack factory firmware, the launcher and the original' -ForegroundColor Red
    Write-Host '# apps stored in flash, and installs a different partition table (no OTA).' -ForegroundColor Red
    Write-Host '# They are LOST unless you backed them up some other way. Press Ctrl+C now.' -ForegroundColor Red
    Write-Host '################################################################################' -ForegroundColor Red
    Write-Host ''
    Start-Sleep -Seconds 5
} else {
    $esptool = Assert-OysterEsptool
    Write-OysterStep 'Backup gate: reading the chip MAC (read-only)'
    $id = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList @('--chip', 'esp32s3', '--port', $Port, 'flash_id')
    $info = Get-OysterEsptoolInfo -Text $id.Text
    if ($id.ExitCode -ne 0 -or -not $info.Mac) {
        throw "Could not read the chip on $Port (esptool exit code $($id.ExitCode)), so a backup cannot be matched to it. Close any serial monitor, check the cable, or use download mode (docs/TROUBLESHOOTING.md). Nothing was flashed."
    }
    $backup = Find-OysterValidBackup -Mac $info.Mac
    if (-not $backup) {
        $macKey = ConvertTo-OysterMacKey -Mac $info.Mac
        Write-Host ''
        Write-Host "REFUSING TO FLASH: no verified backup for this chip (MAC $($info.Mac))." -ForegroundColor Red
        Write-Host 'Flashing replaces the factory firmware, the launcher and the original apps.'
        Write-Host 'Create the backup first, then run this script again:'
        Write-Host "    $backupCmd" -ForegroundColor Yellow
        Write-Host "A backup counts only if _backups\$macKey\<stamp>\$OysterBackupFileName is exactly $OysterFlashBytes bytes"
        Write-Host "and its SHA-256 matches $OysterBackupFileName.sha256."
        Write-Host 'To flash anyway and lose the original firmware, add -SkipBackupCheck.'
        throw 'Refusing to flash without a verified backup. Nothing was flashed.'
    }
    Write-OysterStep 'Backup gate passed'
    Write-Host "    chip:    $($info.Chip)  MAC $($info.Mac)"
    Write-Host "    backup:  _backups\$(ConvertTo-OysterMacKey -Mac $info.Mac)\$($backup.Stamp)\$OysterBackupFileName"
    Write-Host "    sha256:  $($backup.Sha256)"
    # esptool restarts the chip when it finishes; the USB port can drop for a moment.
    if (-not (Wait-OysterPort -Port $Port)) {
        throw "$Port did not come back after reading the chip. Check the cable and run this again. Nothing was flashed."
    }
}

Write-OysterStep "Flashing $Port  (manual step - unverified until run)"
Invoke-Native -FilePath $OysterPio -ArgumentList $uploadArgs -WorkingDirectory $OysterFirmwareDir

# PlatformIO ends the upload with a reset over the DTR/RTS lines. With the USB-Serial/JTAG port on Windows
# that can leave the chip in its ROM bootloader (dark screen, no program running). A watchdog reset does not
# depend on the line state, so the device is restarted that way. Failure here is not fatal: the upload itself
# has already been verified by esptool.
Write-OysterStep 'Restarting the device into the new firmware'
$resetTool = Find-OysterEsptool
if ($resetTool -and (Wait-OysterPort -Port $Port)) {
    $reset = Invoke-OysterEsptoolCapture -Esptool $resetTool -ArgumentList @(
        '--chip', 'esp32s3', '--port', $Port, '--before', 'default_reset', '--after', 'watchdog_reset', 'read_mac')
    if ($reset.ExitCode -ne 0) {
        Write-OysterWarn 'The restart command failed. Unplug the USB cable, switch the Cardputer off, plug it in and switch it on.'
    }
} else {
    Write-OysterWarn "$Port is not available for the restart. Unplug the USB cable, switch the Cardputer off, plug it in and switch it on."
}

Write-OysterStep 'Upload finished'
Write-Host '    If the screen stays dark, unplug the USB cable, switch the Cardputer off, plug it in and switch it on.'
Write-Host '    Do not open a serial terminal with DTR/RTS asserted: on this USB mode that can reset the chip into its bootloader.'
Write-Host "    Watch the boot log with: .\scripts\firmware-monitor.ps1 -Port $Port"
Write-Host '    Record what you observe in docs/HARDWARE_VERIFICATION.md.'
