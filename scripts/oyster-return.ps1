<#
.SYNOPSIS
  Make a dual-boot Cardputer ADV boot Oyster (ota_0) again when it is stuck in the original firmware (ota_1).
  MANUAL STEP - unverified until run on a real device.

.DESCRIPTION
  Home > 6 Original Apps points the boot selector (otadata) at ota_1 and restarts. The bootloader of this build
  has app rollback enabled, so the original should be dropped again at its next reset unless it marks itself
  valid (docs\DUAL_BOOT.md). If it does mark itself valid, a reset keeps booting it. This script is the PC-side
  fallback: it rewrites the otadata partition with the standard initial image (boot_app0.bin: "boot ota_0"), the
  same bytes every PlatformIO upload writes. Nothing else on the flash is touched: Oyster (ota_0), the original
  (ota_1) and the settings stay as they are.

  Safety gates (the chip is only read until the last step):
   1. The chip is an ESP32-S3 with 8 MB flash.
   2. A verified full-flash backup exists for this chip's MAC (same rule as firmware-flash.ps1);
      -SkipBackupCheck overrides it.
   3. The partition table on the chip is the dual-boot layout of firmware\partitions_8mb_dualboot.csv. Any other
      table is refused: the same 8 KiB would belong to something else (NVS, PHY data) there, and no override
      exists for this gate.
   4. otadata is read: when it already selects ota_0 nothing is written (use -Force to write anyway).

  Close any serial monitor first (only one program can open a COM port). If the original firmware does not
  react to the automatic reset into the bootloader (it may drive the USB port itself), use download mode: power
  switch OFF, hold G0, plug in the USB-C data cable, release G0; the port number can change in download mode.
  Afterwards the device restarts by itself; if the screen stays dark, switch it off and on.

  Last resort when even this does not help: write the full backup back with esptool (write_flash 0x0
  _backups\<mac>\<stamp>\factory-8MB.bin), see docs\TROUBLESHOOTING.md and docs\DUAL_BOOT.md.

.PARAMETER Port
  The device's COM port, for example COM5. Required unless -DryRun.

.PARAMETER SkipBackupCheck
  Do not require a verified backup (prints a warning and waits 5 seconds; Ctrl+C aborts).

.PARAMETER Force
  Write the initial otadata even when it already selects ota_0.

.PARAMETER DryRun
  Print the gates and the commands and check the otadata image. Touches nothing: no serial port, no flash.

.EXAMPLE
  .\scripts\oyster-return.ps1 -DryRun
  .\scripts\oyster-return.ps1 -Port COM5
#>
[CmdletBinding()]
param(
    [ValidatePattern('^COM\d+$')]
    [string]$Port = '',

    [switch]$SkipBackupCheck,

    [switch]$Force,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')

$PartitionCsv = Join-Path $OysterFirmwareDir 'partitions_8mb_dualboot.csv'
$Baud = 115200
$StateNames = @{ 0 = 'NEW'; 1 = 'PENDING_VERIFY'; 2 = 'VALID'; 3 = 'INVALID'; 4 = 'ABORTED' }

# ---- helpers (the first four are the same as in dualboot-setup.ps1) ----

function ConvertTo-OysterNumber {
    param([Parameter(Mandatory = $true)][string]$Text)
    $t = $Text.Trim()
    if ($t -match '^0[xX]([0-9a-fA-F]+)$') { return [Convert]::ToInt64($Matches[1], 16) }
    if ($t -match '^(\d+)([KkMm]?)$') {
        $n = [int64]$Matches[1]
        switch ($Matches[2].ToUpperInvariant()) {
            'K' { return $n * 1024 }
            'M' { return $n * 1024 * 1024 }
            default { return $n }
        }
    }
    throw "Cannot read '$Text' as a number in the partition CSV."
}

function Read-OysterPartitionCsv {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Partition file not found: $Path" }
    $rows = [ordered]@{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $f = @($t -split ',' | ForEach-Object { $_.Trim() })
        if ($f.Count -lt 5) { throw "Bad partition row in ${Path}: $t" }
        $rows[$f[0]] = [pscustomobject]@{
            Name = $f[0]; Type = $f[1]; Subtype = $f[2]
            Offset = (ConvertTo-OysterNumber $f[3]); Size = (ConvertTo-OysterNumber $f[4])
        }
    }
    return $rows
}

function Read-OysterFlashTable {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes, [Parameter(Mandatory = $true)][int]$Offset)
    $list = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt 0xC00; $i += 32) {
        $p = $Offset + $i
        if ($p + 32 -gt $Bytes.Length) { break }
        if ($Bytes[$p] -ne 0xAA -or $Bytes[$p + 1] -ne 0x50) { break }
        $nameBytes = New-Object byte[] 16
        [Array]::Copy($Bytes, $p + 12, $nameBytes, 0, 16)
        $list.Add([pscustomobject]@{
            Name = [System.Text.Encoding]::ASCII.GetString($nameBytes).Split([char]0)[0]
            Type = [int]$Bytes[$p + 2]
            Subtype = [int]$Bytes[$p + 3]
            Offset = [int64][BitConverter]::ToUInt32($Bytes, $p + 4)
            Size = [int64][BitConverter]::ToUInt32($Bytes, $p + 8)
        })
    }
    return , $list.ToArray()
}

