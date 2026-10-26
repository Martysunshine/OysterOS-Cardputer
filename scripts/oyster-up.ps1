<#
.SYNOPSIS
  One command to get a Cardputer talking to this PC: certificate, backend, firewall check, provisioning.
  MANUAL STEP - unverified until run on a real device.

.DESCRIPTION
  Steps (each is skipped when it is already done):
   1. Finds this PC's LAN IPv4 (the interface with a default gateway; override with -LanIp).
   2. Makes sure the backend's server certificate names that IP; creates the private dev CA and a certificate
      when it does not ("oyster make-dev-ca"). A new CA means the device must be provisioned again, which
      step 5 does.
   3. Starts the backend in the background (HTTP on 127.0.0.1 for the CLI, HTTPS on the LAN for the Cardputer)
      and waits for /v1/health. Logs: backend\data\logs\up-<timestamp>\. Stop it with -Stop.
   4. Checks for a Windows Firewall rule that lets the Cardputer reach port 8443 and prints the one command
      to run in an elevated PowerShell when there is none (this script never elevates itself).
   5. Provisions the device over USB serial (docs/PROVISIONING.md): device id, backend address, Wi-Fi, token,
      CA, clock and the enrolled lab targets. The Wi-Fi password is ALWAYS typed at the prompt of the
      provisioning tool; it never goes on a command line, into a log or into this window's history.
   6. Seeds the demo data for the module tiles (`oyster seed-demo`): this PC as a read-only host and the Desk profile.

  The Cardputer's Wi-Fi is 2.4 GHz only. The serial port is opened with DTR/RTS off, so it cannot reset the chip.

.PARAMETER Ssid
  The 2.4 GHz network the Cardputer should join. Asked for when omitted.

.PARAMETER DeviceId
  Device id to enrol (default cardputer-01).

.PARAMETER Port
  The device's COM port. Found automatically (Espressif USB, VID 303A) when omitted.

.PARAMETER LanIp
  Use this IPv4 instead of the detected one (it must be the address the Cardputer reaches this PC at).

.PARAMETER SkipProvision
  Only prepare the certificate and the backend.

.PARAMETER Stop
  Stop the backend that this script started (or any "oyster serve" of this project) and exit.

.PARAMETER DryRun
  Print what would be done. Starts nothing, writes nothing, opens no serial port.

.EXAMPLE
  .\scripts\oyster-up.ps1
  .\scripts\oyster-up.ps1 -Ssid "MyNetwork24"
  .\scripts\oyster-up.ps1 -Stop
#>
[CmdletBinding()]
param(
    [string]$Ssid = '',
    [ValidatePattern('^[0-9a-zA-Z_-]{1,40}$')]
    [string]$DeviceId = 'cardputer-01',
    [ValidatePattern('^(COM\d+)?$')]
    [string]$Port = '',
    [ValidatePattern('^(\d{1,3}(\.\d{1,3}){3})?$')]
    [string]$LanIp = '',
    [switch]$SkipProvision,
    [switch]$Stop,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterCli -Hint 'Run .\scripts\setup.ps1.'
Assert-OysterEnvFile

function Get-ServeProcesses {
    # "oyster serve" of THIS project (the venv's oyster.exe or its python -m launcher), nothing else.
    $needle = [regex]::Escape($OysterVenvScripts)
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -match $needle -and $_.CommandLine -match '\bserve\b'
    })
}

