<#
.SYNOPSIS
  M1 vertical-slice demo: a real AI model plans a supported operation, a SIMULATED device executes it,
  and the answer cites the simulated evidence. Exports a Markdown report.

.DESCRIPTION
  Steps:
   1. Checks AI_PROVIDER / AI_MODEL / AI_API_KEY (environment or backend\.env). If the real provider is
      not configured, it says so and stops - it never substitutes a fake answer.
   2. Starts the backend in the background (unless one already answers on 127.0.0.1:8000).
   3. Enrols the simulated device (only if backend\data\<DeviceId>.token is missing) and upserts the
      lab target (schemas\examples\target.lab-01.json).
   4. Starts "oyster-sim run --scenario success --auto-confirm --serve" in the background (keeps polling for jobs).
   5. Runs "oyster ask --session <id> <question>" and prints the answer.
   6. Exports the session report to backend\data\reports\.
   7. Stops the processes it started (logs stay in backend\data\logs\).
  All observations in this demo are SIMULATED. It is not a hardware or radio result.

.EXAMPLE
  .\scripts\demo-m1.ps1
  .\scripts\demo-m1.ps1 -Question "Inspect my enrolled BLE sensor and explain what you found."
#>
[CmdletBinding()]
param(
    [string]$Question = 'Check whether my test peripheral exposes its diagnostic data before application authorisation. Show the evidence and explain the impact.',
    [string]$Scenario = 'success',
    [ValidatePattern('^[0-9a-zA-Z_-]{1,40}$')]
    [string]$DeviceId = 'sim-cardputer-01',
    [int]$StartupTimeoutSec = 45,
    [switch]$SkipEnrollTarget,
    # Extra arguments appended to "oyster-sim run" (for example a keep-polling option).
    [string[]]$SimExtraArgs = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv
Assert-OysterTool -Path $OysterCli -Hint 'Run .\scripts\setup.ps1.'
Assert-OysterTool -Path $OysterSimCli -Hint 'Run .\scripts\setup.ps1.'
Assert-OysterEnvFile

# ---- 1. real provider configured? ----
$provider = Get-OysterEnvValue -Name 'AI_PROVIDER' -Default 'groq'
$missing = @()
if ((Get-OysterEnvValue -Name 'AI_MODEL') -eq '') { $missing += 'AI_MODEL' }
if ((Get-OysterEnvValue -Name 'AI_API_KEY') -eq '') { $missing += 'AI_API_KEY' }
if ($provider -eq 'fake') {
    Write-Host 'AI_PROVIDER=fake. The M1 demo needs a real model; the fake provider is for automated tests only.' -ForegroundColor Yellow
    Write-Host 'Set AI_PROVIDER=groq, AI_MODEL and AI_API_KEY in backend\.env (docs/SETUP_WINDOWS.md step 6). Nothing was started.'
    exit 2
}
if ($missing.Count -gt 0) {
    Write-Host "Not configured: $($missing -join ', ') (backend\.env or environment)." -ForegroundColor Yellow
    Write-Host 'The demo stops here instead of producing an answer without a real model.'
    Write-Host 'Add your Groq key and pick a model with "oyster ai-check" (docs/SETUP_WINDOWS.md step 6). Nothing was started.'
    exit 2
}
$admin = Get-OysterEnvValue -Name 'OYSTER_ADMIN_TOKEN'
if ($admin -eq '' -or $admin -eq 'change-me-to-a-long-random-string') {
    throw 'OYSTER_ADMIN_TOKEN in backend\.env is empty or still the placeholder.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dataDir = Get-OysterDataDir
$logDir = Join-Path $dataDir "logs\demo-m1-$stamp"
$reportDir = Join-Path $dataDir 'reports'
New-Item -ItemType Directory -Force -Path $logDir, $reportDir | Out-Null
$tokenFile = Join-Path $dataDir "$DeviceId.token"   # same file as scripts\sim.ps1 uses
$sessionFile = Join-Path $logDir 'session.txt'
$reportFile = Join-Path $reportDir "demo-m1-$stamp.md"
$base = Get-OysterHttpBase

$started = New-Object System.Collections.Generic.List[object]

function Start-Background([string]$Name, [string]$Exe, [string[]]$ArgList, [string]$Dir) {
    $out = Join-Path $logDir "$Name.out.log"
    $err = Join-Path $logDir "$Name.err.log"
    $p = Start-Process -FilePath $Exe -ArgumentList (ConvertTo-OysterArgString $ArgList) -WorkingDirectory $Dir `
        -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $p.Handle   # keeps the handle so ExitCode is readable after the process ends
    $started.Add([pscustomobject]@{ Name = $Name; Process = $p; Err = $err; Out = $out })
    Write-OysterNote "$Name started (pid $($p.Id)); logs: $out"
    return $p
}

function Stop-Started {
    for ($i = $started.Count - 1; $i -ge 0; $i--) {
        $s = $started[$i]
        if (-not $s.Process.HasExited) {
            Write-OysterNote "stopping $($s.Name) (pid $($s.Process.Id))"
            $old = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            # /T: the console-script launcher has a python.exe child that must stop too.
            & taskkill.exe /PID $s.Process.Id /T /F *> $null
            $ErrorActionPreference = $old
        }
    }
}

function Show-LogTail([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Write-Host "---- last lines of $Path"
        Get-Content -LiteralPath $Path -Tail 20 | Out-Host
    }
}

$exitCode = 0
try {
    # ---- 2. backend ----
    if (Test-OysterBackend -BaseUrl $base) {
        Write-OysterStep "Backend already running at $base - using it (it will not be stopped by this script)"
    } else {
        Write-OysterStep 'oyster init-db'
        Invoke-Native -FilePath $OysterCli -ArgumentList @('init-db') -WorkingDirectory $OysterBackendDir
        Write-OysterStep 'Starting backend in the background'
        $be = Start-Background -Name 'backend' -Exe $OysterCli -ArgList @('serve') -Dir $OysterBackendDir
        $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
        while (-not (Test-OysterBackend -BaseUrl $base)) {
            if ($be.HasExited) { Show-LogTail (Join-Path $logDir 'backend.err.log'); throw "backend exited early (code $($be.ExitCode))." }
            if ((Get-Date) -gt $deadline) { Show-LogTail (Join-Path $logDir 'backend.err.log'); throw "backend did not answer $base/v1/health within $StartupTimeoutSec s." }
            Start-Sleep -Milliseconds 500
        }
        Write-OysterNote "backend healthy at $base"
    }

    # ---- 3. enrolment ----
    if (-not (Test-Path -LiteralPath $tokenFile)) {
        Write-OysterStep "Enrolling simulated device '$DeviceId' (token -> $tokenFile, not printed)"
        Invoke-Native -FilePath $OysterCli -ArgumentList @('enroll-device', '--id', $DeviceId, '--simulated', '--write-token', $tokenFile)
    } else {
        Write-OysterNote "Using existing simulated-device token $tokenFile"
    }
    if (-not $SkipEnrollTarget) {
        $target = Join-Path $OysterRoot 'schemas\examples\target.lab-01.json'
        Write-OysterStep 'Enrolling the lab target (target-lab-01) on the backend'
        & $OysterCli enroll-target --file $target
        if ($LASTEXITCODE -ne 0) { Write-OysterWarn "enroll-target exited with $LASTEXITCODE (it may already be enrolled); continuing." }
    }

    # ---- 4. simulator ----
    Write-OysterStep "Starting oyster-sim (scenario '$Scenario', SIMULATED) in the background"
    $simArgs = @('run', '--scenario', $Scenario, '--backend', $base, '--token-file', $tokenFile,
        '--device-id', $DeviceId, '--session-file', $sessionFile, '--auto-confirm',
        '--serve', '--idle-exit', '900') + @($SimExtraArgs)
    $sim = Start-Background -Name 'sim' -Exe $OysterSimCli -ArgList $simArgs -Dir $OysterRoot
    $sessionId = $null
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
    while (-not $sessionId) {
        if (Test-Path -LiteralPath $sessionFile) {
            $raw = (Get-Content -LiteralPath $sessionFile -Raw)
            if ($raw) {
                $raw = $raw.Trim()
                $candidate = $raw
                try {
                    $j = $raw | ConvertFrom-Json
                    if ($j -and ($j.PSObject.Properties.Name -contains 'session_id')) { $candidate = [string]$j.session_id }
                } catch { $candidate = ($raw -split "`r?`n")[0].Trim() }
                if ($candidate -match '^sess-[0-9A-Za-z_-]{1,40}$') { $sessionId = $candidate }
            }
        }
        if (-not $sessionId) {
            if ($sim.HasExited) { Show-LogTail (Join-Path $logDir 'sim.err.log'); throw "oyster-sim exited before writing a session id (code $($sim.ExitCode))." }
            if ((Get-Date) -gt $deadline) { Show-LogTail (Join-Path $logDir 'sim.err.log'); throw "no session id in $sessionFile after $StartupTimeoutSec s." }
            Start-Sleep -Milliseconds 500
        }
    }
    Write-OysterNote "simulated session: $sessionId"

    # The AI's device jobs are executed only while the simulator keeps polling. Do not spend provider
    # requests on a run whose jobs nobody can execute.
    Start-Sleep -Seconds 2
    if ($sim.HasExited) {
        Show-LogTail (Join-Path $logDir 'sim.out.log')
        throw ("oyster-sim exited (code $($sim.ExitCode)) right after registering $sessionId, so nothing would execute " +
            "the AI's device jobs. The AI was not called. oyster-sim runs with --serve and should keep polling; see " +
            "$logDir for its error log.")
    }

    # ---- 5. ask ----
    Write-OysterStep 'oyster ask (real model, simulated device)'
    Write-Host "    Q: $Question"
    Invoke-Native -FilePath $OysterCli -ArgumentList @('ask', '--session', $sessionId, $Question)
    if ($sim.HasExited -and $sim.ExitCode -ne 0) {
        Write-OysterWarn "oyster-sim exited with code $($sim.ExitCode) during the run; check $logDir."
    }

    # ---- 6. report ----
    Write-OysterStep "Exporting report -> $reportFile"
    Invoke-Native -FilePath $OysterCli -ArgumentList @('export-report', '--session', $sessionId, '--format', 'md', '--out', $reportFile)
    Write-Host ''
    Write-Host 'Done. Every observation in this report is SIMULATED (source_mode: simulated).' -ForegroundColor Green
    Write-Host "Report: $reportFile"
    Write-Host "Logs:   $logDir"
} catch {
    Write-Host "DEMO FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Logs: $logDir"
    $exitCode = 1
} finally {
    Stop-Started
}
exit $exitCode