function Test-OysterDualbootTable {
    param([object[]]$Entries, $Csv)
    foreach ($n in @('otadata', 'ota_0', 'ota_1')) {
        $e = @($Entries | Where-Object { $_.Name -eq $n }) | Select-Object -First 1
        if (-not $e -or $e.Offset -ne $Csv[$n].Offset -or $e.Size -ne $Csv[$n].Size) { return $false }
    }
    return $true
}

# ESP-IDF's crc32_le(UINT32_MAX, data, len), the check value of an otadata entry over its 4-byte sequence number.
# The ROM routine inverts its start value, so UINT32_MAX means "start at 0"; this is not the zlib/PKZIP CRC-32.
function Get-OysterCrc32 {
    param([byte[]]$Bytes)
    $crc = 0L
    foreach ($b in $Bytes) {
        $crc = $crc -bxor [int64]$b
        for ($k = 0; $k -lt 8; $k++) {
            if (($crc -band 1L) -ne 0) { $crc = ($crc -shr 1) -bxor 0xEDB88320L } else { $crc = $crc -shr 1 }
        }
    }
    return ($crc -bxor 0xFFFFFFFFL)
}

# The two 32-byte otadata entries of an 8 KiB image (sector 0 at 0, sector 1 at 0x1000).
function Read-OysterOtadata {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    if ($Bytes.Length -lt 0x2000) { throw 'The otadata image is shorter than 8 KiB.' }
    $entries = @()
    for ($i = 0; $i -lt 2; $i++) {
        $p = $i * 0x1000
        $seq = [int64][BitConverter]::ToUInt32($Bytes, $p)
        $state = [int64][BitConverter]::ToUInt32($Bytes, $p + 24)
        $crc = [int64][BitConverter]::ToUInt32($Bytes, $p + 28)
        $seqBytes = New-Object byte[] 4
        [Array]::Copy($Bytes, $p, $seqBytes, 0, 4)
        $erased = ($seq -eq 0xFFFFFFFFL)
        $valid = (-not $erased) -and ($crc -eq (Get-OysterCrc32 -Bytes $seqBytes)) -and ($state -ne 3) -and ($state -ne 4)
        $entries += [pscustomobject]@{ Sector = $i; Seq = $seq; State = $state; Erased = $erased; Valid = $valid }
    }
    return , $entries
}

function Get-OysterStateText {
    param([int64]$State)
    if ($State -eq 0xFFFFFFFFL) { return 'UNDEFINED' }
    if ($StateNames.ContainsKey([int]$State)) { return $StateNames[[int]$State] }
    return ('0x{0:X}' -f $State)
}

