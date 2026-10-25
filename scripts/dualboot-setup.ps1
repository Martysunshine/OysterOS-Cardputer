<#
.SYNOPSIS
  Prepare dual boot on a Cardputer ADV: copy the original firmware out of your verified backup into the ota_1 slot.
  MANUAL STEP - unverified until run on a real device.

.DESCRIPTION
  Oyster normally owns the whole flash (firmware\partitions_8mb_noota.csv). The dual-boot layout
  (firmware\partitions_8mb_dualboot.csv, PlatformIO env cardputer-adv-dualboot) keeps Oyster in ota_0 and the
  original firmware in ota_1; Home > 6 Original Apps then boots it. Background and risks: docs\DUAL_BOOT.md,
  docs\adr\ADR-009-dual-boot-original-firmware.md.

  This script does the part that needs your backup. Without -Apply it only prints the plan and runs read-only
  checks; nothing is ever written to the chip without -Apply.
   1. Backup gate: a verified full-flash backup for the connected chip's MAC must exist (the same rule as
      firmware-flash.ps1: _backups\<mac>\<stamp>\factory-8MB.bin is 8388608 bytes and matches its .sha256).
      It refuses otherwise. Backups taken after Oyster was flashed contain Oyster, not the original; the newest
      backup that still holds a non-Oyster factory app is the source, and the script refuses when there is none.
   2. Cuts the original app out of that dump with exact bounds: the factory app partition of the dump's own
      partition table (offset 0x10000), length = the last non-0xFF byte rounded up to 4 KiB. It asserts the image
      magic 0xE9, the app descriptor magic and that the length equals the end the image's segment table implies,
      prints the descriptor and the SHA-256, and writes _backups\<mac>\dualboot\original-app.bin (+ .sha256).
   3. -Apply only: reads the chip's partition table (it must be the Oyster single-slot or the dual-boot layout,
      nothing else), then esptool write_flash of that file to the ota_1 offset of the dual-boot CSV (0x3B0000)
      and esptool verify_flash of the same file. Skipped when ota_1 already holds exactly this image, so the
      script can be run again safely.
   4. Prints the next steps: flash the dual-boot build (.\scripts\firmware-flash.ps1 -Env cardputer-adv-dualboot
      writes bootloader, the new partition table, the initial otadata and Oyster into ota_0), then provision the
      device again with .\scripts\oyster-up.ps1 (the repartition resizes NVS and moves the LittleFS partition).

  While the old single-slot layout is still installed, the write at 0x3B0000 lands in the unused tail of the
  Oyster app slot and in the first part of the old spiffs partition: the backend CA stored there is lost and the
  device must be provisioned again (it has to be anyway, see step 4). Oyster itself keeps running.

  RESTORE the device to exactly what the backup holds (no Oyster, no dual boot): put the board into download
  mode (power switch OFF, hold G0, plug in the USB-C data cable, release G0), close any serial monitor, then
      & .\.venv\Scripts\python.exe <esptool.py from the PlatformIO packages> --chip esp32s3 --port COM5 --baud 115200 write_flash --flash_mode keep --flash_freq keep --flash_size keep 0x0 _backups\<mac>\<stamp>\factory-8MB.bin
  and switch the device off and on. More in docs\TROUBLESHOOTING.md.

  Before running with a port: close any serial monitor (only one program can open a COM port) and use a USB-C
  cable that carries data. If the chip cannot be reached use download mode as above.

.PARAMETER Port
  The device's COM port, for example COM5. Required unless -DryRun.

.PARAMETER Mac
  Dry run only (no chip is read, so the MAC cannot come from it): which backup folder to use when more than one
  device has a backup in _backups\. Colon or dash form.

.PARAMETER Apply
  Write the original app to ota_1 (and verify it). Without it the chip is only read.

.PARAMETER DryRun
  Print the plan and run the file checks on the backup (nothing is written, no file is created, no serial port
  is opened). -Port is optional.

.EXAMPLE
  .\scripts\dualboot-setup.ps1 -DryRun
  .\scripts\dualboot-setup.ps1 -Port COM5
  .\scripts\dualboot-setup.ps1 -Port COM5 -Apply
  .\scripts\firmware-flash.ps1 -Port COM5 -Env cardputer-adv-dualboot
