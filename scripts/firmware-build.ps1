<#
.SYNOPSIS
  Build the Cardputer ADV firmware (PlatformIO env "cardputer-adv") and report its size.

.DESCRIPTION
  Runs "pio run -e cardputer-adv" and "pio run -e cardputer-adv -t size" in firmware\, then prints
  firmware.bin size, its share of the 5 MiB factory partition, and its SHA-256 (record that hash
  with any hardware result in docs/HARDWARE_VERIFICATION.md). Honours PLATFORMIO_BUILD_DIR if set.
  Building does not touch the device.

.EXAMPLE
  .\scripts\firmware-build.ps1
  .\scripts\firmware-build.ps1 -Clean
#>
[CmdletBinding()]
param([switch]$Clean)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterPio -Hint 'Run .\scripts\setup.ps1 (installs PlatformIO).'

if ($Clean) {
    Write-OysterStep "pio run -e $OysterFirmwareEnv -t clean"
    Invoke-Native -FilePath $OysterPio -ArgumentList @('run', '-e', $OysterFirmwareEnv, '-t', 'clean') -WorkingDirectory $OysterFirmwareDir
}

Write-OysterStep "pio run -e $OysterFirmwareEnv"
Invoke-Native -FilePath $OysterPio -ArgumentList @('run', '-e', $OysterFirmwareEnv) -WorkingDirectory $OysterFirmwareDir

Write-OysterStep "pio run -e $OysterFirmwareEnv -t size"
Invoke-Native -FilePath $OysterPio -ArgumentList @('run', '-e', $OysterFirmwareEnv, '-t', 'size') -WorkingDirectory $OysterFirmwareDir

$buildRoot = Join-Path $OysterFirmwareDir '.pio\build'
if ($env:PLATFORMIO_BUILD_DIR) {
    $buildRoot = $env:PLATFORMIO_BUILD_DIR
    if (-not [System.IO.Path]::IsPathRooted($buildRoot)) { $buildRoot = Join-Path $OysterFirmwareDir $buildRoot }
}
$bin = Join-Path (Join-Path $buildRoot $OysterFirmwareEnv) 'firmware.bin'
if (Test-Path -LiteralPath $bin) {
    $size = (Get-Item -LiteralPath $bin).Length
    $pct = [math]::Round(100.0 * $size / $OysterFactoryPartitionBytes, 1)
    $hash = (Get-FileHash -LiteralPath $bin -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-OysterStep 'Firmware image'
    Write-Host "    file:    $bin"
    Write-Host "    size:    $size bytes ($pct% of the $OysterFactoryPartitionBytes-byte factory partition)"
    Write-Host "    sha256:  $hash"
} else {
    Write-OysterWarn "Build reported success but $bin was not found."
}