# Slot of a sequence number as the bootloader computes it: (seq - 1) modulo 2^32, then modulo the number of OTA
# slots (2). Sequence 0 therefore means ota_1, which is why boot_app0.bin's second entry (sequence 0) never wins.
function Get-OysterSlotOfSeq {
    param([int64]$Seq)
    return [int]((($Seq - 1) -band 0xFFFFFFFFL) % 2)
}

# Slot index (0 = ota_0, 1 = ota_1) the bootloader picks: the valid entry with the larger sequence number.
function Get-OysterActiveSlot {
    param($Entries)
    $valid = @($Entries | Where-Object { $_.Valid } | Sort-Object Seq -Descending)
    if ($valid.Count -eq 0) { return -1 }
    return (Get-OysterSlotOfSeq -Seq $valid[0].Seq)
}

# boot_app0.bin from the PlatformIO Arduino framework (what every upload writes at 0xE000); when it is missing or
# does not look right, the same 8 KiB are built here: entry 0 = sequence 1 (ota_0), state UNDEFINED, valid CRC.
function Get-OysterInitialOtadata {
    $cores = @()
    if ($env:PLATFORMIO_CORE_DIR) { $cores += $env:PLATFORMIO_CORE_DIR }
    $userHome = [Environment]::GetFolderPath('UserProfile')
    if ($userHome) { $cores += (Join-Path $userHome '.platformio') }
    foreach ($core in $cores) {
        $p = Join-Path $core 'packages\framework-arduinoespressif32\tools\partitions\boot_app0.bin'
        if (-not (Test-Path -LiteralPath $p)) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($p)
        if ($bytes.Length -ne 0x2000) { continue }
        $e = Read-OysterOtadata -Bytes $bytes
        if ($e[0].Valid -and $e[0].Seq -eq 1 -and $e[0].State -eq 0xFFFFFFFFL -and $e[1].Seq -lt 1) {
            return [pscustomobject]@{ Bytes = $bytes; Path = $p; Source = 'PlatformIO framework boot_app0.bin' }
        }
    }
    $img = New-Object byte[] 0x2000
    for ($i = 0; $i -lt $img.Length; $i++) { $img[$i] = 0xFF }
    $img[0] = 1; $img[1] = 0; $img[2] = 0; $img[3] = 0
    $crc = [BitConverter]::GetBytes([uint32](Get-OysterCrc32 -Bytes $img[0..3]))
    [Array]::Copy($crc, 0, $img, 28, 4)
    return [pscustomobject]@{ Bytes = $img; Path = ''; Source = 'built by this script (boot_app0.bin was not found in the PlatformIO packages)' }
}

function Show-OysterCommand {
    param([string]$Tool, [string[]]$ArgumentList)
    Write-Host ('    python ' + (ConvertTo-OysterArgString -ArgumentList (@($Tool) + $ArgumentList))) -ForegroundColor DarkGray
}

function Read-OysterLiveFlash {
    param([string]$Tool, [string[]]$Common, [string]$Address, [string]$Length)
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('oyster-' + [Guid]::NewGuid().ToString('N') + '.bin')
    try {
        $r = Invoke-OysterEsptoolCapture -Esptool $Tool -ArgumentList ($Common + @('read_flash', $Address, $Length, $tmp))
        if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $tmp)) {
            throw "Could not read the flash at $Address (esptool exit code $($r.ExitCode)). Nothing was written."
        }
        $bytes = [System.IO.File]::ReadAllBytes($tmp)
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    }
    return , $bytes
}

# ---- inputs ----

if (-not $DryRun -and -not $Port) {
    throw '-Port is required (only -DryRun works without a chip). Example: .\scripts\oyster-return.ps1 -Port COM5'
}
$csv = Read-OysterPartitionCsv -Path $PartitionCsv
foreach ($n in @('otadata', 'ota_0', 'ota_1')) {
    if (-not $csv.Contains($n)) { throw "$PartitionCsv has no '$n' partition." }
}
$otaOffset = $csv['otadata'].Offset
$otaHex = '0x{0:X}' -f $otaOffset
$otaLenHex = '0x{0:X}' -f $csv['otadata'].Size
$initial = Get-OysterInitialOtadata

