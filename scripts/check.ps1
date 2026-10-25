<#
.SYNOPSIS
  Run the project's automated checks and report each one as PASS, FAIL or SKIPPED.

.DESCRIPTION
  1. ruff check            (backend, lab, tests, scripts)
  2. pytest                (tests\; exit code 5 = no tests collected -> SKIPPED, not PASS)
  3. oyster validate-schemas
  4. lab_profile_gen.h freshness  (scripts\gen_lab_profile_header.py --check)
  5. pio test -e native    (host unit tests of firmware\lib\oyster_core; SKIPPED without a host g++)
  6. pio run -e cardputer-adv  (firmware compile + link)
  A SKIPPED step always prints why. The script exits 1 if any step FAILS, otherwise 0.
  None of these steps touches a Cardputer; passing them says nothing about hardware behaviour.

.EXAMPLE
  .\scripts\check.ps1
  .\scripts\check.ps1 -SkipFirmware
#>
[CmdletBinding()]
param(
    [switch]$SkipFirmware,
    [switch]$SkipNativeTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv

$results = New-Object System.Collections.Generic.List[object]

function Add-Result([string]$Name, [string]$Status, [string]$Detail, [double]$Seconds) {
    $results.Add([pscustomobject]@{ Step = $Name; Result = $Status; Seconds = [math]::Round($Seconds, 1); Detail = $Detail })
    $color = 'Green'
    if ($Status -eq 'FAIL') { $color = 'Red' } elseif ($Status -eq 'SKIPPED') { $color = 'Yellow' }
    Write-Host ("<== {0}: {1} {2}" -f $Name, $Status, $Detail) -ForegroundColor $color
}

# Runs a native command in $Dir, streaming output; maps the exit code to PASS/FAIL (or SKIPPED codes).
function Invoke-CheckStep {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$ArgList,
        [string]$Dir,
        [int[]]$SkipCodes = @(),
        [string]$SkipReason = ''
    )
    Write-Host ''
    Write-OysterStep "$Name"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $code = $null
    Push-Location -LiteralPath $Dir
    try {
        & $Exe @ArgList | Out-Host
        $code = $LASTEXITCODE
    } catch {
        Add-Result $Name 'FAIL' ("could not run: " + $_.Exception.Message) $sw.Elapsed.TotalSeconds
        return
    } finally {
        Pop-Location
    }
    if ($code -eq 0) { Add-Result $Name 'PASS' '' $sw.Elapsed.TotalSeconds }
    elseif ($SkipCodes -contains $code) { Add-Result $Name 'SKIPPED' "($SkipReason; exit $code)" $sw.Elapsed.TotalSeconds }
    else { Add-Result $Name 'FAIL' "(exit $code)" $sw.Elapsed.TotalSeconds }
}

# 1. ruff
Invoke-CheckStep -Name 'ruff check' -Exe $OysterPython -ArgList @('-m', 'ruff', 'check', '.') -Dir $OysterRoot

# 2. pytest
Invoke-CheckStep -Name 'pytest' -Exe $OysterPython -ArgList @('-m', 'pytest') -Dir $OysterRoot `
    -SkipCodes @(5) -SkipReason 'no tests were collected'

# 3. schema validation
if (Test-Path -LiteralPath $OysterCli) {
    Invoke-CheckStep -Name 'oyster validate-schemas' -Exe $OysterCli -ArgList @('validate-schemas') -Dir $OysterRoot
} else {
    Add-Result 'oyster validate-schemas' 'FAIL' '(oyster CLI not installed - run .\scripts\setup.ps1)' 0
}

# 4. generated lab-profile header
Invoke-CheckStep -Name 'lab_profile_gen.h up to date' -Exe $OysterPython `
    -ArgList @((Join-Path $OysterRoot 'scripts\gen_lab_profile_header.py'), '--check') -Dir $OysterRoot

# 5. host unit tests of oyster_core
if ($SkipFirmware -or $SkipNativeTests) {
    Add-Result 'pio test -e native' 'SKIPPED' '(skipped by command-line switch)' 0
} elseif (-not (Test-Path -LiteralPath $OysterPio)) {
    Add-Result 'pio test -e native' 'FAIL' '(pio not installed - run .\scripts\setup.ps1)' 0
} elseif (-not (Get-Command g++ -ErrorAction SilentlyContinue)) {
    Add-Result 'pio test -e native' 'SKIPPED' '(no host g++ on PATH - run .\scripts\setup.ps1 -InstallWinLibs or set OYSTER_GCC_BIN)' 0
} else {
    Invoke-CheckStep -Name 'pio test -e native' -Exe $OysterPio -ArgList @('test', '-e', 'native') -Dir $OysterFirmwareDir
}

# 6. firmware build
if ($SkipFirmware) {
    Add-Result "pio run -e $OysterFirmwareEnv" 'SKIPPED' '(skipped by -SkipFirmware)' 0
} elseif (-not (Test-Path -LiteralPath $OysterPio)) {
    Add-Result "pio run -e $OysterFirmwareEnv" 'FAIL' '(pio not installed - run .\scripts\setup.ps1)' 0
} else {
    Invoke-CheckStep -Name "pio run -e $OysterFirmwareEnv" -Exe $OysterPio -ArgList @('run', '-e', $OysterFirmwareEnv) -Dir $OysterFirmwareDir
}

Write-Host ''
Write-Host 'Summary (automated checks only - nothing here is a hardware result):' -ForegroundColor Cyan
$results | Format-Table -AutoSize -Wrap | Out-Host
$failed = @($results | Where-Object { $_.Result -eq 'FAIL' }).Count
$skipped = @($results | Where-Object { $_.Result -eq 'SKIPPED' }).Count
$passed = @($results | Where-Object { $_.Result -eq 'PASS' }).Count
Write-Host "PASS $passed  FAIL $failed  SKIPPED $skipped"
if ($failed -gt 0) { exit 1 }
exit 0