#>
[CmdletBinding()]
param(
    [ValidatePattern('^COM\d+$')]
    [string]$Port = '',

    [ValidatePattern('^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')]
    [string]$Mac = '',

    [switch]$Apply,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')

$DualEnv = 'cardputer-adv-dualboot'
$PartitionCsv = Join-Path $OysterFirmwareDir 'partitions_8mb_dualboot.csv'
$ImageMagic = 0xE9
$AppDescMagic = 0xABCD5432L   # long suffix: a plain hex literal this large would be a negative Int32
$Baud = 115200

# ---- helpers (pure functions; no chip access) ----

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

# name -> @{Name; Type; Subtype; Offset; Size} for the rows of an ESP-IDF partition CSV.
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

# Entries of a binary partition table that starts at $Offset inside $Bytes (0xAA 0x50 magic per 32-byte row).
function Read-OysterFlashTable {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes, [Parameter(Mandatory = $true)][int]$Offset)
    $list = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt 0xC00; $i += 32) {
        $p = $Offset + $i
        if ($p + 32 -gt $Bytes.Length) { break }
        if ($Bytes[$p] -ne 0xAA -or $Bytes[$p + 1] -ne 0x50) { break }   # MD5 row (EB EB) or blank ends the table
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

# 'dualboot' (matches the CSV), 'single' (the Oyster single-slot table: one 5 MiB factory app) or 'other'.
function Get-OysterLayoutName {
    param([object[]]$Entries, $Csv)
    $find = { param($name) @($Entries | Where-Object { $_.Name -eq $name }) | Select-Object -First 1 }
    $o0 = & $find 'ota_0'; $o1 = & $find 'ota_1'; $od = & $find 'otadata'; $fa = & $find 'factory'
    if ($o0 -and $o1 -and $od -and
        $o0.Offset -eq $Csv['ota_0'].Offset -and $o0.Size -eq $Csv['ota_0'].Size -and
        $o1.Offset -eq $Csv['ota_1'].Offset -and $o1.Size -eq $Csv['ota_1'].Size -and
        $od.Offset -eq $Csv['otadata'].Offset -and $od.Size -eq $Csv['otadata'].Size) { return 'dualboot' }
    if ($fa -and -not $o1 -and $fa.Type -eq 0 -and $fa.Subtype -eq 0 -and
        $fa.Offset -eq 0x10000 -and $fa.Size -eq $OysterFactoryPartitionBytes) { return 'single' }
    return 'other'
}

# Number of bytes of $Bytes[$Start .. $Start+$Length) up to and including the last byte that is not 0xFF.
function Get-OysterDataLength {
    param([byte[]]$Bytes, [int]$Start, [int]$Length)
    $i = $Start + $Length
    while ($i - 8 -ge $Start -and [BitConverter]::ToInt64($Bytes, $i - 8) -eq -1) { $i -= 8 }
    while ($i -gt $Start -and $Bytes[$i - 1] -eq 0xFF) { $i-- }
    return ($i - $Start)
}

function Get-OysterSha256Hex {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant()) } finally { $sha.Dispose() }
}

function Get-OysterAscii {
    param([byte[]]$Bytes, [int]$Offset, [int]$Count)
    $b = New-Object byte[] $Count
    [Array]::Copy($Bytes, $Offset, $b, 0, $Count)
    $s = [System.Text.Encoding]::ASCII.GetString($b).Split([char]0)[0]
    return ($s -replace '[^\x20-\x7E]', '?')
}

