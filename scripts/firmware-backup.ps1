<#
.SYNOPSIS
  Back up the complete 8 MB flash of a Cardputer ADV to this PC. Run it BEFORE the first Oyster flash.
  MANUAL STEP - unverified until run on a real device.

.DESCRIPTION
  Oyster Pocket is a full-flash image with its own partition table (firmware/partitions_8mb_noota.csv). It
  replaces the M5Stack factory firmware, the launcher and the original apps. This script saves them first, and
  firmware-flash.ps1 refuses to flash a chip that has no verified backup.

  Uses the esptool.py that PlatformIO already installed (package tool-esptoolpy); nothing is downloaded.
  Steps: (1) read chip info, MAC and flash size (must be ESP32-S3 with 8 MB); (2) read_flash 0x0 0x800000 into
  _backups\<mac>\<yyyymmdd-hhmmss>\factory-8MB.bin.partial; (3) check the size and compute the SHA-256;
  (4) optional -VerifyByReread: read the flash a second time and compare the hashes; (5) esptool verify_flash
  (the chip hashes its own flash and compares it with the file); (6) rename to factory-8MB.bin and write
  factory-8MB.bin.sha256 and backup.json (chip, MAC, flash size, date, esptool version).

  The dump can contain Wi-Fi credentials or keys of the original firmware. Keep it on this PC. _backups\ must
  never be committed or shared.
  If the device already runs Oyster, the dump is an Oyster image, not the factory firmware: take the backup
  before the first flash.

  Close any serial monitor first (only one program can open a COM port). If the chip cannot be reached, use
  download mode (docs/TROUBLESHOOTING.md): switch OFF, hold G0, plug in the USB-C data cable, release G0.
  Afterwards switch the device off and on.

.PARAMETER Port
  The device's COM port, for example COM5 (find it with ".\.venv\Scripts\pio.exe device list").

.PARAMETER Baud
  Serial speed passed to esptool (default 460800).

.PARAMETER VerifyByReread
  Also read the whole flash a second time and require the same SHA-256 (takes about twice as long).

.PARAMETER DryRun
  Print the commands and the output location. Touches nothing: no serial port, no file, no folder.

.EXAMPLE
  .\scripts\firmware-backup.ps1 -Port COM5 -DryRun
  .\scripts\firmware-backup.ps1 -Port COM5
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^COM\d+$')]
    [string]$Port,

    [ValidateRange(115200, 2000000)]
    [int]$Baud = 460800,

    [switch]$VerifyByReread,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv

$flashHex = '0x{0:X}' -f $OysterFlashBytes   # 0x800000
$common = @('--chip', 'esp32s3', '--port', $Port, '--baud', "$Baud")

function Show-OysterCommand {
    param([string]$Esptool, [string[]]$ArgumentList)
    Write-Host ('    python ' + (ConvertTo-OysterArgString -ArgumentList (@($Esptool) + $ArgumentList))) -ForegroundColor DarkGray
}

# ---- dry run: print the plan and stop ----
if ($DryRun) {
    $tool = Find-OysterEsptool
    if (-not $tool) {
        $tool = '<PlatformIO packages>\tool-esptoolpy\esptool.py'
        Write-OysterWarn 'esptool.py was not found; a real run would stop here. Run .\scripts\setup.ps1.'
    }
    Write-OysterStep 'DRY RUN - nothing is read, written or created'
    if (-not (Test-OysterPortPresent -Port $Port)) {
        Write-OysterNote "$Port is not present now; a real run would refuse to start."
    }
    $dest = Join-Path $OysterBackupRoot '<mac>\<yyyymmdd-hhmmss>'
    Write-Host "    output folder: $dest"
    Write-Host "    files: $OysterBackupFileName, $OysterBackupFileName.sha256, backup.json"
    Write-OysterStep '1. chip info, MAC, flash size (must be ESP32-S3, 8MB)'
    Show-OysterCommand -Esptool $tool -ArgumentList ($common + @('--after', 'no_reset', 'flash_id'))
    Write-OysterStep "2. read the whole flash ($flashHex bytes)"
    Show-OysterCommand -Esptool $tool -ArgumentList ($common + @('--after', 'no_reset', 'read_flash', '0x0', $flashHex, "$dest\$OysterBackupFileName.partial"))
    if ($VerifyByReread) {
        Write-OysterStep '3. second full read, SHA-256 must match the first'
        Show-OysterCommand -Esptool $tool -ArgumentList ($common + @('--after', 'no_reset', 'read_flash', '0x0', $flashHex, "$dest\$OysterBackupFileName.partial2"))
    }
    Write-OysterStep 'Last. esptool verify_flash (chip-side hash of its flash against the file), then the device restarts'
    Show-OysterCommand -Esptool $tool -ArgumentList ($common + @('verify_flash', '--flash_mode', 'keep', '--flash_freq', 'keep', '--flash_size', 'keep', '0x0', "$dest\$OysterBackupFileName.partial"))
    Write-Host '    then: size check, SHA-256 file, backup.json, rename .partial -> final name'
    return
}

# ---- real run ----
Assert-OysterPort -Port $Port
$esptool = Assert-OysterEsptool

Write-OysterStep "Backing up the flash of the device on $Port  (manual step - unverified until run)"
Write-OysterNote 'Do this before the first Oyster flash: only then does the dump contain the factory firmware.'
Write-OysterNote 'Keep the files on this PC: the dump can hold Wi-Fi credentials or keys of the original firmware.'