if ($Stop) {
    $procs = Get-ServeProcesses
    if ($procs.Count -eq 0) { Write-OysterNote 'No backend of this project is running.'; return }
    foreach ($p in $procs) {
        Write-OysterStep "Stopping backend process $($p.ProcessId)"
        if (-not $DryRun) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    return
}

# ---- 1. LAN address ---------------------------------------------------------------------------------------
if (-not $LanIp) {
    $cfg = Get-NetIPConfiguration -ErrorAction SilentlyContinue | Where-Object {
        $_.IPv4DefaultGateway -and $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up' -and
        $_.InterfaceAlias -notmatch 'Tailscale|vEthernet|VirtualBox|VMware|Loopback'
    } | Select-Object -First 1
    if (-not $cfg) { throw 'Could not find the PC''s LAN address. Connect to the Wi-Fi network the Cardputer will use, or pass -LanIp.' }
    $LanIp = [string]$cfg.IPv4Address[0].IPAddress
}
Write-OysterStep "This PC's LAN address: $LanIp  (the Cardputer must be on the same network)"

# ---- 2. certificate ---------------------------------------------------------------------------------------
$dataDir = Get-OysterDataDir
$certPath = Get-OysterEnvValue -Name 'OYSTER_TLS_CERT' -Default (Join-Path $dataDir 'dev-ca\server.crt')
if (-not [System.IO.Path]::IsPathRooted($certPath)) { $certPath = Join-Path $OysterBackendDir $certPath }

function Test-CertNamesIp {
    param([string]$Path, [string]$Ip)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        # PEM -> DER by hand: works in Windows PowerShell 5.1 as well as PowerShell 7 (no CreateFromPem there).
        $pem = Get-Content -LiteralPath $Path -Raw
        $b64 = (($pem -split "`n") | Where-Object { $_ -notmatch '^-----' }) -join ''
        $der = [Convert]::FromBase64String($b64.Trim())
        $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList (, $der)
        $san = $c.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.17' } | Select-Object -First 1
        if (-not $san) { return $false }
        return ($san.Format($true) -match ('IP Address=' + [regex]::Escape($Ip) + '(\s|$)'))
    } catch {
        return $false
    }
}

$certOk = Test-CertNamesIp -Path $certPath -Ip $LanIp
$regenerated = $false
if ($certOk) {
    Write-OysterNote 'The server certificate already names this address.'
} elseif ($DryRun) {
    Write-OysterNote "[dry run] would run: oyster make-dev-ca --ip $LanIp (the device is then provisioned again)"
} else {
    Write-OysterStep "Creating the dev CA and a server certificate for $LanIp"
    Invoke-Native -FilePath $OysterCli -ArgumentList @('make-dev-ca', '--ip', $LanIp) -WorkingDirectory $OysterBackendDir
    $regenerated = $true
}

