<#
.SYNOPSIS
  One-time (re-runnable) setup of the Oyster Pocket development environment on Windows.

.DESCRIPTION
  1. Finds Python 3.12+ (prefers the "py" launcher, 3.13 first) and creates .venv if missing.
  2. pip-installs backend[dev] and lab[fixture] in editable mode, plus truststore, PlatformIO and Pillow (screen simulator and asset converter),
     constrained by constraints.txt (the versions recorded in docs/BUILD_MATRIX.md).
  3. Builds .venv\oyster-ca-bundle.pem (certifi + Windows trust stores) so PlatformIO downloads work
     behind TLS-inspecting security software with verification still on.
  4. Creates backend\.env from .env.example when it does not exist, filling OYSTER_ADMIN_TOKEN and
     LAB_FIXTURE_AUTH_TOKEN with fresh random values (never printed). AI_API_KEY / AI_MODEL stay empty.
  5. Downloads the pinned firmware platform, toolchain and libraries (pio pkg install).
  6. Optional: -InstallWinLibs installs the WinLibs host GCC with winget (needed only for
     "pio test -e native" host unit tests).

  Does not touch the Cardputer. Safe to run again.

.EXAMPLE
  .\scripts\setup.ps1
  .\scripts\setup.ps1 -InstallWinLibs
#>
[CmdletBinding()]
param(
    [switch]$InstallWinLibs,
    [switch]$SkipFirmwarePackages
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$venv = Join-Path $root '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }

function Get-PythonVersion([string]$exe, [string[]]$prefix) {
    try {
        $out = & $exe @prefix -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
        if ($LASTEXITCODE -eq 0 -and $out) { return [version]("$out".Trim()) }
    } catch { }
    return $null
}

function Invoke-Checked([string]$exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "'$exe $($argList -join ' ')' failed with exit code $LASTEXITCODE" }
}

# ---- 1. Python + venv ----
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step 'Looking for Python 3.12 or newer'
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Exe = 'py'; Prefix = [string[]]@('-3.13') }
        $candidates += [pscustomobject]@{ Exe = 'py'; Prefix = [string[]]@('-3.12') }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $candidates += [pscustomobject]@{ Exe = 'python'; Prefix = [string[]]@() }
    }
    $chosen = $null
    foreach ($c in $candidates) {
        $ver = Get-PythonVersion $c.Exe $c.Prefix
        if ($ver -and $ver -ge [version]'3.12') {
            $chosen = $c
            Write-Host "    found Python $ver ($($c.Exe) $($c.Prefix -join ' '))"
            break
        }
    }
    if (-not $chosen) {
        throw 'Python 3.12+ not found. Install Python 3.13 from python.org (tick "Add python.exe to PATH" or use the py launcher), then re-run.'
    }
    Write-Step "Creating venv at $venv"
    $venvArgs = @($chosen.Prefix) + @('-m', 'venv', $venv)
    Invoke-Checked $chosen.Exe $venvArgs
} else {
    Write-Step "Using existing venv $venv"
}
$pyVer = Get-PythonVersion $venvPython @()
Write-Host "    venv Python $pyVer"
if ($pyVer -and $pyVer -lt [version]'3.12') { throw "The venv uses Python $pyVer; 3.12+ is required. Delete .venv and re-run." }
if ($pyVer -and ($pyVer.Major -ne 3 -or $pyVer.Minor -ne 13)) {
    Write-Host 'WARNING: constraints.txt was recorded with Python 3.13.5; another minor version may need different wheels.' -ForegroundColor Yellow
}

# ---- 2. Python packages ----
$constraints = Join-Path $root 'constraints.txt'
Write-Step 'Upgrading pip'
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Write-Step 'Installing backend[dev], lab[fixture], truststore, PlatformIO and Pillow (editable, constrained)'
# Relative paths from the repo root: the absolute path may contain spaces.
Push-Location -LiteralPath $root
try {
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '-c', $constraints,
        '-e', '.\backend[dev]', '-e', '.\lab[fixture]', 'truststore==0.10.5', 'platformio', 'pillow')
} finally {
    Pop-Location
}

# ---- 3. CA bundle for certifi-based tools ----
Write-Step 'Building .venv\oyster-ca-bundle.pem (certifi + Windows trust stores; verification stays on)'
Invoke-Checked $venvPython @((Join-Path $root 'scripts\make_ca_bundle.py'), (Join-Path $venv 'oyster-ca-bundle.pem'))

# ---- 4. backend\.env ----
$envFile = Join-Path $root 'backend\.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Step 'Creating backend\.env from .env.example with fresh random admin and fixture tokens'
    $template = Get-Content -LiteralPath (Join-Path $root '.env.example')
    $adminToken = (& $venvPython -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    $fixtureToken = (& $venvPython -c "import secrets; print(secrets.token_hex(32))").Trim()
    $lines = foreach ($l in $template) {
        if ($l -match '^OYSTER_ADMIN_TOKEN=') { "OYSTER_ADMIN_TOKEN=$adminToken" }
        elseif ($l -match '^LAB_FIXTURE_AUTH_TOKEN=') { "LAB_FIXTURE_AUTH_TOKEN=$fixtureToken" }
        else { $l }
    }
    # ASCII keeps the file BOM-free in both PowerShell 5.1 and 7 (python-dotenv reads it as UTF-8).
    Set-Content -LiteralPath $envFile -Value $lines -Encoding Ascii
    Write-Host '    backend\.env created (not tracked). Add AI_API_KEY and AI_MODEL yourself - see docs/SETUP_WINDOWS.md.'
} else {
    Write-Step 'backend\.env already exists - left unchanged'
}

# ---- 5. Firmware packages ----
$env:REQUESTS_CA_BUNDLE = Join-Path $venv 'oyster-ca-bundle.pem'
$env:SSL_CERT_FILE = $env:REQUESTS_CA_BUNDLE
$pio = Join-Path $venv 'Scripts\pio.exe'
Invoke-Checked $pio @('--version')
if (-not $SkipFirmwarePackages) {
    Write-Step 'Downloading the pinned firmware platform, toolchain and libraries (first run: several hundred MB)'
    Invoke-Checked $pio @('pkg', 'install', '-d', (Join-Path $root 'firmware'), '-e', 'cardputer-adv')
} else {
    Write-Step 'Skipping firmware package download (-SkipFirmwarePackages)'
}

# ---- 6. Optional host GCC ----
if ($InstallWinLibs) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'winget not found. Install "App Installer" from the Microsoft Store, or install WinLibs manually (docs/SETUP_WINDOWS.md).'
    }
    Write-Step 'Installing WinLibs GCC (POSIX threads, UCRT) with winget'
    Invoke-Checked 'winget' @('install', '--id', 'BrechtSanders.WinLibs.POSIX.UCRT', '--exact', '--source', 'winget',
        '--accept-package-agreements', '--accept-source-agreements')
}

. (Join-Path $PSScriptRoot '_env.ps1')
if ($OysterGccBin) {
    Write-Host "    host g++ found: $OysterGccBin"
} else {
    Write-Host '    host g++ not found: "pio test -e native" will be SKIPPED by check.ps1 (use -InstallWinLibs to add it).'
}

Write-Step 'Setup finished'
Write-Host 'Next: add AI_API_KEY and AI_MODEL to backend\.env, then run .\scripts\check.ps1 (docs/SETUP_WINDOWS.md).'
