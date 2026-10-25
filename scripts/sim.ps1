<#
.SYNOPSIS
  Run the device simulator (a contract-level virtual Cardputer) against the local backend.

.DESCRIPTION
  Needs the backend running in another window (.\scripts\backend.ps1).
  On first use, enrols a simulated device ("oyster enroll-device --simulated") and writes its token to
  backend\data\<DeviceId>.token (not tracked). Re-enrolling an id rotates its token, so this script
  enrols only when that token file is missing. Then runs "oyster-sim run" as that device.
  Everything the simulator produces is labelled source_mode "simulated"; it proves orchestration,
  policy evaluation and reporting - not radio behaviour.
  Scenarios (oyster-sim scenarios): success, denied_access, timeout, malformed_advertisement,
  unexpected_pairing_transition, address_change, transfer_interrupted, vulnerable_target,
  corrected_target, withheld (blind pick for false-positive evaluation).

.EXAMPLE
  .\scripts\sim.ps1 -List
  .\scripts\sim.ps1 -Scenario success -AutoConfirm
  .\scripts\sim.ps1 -Scenario withheld -Seed 7 -SessionFile .\backend\data\withheld-session.txt
#>
[CmdletBinding()]
param(
    [string]$Scenario = 'success',
    [ValidatePattern('^[0-9a-zA-Z_-]{1,40}$')]
    [string]$DeviceId = 'sim-cardputer-01',
    [string]$TokenFile = '',
    [string]$SessionFile = '',
    [switch]$AutoConfirm,
    [Nullable[int]]$Seed = $null,
    [switch]$List
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterSimCli -Hint 'Run .\scripts\setup.ps1.'

if ($List) {
    Invoke-Native -FilePath $OysterSimCli -ArgumentList @('scenarios')
    return
}

Assert-OysterTool -Path $OysterCli -Hint 'Run .\scripts\setup.ps1.'
$base = Get-OysterHttpBase
if (-not (Test-OysterBackend -BaseUrl $base)) {
    throw "Backend not reachable at $base/v1/health. Start it first in another window: .\scripts\backend.ps1"
}

$dataDir = Get-OysterDataDir
if (-not (Test-Path -LiteralPath $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
if (-not $TokenFile) { $TokenFile = Join-Path $dataDir "$DeviceId.token" }

if (-not (Test-Path -LiteralPath $TokenFile)) {
    Write-OysterStep "Enrolling simulated device '$DeviceId' (token written to $TokenFile, not printed)"
    Invoke-Native -FilePath $OysterCli -ArgumentList @('enroll-device', '--id', $DeviceId, '--simulated', '--write-token', $TokenFile)
} else {
    Write-OysterNote "Using existing token file $TokenFile for '$DeviceId'"
}

$simArgs = @('run', '--scenario', $Scenario, '--backend', $base, '--token-file', $TokenFile, '--device-id', $DeviceId)
if ($SessionFile) { $simArgs += @('--session-file', $SessionFile) }
if ($AutoConfirm) { $simArgs += '--auto-confirm' }
$simArgs += '--serve'   # keep polling for jobs like the firmware, until Ctrl+C
if ($null -ne $Seed) { $simArgs += @('--seed', [string]$Seed) }

Write-OysterStep "oyster-sim run --scenario $Scenario  (SIMULATED data; Ctrl+C to stop)"
Invoke-Native -FilePath $OysterSimCli -ArgumentList $simArgs