$esptool = Find-OysterEsptool
if (-not $DryRun) {
    Assert-OysterVenv
    $esptool = Assert-OysterEsptool
    Assert-OysterPort -Port $Port
} elseif (-not $esptool) {
    $esptool = '<PlatformIO packages>\tool-esptoolpy\esptool.py'
    Write-OysterWarn 'esptool.py was not found; a real run would stop here. Run .\scripts\setup.ps1.'
}
$portText = if ($Port) { $Port } else { '<COM port>' }
$common = @('--chip', 'esp32s3', '--port', $portText, '--baud', "$Baud")
$writeFlags = @('--flash_mode', 'keep', '--flash_freq', 'keep', '--flash_size', 'keep')
$otaFile = if ($initial.Path) { $initial.Path } else { '<temp file with the built otadata image>' }

# ---- dry run: gates and commands, then stop ----

if ($DryRun) {
    Write-OysterStep 'DRY RUN - no serial port is opened, nothing is read from or written to the device'
    $e = Read-OysterOtadata -Bytes $initial.Bytes
    Write-Host "    otadata image: $($initial.Source)"
    Write-Host ('    entry 0: sequence {0} -> ota_{1}, state {2}, CRC ok; entry 1: sequence {3}, never outranks entry 0' -f $e[0].Seq, (Get-OysterSlotOfSeq -Seq $e[0].Seq), (Get-OysterStateText -State $e[0].State), $e[1].Seq)
    Write-Host "    otadata partition in the dual-boot layout: $otaHex, $otaLenHex bytes"
    Write-OysterStep 'Gates of a real run'
    Write-Host '    1. read the chip (flash_id): ESP32-S3 with 8 MB flash'
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('flash_id'))
    Write-Host "    2. require a verified backup for the chip's MAC (override: -SkipBackupCheck)"
    Write-Host "    3. read the partition table: it must be the dual-boot layout, else refuse"
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('read_flash', '0x8000', '0x1000', '<temp file>'))
    Write-Host '    4. read otadata; when it already selects ota_0 nothing is written (override: -Force)'
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('read_flash', $otaHex, $otaLenHex, '<temp file>'))
    Write-OysterStep 'Then'
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('write_flash') + $writeFlags + @($otaHex, $otaFile))
    Show-OysterCommand -Tool $esptool -ArgumentList (@('--chip', 'esp32s3', '--port', $portText, '--baud', "$Baud", '--after', 'watchdog_reset', 'verify_flash') + $writeFlags + @($otaHex, $otaFile))
    return
}

# ---- 1. the chip ----

Write-OysterStep "Reading the chip on $Port (read-only)  (manual step - unverified until run)"
$id = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList ($common + @('flash_id'))
$info = Get-OysterEsptoolInfo -Text $id.Text
if ($id.ExitCode -ne 0 -or -not $info.Mac) {
    throw "Could not read the chip on $Port (esptool exit code $($id.ExitCode)). Close any serial monitor, check the cable, or use download mode (see the help of this script). Nothing was written."
}
if (-not $info.Chip -or $info.Chip -notmatch '^ESP32-S3') { throw "Expected an ESP32-S3 (Cardputer ADV), esptool reports: $($info.Chip). Nothing was written." }
if ($info.FlashSize -ne '8MB') { throw "Expected 8MB of flash (ESP32-S3FN8), esptool reports: $($info.FlashSize). Nothing was written." }
if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after reading the chip. Check the cable and run this again. Nothing was written." }

# ---- 2. backup gate ----

if ($SkipBackupCheck) {
    Write-OysterWarn '-SkipBackupCheck: no verified backup of this device is required or checked.'
    Start-Sleep -Seconds 5
} else {
    $backup = Find-OysterValidBackup -Mac $info.Mac
    if (-not $backup) {
        Write-Host ''
        Write-Host "REFUSING: no verified backup for this chip (MAC $($info.Mac))." -ForegroundColor Red
        Write-Host 'Take one first (.\scripts\firmware-backup.ps1) or, to go on without one, add -SkipBackupCheck.'
        throw 'Refusing to continue without a verified backup. Nothing was written.'
    }
    Write-OysterStep 'Backup gate passed'
    Write-Host "    backup:  _backups\$(ConvertTo-OysterMacKey -Mac $info.Mac)\$($backup.Stamp)\$OysterBackupFileName"
}

