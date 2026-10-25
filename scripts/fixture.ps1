<#
.SYNOPSIS
  Live BLE lab fixture on this PC (native WinRT GATT server, docs/adr/ADR-008-native-winrt-fixture.md).

.DESCRIPTION
  check        Read-only capability check: Bluetooth adapter present, LE peripheral role supported,
               GATT server API available. Run this first; never assume the adapter can do it.
               Exit 0 = ready, 1 = not ready.
  run          Publish and advertise the lab GATT profile (schemas/lab-profile.json) until Ctrl+C.
               -Mode is required:
                 vulnerable = omits the application-authorisation check on the diagnostic transfer
                              (deliberately wrong, for the demonstration);
                 corrected  = enforces it.
               The mode is shown locally only and is never exposed over GATT.
  reset-state  Ask the running fixture to clear authorisation, challenge, transfer and virtual-LED
               state (ADR-007). Exit 3 = no running fixture answered.
  synth        Write the synthetic diagnostic object, its manifest and SHA-256 to -Out
               (default lab\data\synth, kept out of version control) for checking a transferred file.

  "run" needs LAB_FIXTURE_AUTH_TOKEN (>= 32 bytes; setup.ps1 puts a random one in backend\.env). This
  script copies it from backend\.env into the fixture's process environment without printing it.
  Live BLE behaviour with a Cardputer is manual step - unverified until run.

.EXAMPLE
  .\scripts\fixture.ps1 check
  .\scripts\fixture.ps1 run -Mode vulnerable
  .\scripts\fixture.ps1 reset-state
  .\scripts\fixture.ps1 synth
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('check', 'run', 'reset-state', 'synth')]
    [string]$Action = 'check',
    [ValidateSet('vulnerable', 'corrected')]
    [string]$Mode = '',
    [string]$Out = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterFixtureCli -Hint 'Run .\scripts\setup.ps1 (installs lab[fixture]).'

$fxArgs = @()
$token = $null
switch ($Action) {
    'check' { $fxArgs = @('check') }
    'run' {
        if (-not $Mode) { throw 'fixture run needs -Mode vulnerable or -Mode corrected.' }
        $token = Get-OysterEnvValue -Name 'LAB_FIXTURE_AUTH_TOKEN'
        if ($token -eq '') {
            throw 'LAB_FIXTURE_AUTH_TOKEN is empty (environment and backend\.env). Run .\scripts\setup.ps1 for a new .env, or set a random value of at least 32 characters.'
        }
        $fxArgs = @('run', '--mode', $Mode)
    }
    'reset-state' { $fxArgs = @('reset-state') }
    'synth' {
        if (-not $Out) { $Out = Join-Path $OysterLabDir 'data\synth' }
        $fxArgs = @('synth', '--out', $Out)
    }
}

Write-OysterStep "oyster-fixture $($fxArgs -join ' ')"
if ($Action -eq 'run') { Write-OysterNote 'Ctrl+C stops the GATT server. Synthetic data only.' }
# The token is handed to the child through this process's environment and removed again afterwards,
# so it does not linger in your PowerShell session. It is never printed.
$prevToken = [Environment]::GetEnvironmentVariable('LAB_FIXTURE_AUTH_TOKEN', 'Process')
Push-Location -LiteralPath $OysterRoot
try {
    if ($null -ne $token) { [Environment]::SetEnvironmentVariable('LAB_FIXTURE_AUTH_TOKEN', $token, 'Process') }
    $token = $null
    & $OysterFixtureCli @fxArgs
    $code = $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable('LAB_FIXTURE_AUTH_TOKEN', $prevToken, 'Process')
    Pop-Location
}

if ($code -ne 0) {
    switch ($Action) {
        'check' { Write-OysterWarn 'This PC cannot host the live fixture as it is (see the matrix above and docs/TROUBLESHOOTING.md#bluetooth-peripheral-role-check). The simulator still works.' }
        'reset-state' { Write-OysterWarn 'No running fixture acknowledged the reset. A fresh "fixture.ps1 run" always starts unauthorised.' }
        default { Write-OysterWarn "oyster-fixture exited with code $code." }
    }
}
exit $code