# Every backup of this MAC that passes the flash gate (size + SHA-256 sidecar), newest first.
function Get-OysterValidBackups {
    param([Parameter(Mandatory = $true)][string]$MacAddress)
    $macDir = Join-Path $OysterBackupRoot (ConvertTo-OysterMacKey -Mac $MacAddress)
    $found = New-Object System.Collections.Generic.List[object]
    if (-not (Test-Path -LiteralPath $macDir)) { return , $found.ToArray() }
    foreach ($d in @(Get-ChildItem -LiteralPath $macDir -Directory | Where-Object { $_.Name -ne 'dualboot' } | Sort-Object Name -Descending)) {
        $bin = Join-Path $d.FullName $OysterBackupFileName
        $shaFile = "$bin.sha256"
        if (-not (Test-Path -LiteralPath $bin) -or -not (Test-Path -LiteralPath $shaFile)) { continue }
        if ((Get-Item -LiteralPath $bin).Length -ne $OysterFlashBytes) { continue }
        $recorded = (("$(Get-Content -LiteralPath $shaFile -TotalCount 1)".Trim()) -split '\s+')[0].ToLowerInvariant()
        if ($recorded -notmatch '^[0-9a-f]{64}$') { continue }
        if ((Get-FileHash -LiteralPath $bin -Algorithm SHA256).Hash.ToLowerInvariant() -ne $recorded) { continue }
        $found.Add([pscustomobject]@{ Path = $bin; Sha256 = $recorded; Stamp = $d.Name })
    }
    return , $found.ToArray()
}

function Show-OysterCommand {
    param([string]$Tool, [string[]]$ArgumentList)
    Write-Host ('    python ' + (ConvertTo-OysterArgString -ArgumentList (@($Tool) + $ArgumentList))) -ForegroundColor DarkGray
}

# esptool read_flash into a temp file, returned as bytes (read-only on the chip).
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

$doApply = $Apply -and -not $DryRun
if (-not $DryRun -and -not $Port) {
    throw '-Port is required (only -DryRun works without a chip). Example: .\scripts\dualboot-setup.ps1 -Port COM5'
}
$csv = Read-OysterPartitionCsv -Path $PartitionCsv
foreach ($n in @('otadata', 'ota_0', 'ota_1')) {
    if (-not $csv.Contains($n)) { throw "$PartitionCsv has no '$n' partition." }
}
$ota1 = $csv['ota_1']
$ota1Hex = '0x{0:X}' -f $ota1.Offset

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

if ($Apply -and $DryRun) { Write-OysterNote '-DryRun wins over -Apply: nothing is written.' }
if ($DryRun) {
    Write-OysterStep 'DRY RUN - no chip access, no file is created'
} elseif ($doApply) {
    Write-OysterStep "APPLY - the original app is written to ota_1 ($ota1Hex) on $Port  (manual step - unverified until run)"
} else {
    Write-OysterStep 'CHECK - read-only (add -Apply to write ota_1)'
}

# ---- 1. which device, and the backup gate ----

if ($DryRun) {
    if ($Mac) {
        $macText = $Mac.Replace('-', ':').ToLowerInvariant()
    } else {
        $cands = @()
        if (Test-Path -LiteralPath $OysterBackupRoot) {
            foreach ($d in @(Get-ChildItem -LiteralPath $OysterBackupRoot -Directory)) {
                if ($d.Name -match '^[0-9a-f]{2}(-[0-9a-f]{2}){5}$') { $cands += $d.Name.Replace('-', ':') }
            }
        }
        if ($cands.Count -eq 0) {
            throw 'No backup folder under _backups\. Take the backup first, before the first Oyster flash: .\scripts\firmware-backup.ps1 -Port COM5'
        }
        if ($cands.Count -gt 1) { throw "$($cands.Count) devices have backups; pass -Mac <aa:bb:cc:dd:ee:ff> to choose one." }
        $macText = $cands[0]
    }
    Write-OysterNote 'dry run: the MAC comes from the backup folder; the chip is not read.'
} else {
    Write-OysterStep 'Reading the chip MAC (read-only)'
    $id = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList ($common + @('flash_id'))
    $info = Get-OysterEsptoolInfo -Text $id.Text
    if ($id.ExitCode -ne 0 -or -not $info.Mac) {
        throw "Could not read the chip on $Port (esptool exit code $($id.ExitCode)). Close any serial monitor, check the cable, or use download mode (docs/TROUBLESHOOTING.md). Nothing was written."
    }
    if (-not $info.Chip -or $info.Chip -notmatch '^ESP32-S3') {
        throw "Expected an ESP32-S3 (Cardputer ADV), esptool reports: $($info.Chip). Nothing was written."
    }
    if ($info.FlashSize -ne '8MB') {
        throw "Expected 8MB of flash (ESP32-S3FN8), esptool reports: $($info.FlashSize). Nothing was written."
    }
    $macText = $info.Mac
    if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after reading the chip. Check the cable and run this again. Nothing was written." }
}
$macKey = ConvertTo-OysterMacKey -Mac $macText