# ---- 3. layout gate ----

Write-OysterStep 'Reading the partition table (read-only)'
$table = Read-OysterLiveFlash -Tool $esptool -Common $common -Address '0x8000' -Length '0x1000'
$entries = Read-OysterFlashTable -Bytes $table -Offset 0
if (-not (Test-OysterDualbootTable -Entries $entries -Csv $csv)) {
    Write-Host ''
    Write-Host 'REFUSING: the partition table on the chip is not the dual-boot layout.' -ForegroundColor Red
    foreach ($x in $entries) { Write-Host ('    {0,-9} 0x{1:X6} + 0x{2:X}' -f $x.Name, $x.Offset, $x.Size) }
    Write-Host "This script only resets the otadata partition of firmware\partitions_8mb_dualboot.csv ($otaHex); at that address"
    Write-Host 'another table keeps different data. Nothing was written.'
    throw 'Refusing: not the dual-boot layout. Nothing was written.'
}
Write-Host '    dual-boot layout confirmed (otadata, ota_0 and ota_1 are where the CSV puts them).'
if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after reading the chip. Nothing was written." }

# ---- 4. otadata ----

Write-OysterStep 'Reading otadata (read-only)'
$cur = Read-OysterLiveFlash -Tool $esptool -Common $common -Address $otaHex -Length $otaLenHex
$ent = Read-OysterOtadata -Bytes $cur
foreach ($x in $ent) {
    if ($x.Erased) {
        Write-Host ('    entry {0}: erased' -f $x.Sector)
    } else {
        Write-Host ('    entry {0}: sequence {1} -> ota_{2}, state {3}, {4}' -f $x.Sector, $x.Seq, (Get-OysterSlotOfSeq -Seq $x.Seq), (Get-OysterStateText -State $x.State), $(if ($x.Valid) { 'valid' } else { 'ignored by the bootloader' }))
    }
}
$active = Get-OysterActiveSlot -Entries $ent
if ($active -lt 0) {
    Write-Host '    no valid entry: the bootloader boots ota_0 (there is no factory partition).'
} else {
    Write-Host "    the bootloader boots ota_$active."
}
if ($active -lt 1 -and -not $Force) {
    Write-OysterStep 'Nothing to do'
    Write-Host '    otadata does not select ota_1, so a reset boots Oyster. Nothing was written (-Force writes the initial otadata anyway).'
    return
}
if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after reading the chip. Nothing was written." }

# ---- 5. write the initial otadata ----

$tmpFile = ''
$source = $initial.Path
if (-not $source) {
    $tmpFile = Join-Path ([System.IO.Path]::GetTempPath()) ('oyster-otadata-' + [Guid]::NewGuid().ToString('N') + '.bin')
    [System.IO.File]::WriteAllBytes($tmpFile, $initial.Bytes)
    $source = $tmpFile
}
try {
    Write-OysterStep "Writing the initial otadata ($($initial.Source)) to $otaHex"
    Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool) + $common + @('write_flash') + $writeFlags + @($otaHex, $source))
    if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after the write. Check the cable and run this again (it is safe to repeat)." }
    Write-OysterStep 'Verifying, then restarting the device'
    Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool, '--chip', 'esp32s3', '--port', $Port, '--baud', "$Baud", '--after', 'watchdog_reset', 'verify_flash') + $writeFlags + @($otaHex, $source))
} finally {
    if ($tmpFile -and (Test-Path -LiteralPath $tmpFile)) { Remove-Item -LiteralPath $tmpFile -Force }
}

Write-OysterStep 'Done'
Write-Host '    otadata now selects ota_0: Oyster should start within a few seconds.'
Write-Host '    If the screen stays dark, unplug the USB cable, switch the Cardputer off, plug it in and switch it on.'
Write-Host '    Record what you observe in docs/HARDWARE_VERIFICATION.md.'