Write-OysterStep '1/4 Reading chip info, MAC and flash size'
$id = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList ($common + @('--after', 'no_reset', 'flash_id'))
$info = Get-OysterEsptoolInfo -Text $id.Text
if ($id.ExitCode -ne 0 -or -not $info.Mac) {
    throw "Could not read the chip on $Port (esptool exit code $($id.ExitCode)). Close any serial monitor, use a data cable, or put the board into download mode (docs/TROUBLESHOOTING.md)."
}
if (-not $info.Chip -or $info.Chip -notmatch '^ESP32-S3') {
    throw "Expected an ESP32-S3 (Cardputer ADV), esptool reports: $($info.Chip). Nothing was read. Switch the device off and on before trying another board."
}
if ($info.FlashSize -ne '8MB') {
    throw "Expected 8MB of flash (ESP32-S3FN8), esptool reports: $($info.FlashSize). Nothing was read. Switch the device off and on before trying another board."
}
$macKey = ConvertTo-OysterMacKey -Mac $info.Mac

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dir = Join-Path (Join-Path $OysterBackupRoot $macKey) $stamp
$partial = Join-Path $dir "$OysterBackupFileName.partial"
$partial2 = Join-Path $dir "$OysterBackupFileName.partial2"
$final = Join-Path $dir $OysterBackupFileName
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# A flash dump can contain credentials.
Write-OysterWarn 'The dump may contain credentials: do not commit or share it.'

try {
    Write-OysterStep "2/4 Reading $flashHex bytes into $OysterBackupFileName.partial (several minutes; do not unplug)"
    Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool) + $common + @('--after', 'no_reset', 'read_flash', '0x0', $flashHex, $partial))

    Write-OysterStep '3/4 Checking size and hash'
    $size = (Get-Item -LiteralPath $partial).Length
    if ($size -ne $OysterFlashBytes) { throw "The dump is $size bytes, expected $OysterFlashBytes." }
    $sha = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "    size:    $size bytes"
    Write-Host "    sha256:  $sha"
    $verified = @("size is exactly $OysterFlashBytes bytes")

    if ($VerifyByReread) {
        Write-OysterNote 'Second full read (-VerifyByReread)'
        Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool) + $common + @('--after', 'no_reset', 'read_flash', '0x0', $flashHex, $partial2))
        $sha2 = (Get-FileHash -LiteralPath $partial2 -Algorithm SHA256).Hash.ToLowerInvariant()
        Remove-Item -LiteralPath $partial2 -Force
        if ($sha2 -ne $sha) { throw 'The second read differs from the first (SHA-256 mismatch). The cable or the connection is unreliable.' }
        $verified += 'second full read has the same SHA-256'
    }

    Write-OysterStep '4/4 esptool verify_flash: the chip hashes its own flash and compares it with the file'
    Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool) + $common + @('verify_flash', '--flash_mode', 'keep', '--flash_freq', 'keep', '--flash_size', 'keep', '0x0', $partial))
    $verified += 'esptool verify_flash: on-chip digest of the flash equals the file'
} catch {
    foreach ($f in @($partial, $partial2)) { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force } }
    if ((Test-Path -LiteralPath $dir) -and -not @(Get-ChildItem -LiteralPath $dir -Force).Count) { Remove-Item -LiteralPath $dir -Force }
    Write-OysterWarn 'The backup failed and the partial file was removed. Nothing was written to the device.'
    throw
}

Move-Item -LiteralPath $partial -Destination $final
$ascii = New-Object System.Text.ASCIIEncoding
[System.IO.File]::WriteAllText("$final.sha256", "$sha  $OysterBackupFileName`n", $ascii)
$meta = [ordered]@{
    chip           = $info.Chip
    mac            = $info.Mac
    flash_size     = $info.FlashSize
    flash_bytes    = $OysterFlashBytes
    file           = $OysterBackupFileName
    sha256         = $sha
    created_utc    = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    esptool        = $info.EsptoolVersion
    verified       = $verified
    note           = 'Whole-flash dump taken with firmware-backup.ps1. It is the factory firmware only if it was taken before the first Oyster flash.'
}
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $dir 'backup.json'), (($meta | ConvertTo-Json -Depth 4) + "`n"), $utf8)

# An erased or unbootable flash has no bootloader image (0xE9) at offset 0: such a dump cannot restore a working device.
$fs = [System.IO.File]::OpenRead($final)
try { $first = $fs.ReadByte() } finally { $fs.Dispose() }
if ($first -ne 0xE9) {
    Write-OysterWarn ('The first byte of the dump is 0x{0:X2}, not 0xE9 (ESP32-S3 bootloader image). The device may have been blank; this dump may not restore a working device.' -f $first)
}

Write-OysterStep 'Backup finished'
Write-Host "    folder:  $dir"
Write-Host "    file:    $OysterBackupFileName ($OysterFlashBytes bytes), $OysterBackupFileName.sha256, backup.json"
Write-Host '    Switch the device off and on once. Restore route: docs/TROUBLESHOOTING.md (write_flash 0x0).'
Write-Host "    Next: .\scripts\firmware-flash.ps1 -Port $Port"
Write-Host '    Record the result in docs/HARDWARE_VERIFICATION.md (HV-27).'
