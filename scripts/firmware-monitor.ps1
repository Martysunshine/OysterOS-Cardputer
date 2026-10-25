<#
.SYNOPSIS
  Open the Cardputer's USB serial log (115200 baud) with the ESP32 exception decoder.
  MANUAL STEP - unverified until run.

.DESCRIPTION
  Runs "pio device monitor -e cardputer-adv -p <Port>" in firmware\. Ctrl+C exits.
  -Timestamps adds PlatformIO's "time" filter (PC-side time on every line), useful for the cancel-latency
  and soak checks in docs/HARDWARE_VERIFICATION.md.
  -SaveLog adds PlatformIO's "log2file" filter, which writes firmware\platformio-device-monitor-*.log
  (*.log files are not tracked; check a log for anything sensitive before sharing it).
  Close the monitor before flashing or provisioning: a COM port can be open in one program only.
  The firmware never prints the device token, Wi-Fi password or CA; if you ever see one, report it.

.EXAMPLE
  .\scripts\firmware-monitor.ps1 -Port COM5
  .\scripts\firmware-monitor.ps1 -Port COM5 -Timestamps -SaveLog
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^COM\d+$')]
    [string]$Port,
    [switch]$Timestamps,
    [switch]$SaveLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterPio -Hint 'Run .\scripts\setup.ps1 (installs PlatformIO).'

# Refuse early when the port is absent (skipped only if .NET cannot enumerate ports at all).
$ports = $null
try { $ports = @([System.IO.Ports.SerialPort]::GetPortNames()) } catch { $ports = $null }
if ($null -ne $ports -and -not ($ports -contains $Port)) {
    if ($ports.Count -eq 0) { $ports = @('(none)') }
    throw "$Port is not present. Ports seen now: $($ports -join ', '). See docs/TROUBLESHOOTING.md (COM port / driver)."
}

$monArgs = @('device', 'monitor', '-e', $OysterFirmwareEnv, '-p', $Port)
if ($Timestamps -or $SaveLog) {
    # Filters given on the command line are listed explicitly so the exception decoder stays active.
    $monArgs += @('-f', 'esp32_exception_decoder')
    if ($Timestamps) { $monArgs += @('-f', 'time') }
    if ($SaveLog) {
        $monArgs += @('-f', 'log2file')
        Write-OysterNote 'Saving the session to firmware\platformio-device-monitor-*.log'
    }
}

Write-OysterStep "Serial monitor on $Port (Ctrl+C to exit)  - manual step, unverified until run"
Invoke-Native -FilePath $OysterPio -ArgumentList $monArgs -WorkingDirectory $OysterFirmwareDir