Write-OysterStep 'Backup gate'
$gate = Find-OysterValidBackup -Mac $macText
if (-not $gate) {
    Write-Host ''
    Write-Host "REFUSING: no verified backup for this chip (MAC $macText)." -ForegroundColor Red
    Write-Host 'The original firmware can only be copied out of a full-flash backup taken before the first Oyster flash:'
    Write-Host "    .\scripts\firmware-backup.ps1 -Port $portText" -ForegroundColor Yellow
    Write-Host "A backup counts only if _backups\$macKey\<stamp>\$OysterBackupFileName is exactly $OysterFlashBytes bytes"
    Write-Host "and its SHA-256 matches $OysterBackupFileName.sha256."
    throw 'Refusing to continue without a verified backup. Nothing was written.'
}
Write-Host "    newest verified backup: _backups\$macKey\$($gate.Stamp)\$OysterBackupFileName"

# ---- 2. find the original app in the backups and cut it out ----

Write-OysterStep 'Locating the original app in the backups'
$source = $null
foreach ($b in (Get-OysterValidBackups -MacAddress $macText)) {
    $dump = [System.IO.File]::ReadAllBytes($b.Path)
    $entries = Read-OysterFlashTable -Bytes $dump -Offset 0x8000
    $layout = Get-OysterLayoutName -Entries $entries -Csv $csv
    $factory = @($entries | Where-Object { $_.Type -eq 0 -and $_.Subtype -eq 0 }) | Select-Object -First 1
    if ($dump[0] -ne $ImageMagic) {
        Write-OysterNote "$($b.Stamp): no bootloader image at offset 0 - skipped"
    } elseif ($layout -ne 'other') {
        Write-OysterNote "$($b.Stamp): holds Oyster ($layout layout), not the original - skipped"
    } elseif (-not $factory) {
        Write-OysterNote "$($b.Stamp): no factory app partition in its table - skipped"
    } else {
        $source = [pscustomobject]@{ Backup = $b; Dump = $dump; Factory = $factory }
        break
    }
}
if (-not $source) {
    throw "None of the verified backups of this device holds the original firmware (every one was taken after Oyster was flashed, or has no factory app). Nothing was written. The original exists only if a backup was taken before the first Oyster flash; see docs/DUAL_BOOT.md."
}
$dump = $source.Dump
$fOff = [int]$source.Factory.Offset
$fSize = [int]$source.Factory.Size
Write-Host "    source backup: _backups\$macKey\$($source.Backup.Stamp)\$OysterBackupFileName"
Write-Host ('    factory app:   offset 0x{0:X}, slot size 0x{1:X}' -f $fOff, $fSize)

if ($fOff + $fSize -gt $dump.Length) { throw 'The factory partition runs past the end of the dump. Nothing was written.' }
$used = Get-OysterDataLength -Bytes $dump -Start $fOff -Length $fSize
if ($used -lt 0x100) { throw 'The factory app partition of the backup is blank. Nothing was written.' }
if ($dump[$fOff] -ne $ImageMagic) { throw ('The factory app does not start with the image magic 0xE9 (found 0x{0:X2}). Nothing was written.' -f $dump[$fOff]) }
if ([BitConverter]::ToUInt32($dump, $fOff + 0x20) -ne $AppDescMagic) {
    throw 'The factory app has no app descriptor (magic 0xABCD5432 at +0x20). Nothing was written.'
}
# End of the image according to its own header: 24-byte header, segments, checksum byte (16-byte aligned), SHA-256.
$segCount = [int]$dump[$fOff + 1]
$pos = 24
for ($s = 0; $s -lt $segCount; $s++) {
    if ($pos + 8 -gt $used) { throw 'The segment table of the factory app runs past its data. Nothing was written.' }
    $pos += 8 + [int][BitConverter]::ToUInt32($dump, $fOff + $pos + 4)
}
$pos += 15 - ($pos % 16)
$imageEnd = $pos + 1
if ($dump[$fOff + 23] -eq 1) { $imageEnd += 32 }
if ($imageEnd -gt $fSize) { throw 'The factory app image is larger than its slot. Nothing was written.' }
if ($used -gt $imageEnd) { Write-OysterNote ('data follows the image end (0x{0:X}) inside the slot; it is copied too.' -f $imageEnd) }
$dataEnd = [Math]::Max($used, $imageEnd)
$len = [int]([Math]::Ceiling($dataEnd / 4096.0) * 4096)
if ($len -gt $fSize) { $len = $fSize }
if ($len -gt $ota1.Size) { throw ('The original app (0x{0:X} bytes) does not fit into ota_1 (0x{1:X}). Nothing was written.' -f $len, $ota1.Size) }