# ---- 3. backend ---------------------------------------------------------------------------------------------
Write-OysterStep 'Backend'
$running = Test-OysterBackend
if ($running -and $regenerated) {
    Write-OysterNote 'The certificate changed: restarting the backend so it serves the new one.'
    foreach ($p in (Get-ServeProcesses)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $running = $false
}
if ($running) {
    Write-OysterNote 'Already running (HTTP /v1/health answers).'
} elseif ($DryRun) {
    Write-OysterNote '[dry run] would start: oyster init-db; oyster serve (hidden, logs under backend\data\logs\up-*)'
} else {
    $logDir = Join-Path $dataDir ('logs\up-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Invoke-Native -FilePath $OysterCli -ArgumentList @('init-db') -WorkingDirectory $OysterBackendDir
    $null = Start-Process -FilePath $OysterCli -ArgumentList @('serve') -WorkingDirectory $OysterBackendDir `
        -RedirectStandardOutput (Join-Path $logDir 'backend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'backend.err.log') -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(45)
    while (-not (Test-OysterBackend) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-OysterBackend)) { throw "The backend did not answer within 45 s. Read $logDir\backend.err.log" }
    Write-OysterNote "Started. Logs: $logDir"
}

# ---- 4. firewall ----------------------------------------------------------------------------------------------
Write-OysterStep 'Windows Firewall (the Cardputer connects to port 8443 from the LAN)'
$httpsPort = [int](Get-OysterEnvValue -Name 'OYSTER_HTTPS_PORT' -Default '8443')
$allowed = $false
$fwChecked = $false
try {
    # Two bulk queries (per-rule queries take minutes on a machine with many rules).
    $viaPort = @(Get-NetFirewallPortFilter -Protocol TCP -ErrorAction Stop | Where-Object { $_.LocalPort -contains "$httpsPort" } |
        Get-NetFirewallRule -ErrorAction SilentlyContinue)
    $viaProg = @(Get-NetFirewallApplicationFilter -ErrorAction Stop |
        Where-Object { $_.Program -and $_.Program -match 'python[0-9.]*\.exe$' } |
        Get-NetFirewallRule -ErrorAction SilentlyContinue)
    $hit = @(($viaPort + $viaProg) | Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' })
    $allowed = $hit.Count -gt 0
    $fwChecked = $true
} catch {
    Write-OysterNote "Could not read the firewall rules without administrator rights ($($_.Exception.Message))."
}
if ($allowed) {
    Write-OysterNote "An inbound allow rule for port $httpsPort or the Python program exists."
} else {
    $why = if ($fwChecked) { "No inbound rule found for port $httpsPort." } else { 'The rules could not be read.' }
    Write-OysterWarn "$why If the device cannot connect, run this once in an ELEVATED PowerShell (Private networks only):"
    Write-Host "    New-NetFirewallRule -DisplayName 'Oyster backend $httpsPort' -Direction Inbound -Protocol TCP -LocalPort $httpsPort -Profile Private -Action Allow" -ForegroundColor Yellow
}

# ---- 5. provisioning --------------------------------------------------------------------------------------------
if ($SkipProvision) { Write-OysterNote 'Provisioning skipped (-SkipProvision).'; return }

if (-not $Port) {
    $found = @()
    try {
        $found = @(Get-PnpDevice -PresentOnly -Class Ports -ErrorAction Stop | Where-Object { $_.InstanceId -match 'VID_303A' } |
            ForEach-Object { if ($_.FriendlyName -match '\((COM\d+)\)') { $Matches[1] } })
    } catch { $found = @() }
    if ($found.Count -eq 0) { $found = @([System.IO.Ports.SerialPort]::GetPortNames()) }
    if ($found.Count -eq 1) {
        $Port = $found[0]
    } elseif ($found.Count -eq 0) {
        throw 'No serial port found. Plug the Cardputer in with a data cable (docs/TROUBLESHOOTING.md), or pass -Port COMx.'
    } else {
        throw "More than one serial port ($($found -join ', ')). Pass -Port COMx."
    }
}
Write-OysterStep "Provisioning $DeviceId over $Port"
if (-not $Ssid -and -not $DryRun) { $Ssid = Read-Host '2.4 GHz Wi-Fi network (SSID) for the Cardputer' }
if ($DryRun) {
    Write-OysterNote "[dry run] would run: provision.ps1 -Port $Port --id $DeviceId --ssid <ssid>  (the password is prompted)"
    return
}
if (-not $Ssid) { throw 'An SSID is required.' }
& (Join-Path $PSScriptRoot 'provision.ps1') -Port $Port --id $DeviceId --ssid $Ssid
if ($LASTEXITCODE -ne 0) { throw "Provisioning failed (exit code $LASTEXITCODE). Nothing else was changed." }

# The Hosts and Control Deck tiles need something to show: this PC as a read-only host and the Desk profile.
Write-OysterStep 'Seeding this PC as a host and the Desk profile for the device'
Invoke-Native -FilePath $OysterCli -ArgumentList @('seed-demo', '--id', $DeviceId) -WorkingDirectory $OysterBackendDir

Write-OysterStep 'Done'
Write-OysterNote 'The device restarts. Press any key on its start screen. The Home heading should read OYSTER OS (no "setup!")'
Write-OysterNote 'and the status bar should turn to BACKEND OK within a few seconds. Then try Oyster AI (tile 2).'
Write-OysterNote 'Provisioning also loaded the AI key and model from backend/.env, so Oyster AI answers even away'
Write-OysterNote 'from this PC (tile 2 shows "Oyster AI (direct)" when the backend is not reachable).'
Write-OysterNote 'Stop the backend later with: .\scripts\oyster-up.ps1 -Stop'
