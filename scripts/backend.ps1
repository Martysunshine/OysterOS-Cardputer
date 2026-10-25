<#
.SYNOPSIS
  Start the Oyster Pocket backend in this window (Ctrl+C stops it).

.DESCRIPTION
  Runs "oyster init-db" (idempotent) and then "oyster serve". Configuration comes from backend\.env.
  Listeners (docs/API.md): HTTP on 127.0.0.1:8000 for the CLI/simulator, HTTPS on
  OYSTER_BIND_HOST:8443 for the Cardputer (needs the dev CA from "oyster make-dev-ca").

  -MakeDevCaIp <LAN-IP> first creates the private dev CA and a server certificate for that IP
  (needed once before provisioning a real Cardputer; re-run when the PC's LAN IP changes).
  The first HTTPS start may trigger a Windows Firewall prompt for port 8443 - allow "Private networks"
  only (docs/TROUBLESHOOTING.md).

.EXAMPLE
  .\scripts\backend.ps1
  .\scripts\backend.ps1 -MakeDevCaIp 192.168.1.50
#>
[CmdletBinding()]
param(
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')]
    [string]$MakeDevCaIp = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterCli -Hint 'Run .\scripts\setup.ps1.'
Assert-OysterEnvFile

$admin = Get-OysterEnvValue -Name 'OYSTER_ADMIN_TOKEN'
if ($admin -eq '' -or $admin -eq 'change-me-to-a-long-random-string') {
    throw 'OYSTER_ADMIN_TOKEN in backend\.env is empty or still the placeholder. Set a long random value (setup.ps1 does this for a new .env).'
}

if ($MakeDevCaIp) {
    Write-OysterStep "Creating the dev CA and a server certificate for $MakeDevCaIp"
    Invoke-Native -FilePath $OysterCli -ArgumentList @('make-dev-ca', '--ip', $MakeDevCaIp) -WorkingDirectory $OysterBackendDir
}

$dataDir = Get-OysterDataDir
$cert = Get-OysterEnvValue -Name 'OYSTER_TLS_CERT' -Default (Join-Path $dataDir 'dev-ca\server.crt')
if (-not [System.IO.Path]::IsPathRooted($cert)) { $cert = Join-Path $OysterBackendDir $cert }
if (-not (Test-Path -LiteralPath $cert)) {
    Write-OysterWarn "No server certificate at $cert. The Cardputer (HTTPS) listener needs one: run .\scripts\backend.ps1 -MakeDevCaIp <this PC's LAN IP>. The local HTTP listener for CLI/simulator is unaffected."
}

$provider = Get-OysterEnvValue -Name 'AI_PROVIDER' -Default 'groq'
$model = Get-OysterEnvValue -Name 'AI_MODEL'
$key = Get-OysterEnvValue -Name 'AI_API_KEY'
if ($provider -eq 'fake') {
    Write-OysterWarn 'AI_PROVIDER=fake: answers come from the test provider and are labelled fake. Use it only for tests and simulation.'
} elseif ($model -eq '' -or $key -eq '') {
    Write-OysterWarn 'AI_MODEL or AI_API_KEY is empty: AI runs will end as "ai_unavailable" (no answer is invented). See docs/SETUP_WINDOWS.md step 6.'
}

Write-OysterStep 'oyster init-db'
Invoke-Native -FilePath $OysterCli -ArgumentList @('init-db') -WorkingDirectory $OysterBackendDir

Write-OysterStep "oyster serve  (local HTTP: $(Get-OysterHttpBase); Ctrl+C to stop)"
Invoke-Native -FilePath $OysterCli -ArgumentList @('serve') -WorkingDirectory $OysterBackendDir