$cut = New-Object byte[] $len
[Array]::Copy($dump, $fOff, $cut, 0, $len)
$sha = Get-OysterSha256Hex -Bytes $cut
$d0 = $fOff + 0x20
Write-OysterStep 'The original app'
Write-Host ('    cut:      0x{0:X} bytes (image end 0x{1:X}, last data byte 0x{2:X}, rounded up to 4 KiB)' -f $len, $imageEnd, $used)
Write-Host ('    project:  ' + (Get-OysterAscii -Bytes $dump -Offset ($d0 + 48) -Count 32))
Write-Host ('    version:  ' + (Get-OysterAscii -Bytes $dump -Offset ($d0 + 16) -Count 32))
Write-Host ('    IDF:      ' + (Get-OysterAscii -Bytes $dump -Offset ($d0 + 112) -Count 32))
Write-Host ('    built:    ' + (Get-OysterAscii -Bytes $dump -Offset ($d0 + 96) -Count 16) + ' ' + (Get-OysterAscii -Bytes $dump -Offset ($d0 + 80) -Count 16))
Write-Host "    sha256:   $sha"

$outDir = Join-Path (Join-Path $OysterBackupRoot $macKey) 'dualboot'
$outBin = Join-Path $outDir 'original-app.bin'
if ($DryRun) {
    Write-Host "    would write: $outBin (+ .sha256)"
} else {
    $same = (Test-Path -LiteralPath $outBin) -and ((Get-FileHash -LiteralPath $outBin -Algorithm SHA256).Hash.ToLowerInvariant() -eq $sha)
    if ($same) {
        Write-Host "    file:     $outBin (already there, identical)"
    } else {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
        $partial = "$outBin.partial"
        [System.IO.File]::WriteAllBytes($partial, $cut)
        Move-Item -LiteralPath $partial -Destination $outBin -Force
        Write-Host "    file:     $outBin"
    }
    [System.IO.File]::WriteAllText("$outBin.sha256", "$sha  original-app.bin`n", (New-Object System.Text.ASCIIEncoding))
}

# ---- 3. the chip ----

