<#
.SYNOPSIS
  Provision a Cardputer over USB serial: wraps "oyster provision-device --port <Port> ...".
  MANUAL STEP - unverified until run.

.DESCRIPTION
  Enrols the device on the backend (token issued and rotated there) and sends device id, backend
  address, 2.4 GHz Wi-Fi credentials, the token, the dev CA certificate, the current time and the
  enrolled targets using the serial protocol in docs/PROVISIONING.md. The Wi-Fi password is always
  prompted, never passed on the command line. Secrets are never echoed back by the device.
  Every argument after -Port is passed to "oyster provision-device" unchanged; list them with:
      .\scripts\provision.ps1 -Port COM5 --help
  Before running: the backend is running with a dev CA for this PC's LAN IP
  (.\scripts\backend.ps1 -MakeDevCaIp ...), the firmware is flashed, and no serial monitor is open.

.EXAMPLE
  .\scripts\provision.ps1 -Port COM5 --help
  .\scripts\provision.ps1 -Port COM5 --id cardputer-01 --ssid "YourNetwork"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^COM\d+$')]
    [string]$Port,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PassThru = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterCli -Hint 'Run .\scripts\setup.ps1.'
Assert-OysterEnvFile

# Refuse early when the port is absent (skipped only if .NET cannot enumerate ports at all).
$ports = $null
try { $ports = @([System.IO.Ports.SerialPort]::GetPortNames()) } catch { $ports = $null }
if ($null -ne $ports -and -not ($ports -contains $Port)) {
    if ($ports.Count -eq 0) { $ports = @('(none)') }
    throw "$Port is not present. Ports seen now: $($ports -join ', '). See docs/TROUBLESHOOTING.md (COM port / driver)."
}

$provArgs = @('provision-device', '--port', $Port) + @($PassThru)
Write-OysterStep "oyster provision-device --port $Port ...  (manual step - unverified until run)"
Invoke-Native -FilePath $OysterCli -ArgumentList $provArgs -WorkingDirectory $OysterBackendDir