if ($DryRun) {
    Write-OysterStep 'Plan for a real run (the chip is read first, then -Apply writes)'
    Write-Host '    a. read the chip partition table: it must be the Oyster single-slot or the dual-boot layout'
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('read_flash', '0x8000', '0x1000', '<temp file>'))
    Write-Host "    b. -Apply: skip when ota_1 already holds this file (verify_flash exits 0), else write it:"
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('verify_flash') + $writeFlags + @($ota1Hex, $outBin))
    Show-OysterCommand -Tool $esptool -ArgumentList ($common + @('write_flash') + $writeFlags + @($ota1Hex, $outBin))
    Write-Host '    c. -Apply: verify the chip contents against the file, then restart the device'
    Show-OysterCommand -Tool $esptool -ArgumentList (@('--chip', 'esp32s3', '--port', $portText, '--baud', "$Baud", '--after', 'watchdog_reset', 'verify_flash') + $writeFlags + @($ota1Hex, $outBin))
} else {
    Write-OysterStep 'Reading the chip partition table (read-only)'
    $live = Read-OysterLiveFlash -Tool $esptool -Common $common -Address '0x8000' -Length '0x1000'
    $liveEntries = Read-OysterFlashTable -Bytes $live -Offset 0
    $liveLayout = Get-OysterLayoutName -Entries $liveEntries -Csv $csv
    foreach ($e in $liveEntries) { Write-Host ('    {0,-9} type {1} sub 0x{2:X2}  0x{3:X6} + 0x{4:X}' -f $e.Name, $e.Type, $e.Subtype, $e.Offset, $e.Size) }
    switch ($liveLayout) {
        'single' { Write-Host '    layout: Oyster single-slot (the dual-boot build is not installed yet)' }
        'dualboot' { Write-Host '    layout: dual boot (already installed)' }
        default { Write-OysterWarn 'The partition table on the chip is neither the Oyster single-slot nor the dual-boot layout.' }
    }
    if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after reading the chip. Nothing was written." }

    if (-not $doApply) {
        Write-OysterStep 'Nothing written (no -Apply)'
        Write-Host "    -Apply would write $ota1Hex..0x$('{0:X}' -f ($ota1.Offset + $len)) and verify it."
    } else {
        if ($liveLayout -eq 'other') {
            throw 'Refusing to write: the chip has an unknown partition table, so 0x3B0000 may belong to something else. Nothing was written.'
        }
        if ($liveLayout -eq 'single') {
            Write-OysterWarn 'The old single-slot layout is installed: this overlaps the first part of the old spiffs partition. The stored backend CA is lost; provision again after the dual-boot flash.'
        }
        Write-OysterStep "Is ota_1 ($ota1Hex) already identical? (verify_flash, read-only; 'Verify failed' only means: not yet)"
        $pre = Invoke-OysterEsptoolCapture -Esptool $esptool -ArgumentList ($common + @('verify_flash') + $writeFlags + @($ota1Hex, $outBin))
        if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back. Check the cable and run this again." }
        if ($pre.ExitCode -eq 0) {
            Write-Host '    ota_1 already holds exactly this image; nothing is written.'
        } else {
            Write-OysterStep "Writing 0x$('{0:X}' -f $len) bytes to $ota1Hex (several minutes; do not unplug)"
            Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool) + $common + @('write_flash') + $writeFlags + @($ota1Hex, $outBin))
            if (-not (Wait-OysterPort -Port $Port)) { throw "$Port did not come back after the write. Check the cable and run this again (it is safe to repeat)." }
        }
        Write-OysterStep 'Verifying: the chip hashes ota_1 and compares it with the file'
        Invoke-Native -FilePath $OysterPython -ArgumentList (@($esptool, '--chip', 'esp32s3', '--port', $Port, '--baud', "$Baud", '--after', 'watchdog_reset', 'verify_flash') + $writeFlags + @($ota1Hex, $outBin))
        Write-Host "    ota_1 holds the original app (sha256 $sha)."
    }
}

# ---- 4. next steps ----

Write-OysterStep 'Next steps'
if ((-not $DryRun) -and $liveLayout -eq 'dualboot') {
    Write-Host '    The dual-boot layout is already installed. Open Home > 6 Original Apps on the device.'
} else {
    Write-Host '    1. Flash the dual-boot build (bootloader, the new partition table, the initial otadata, Oyster in ota_0):'
    Write-Host "         .\scripts\firmware-flash.ps1 -Port $portText -Env $DualEnv" -ForegroundColor Yellow
    Write-Host "       equivalent: pio run -e $DualEnv -t upload --upload-port $portText   (in firmware\)"
    Write-Host '    2. Provision the device again (NVS was resized, the LittleFS partition moved):'
    Write-Host '         .\scripts\oyster-up.ps1' -ForegroundColor Yellow
    Write-Host '    3. Home > 6 Original Apps should show "ready". Booting it and coming back is unverified until tried.'
}
Write-Host '    Return paths: reset or power-cycle; if Oyster does not come back, .\scripts\oyster-return.ps1 -Port <COM port>.'
Write-Host '    Full restore of the backup: see the help of this script (Get-Help .\scripts\dualboot-setup.ps1 -Full).'
Write-Host '    Record what you observe in docs/HARDWARE_VERIFICATION.md.'
