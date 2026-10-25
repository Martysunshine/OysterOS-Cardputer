# Oyster Pocket - shared environment for scripts\*.ps1. Dot-source it:  . "$PSScriptRoot\_env.ps1"
# Works in PowerShell 7 and Windows PowerShell 5.1.
# Sets paths ($OysterRoot, $OysterPython, ...), the CA-bundle variables used by PlatformIO / requests
# (REQUESTS_CA_BUNDLE, SSL_CERT_FILE -> .venv\oyster-ca-bundle.pem, regenerated when missing), and puts
# the venv Scripts folder and the WinLibs host g++ (when installed) on PATH for this process only.
# Certificate verification is never disabled (docs/adr/ADR-003-transport-tls.md, docs/TROUBLESHOOTING.md).
# Scripts set Set-StrictMode / $ErrorActionPreference themselves; this file does not, so that
# ". .\scripts\_env.ps1" in an interactive window does not change how that window handles errors.

$OysterRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$OysterVenv = Join-Path $OysterRoot '.venv'
$OysterVenvScripts = Join-Path $OysterVenv 'Scripts'
$OysterPython = Join-Path $OysterVenvScripts 'python.exe'
$OysterPio = Join-Path $OysterVenvScripts 'pio.exe'
$OysterCli = Join-Path $OysterVenvScripts 'oyster.exe'
$OysterSimCli = Join-Path $OysterVenvScripts 'oyster-sim.exe'
$OysterFixtureCli = Join-Path $OysterVenvScripts 'oyster-fixture.exe'
$OysterCaBundle = Join-Path $OysterVenv 'oyster-ca-bundle.pem'
$OysterBackendDir = Join-Path $OysterRoot 'backend'
$OysterFirmwareDir = Join-Path $OysterRoot 'firmware'
$OysterLabDir = Join-Path $OysterRoot 'lab'
$OysterEnvFile = Join-Path $OysterBackendDir '.env'
$OysterFirmwareEnv = 'cardputer-adv'
$OysterFactoryPartitionBytes = 0x500000   # firmware/partitions_8mb_noota.csv, "factory" app slot
$OysterFlashBytes = 0x800000              # the whole 8 MB flash (what firmware-backup.ps1 dumps)
$OysterBackupRoot = Join-Path $OysterRoot '_backups'   # <mac>\<yyyymmdd-hhmmss>\factory-8MB.bin; local only, never commit
$OysterBackupFileName = 'factory-8MB.bin'

function Write-OysterStep {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-OysterNote {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Write-OysterWarn {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

# Runs a native program, streams its output, throws when the exit code is not 0.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = ''
    )
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    try {
        & $FilePath @ArgumentList
        $code = $LASTEXITCODE
    } finally {
        if ($WorkingDirectory) { Pop-Location }
    }
    if ($code -ne 0) {
        throw "'$([System.IO.Path]::GetFileName($FilePath)) $($ArgumentList -join ' ')' failed with exit code $code"
    }
}

# Quotes arguments for Start-Process, which joins -ArgumentList with spaces and does not quote paths.
function ConvertTo-OysterArgString {
    param([string[]]$ArgumentList = @())
    $parts = foreach ($a in $ArgumentList) {
        if ($a -eq '') { '""' }
        elseif ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' }
        else { $a }
    }
    return ($parts -join ' ')
}

function Assert-OysterVenv {
    if (-not (Test-Path -LiteralPath $OysterPython)) {
        throw "Python venv not found at $OysterVenv. Run .\scripts\setup.ps1 first (docs/SETUP_WINDOWS.md)."
    }
}

function Assert-OysterTool {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Hint)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$([System.IO.Path]::GetFileName($Path)) not found in $OysterVenvScripts. $Hint"
    }
}

# The backend reads backend\.env, but settings may also come from the process environment (as with
# python-dotenv, the environment wins). Require one of the two to provide OYSTER_ADMIN_TOKEN.
function Assert-OysterEnvFile {
    if (-not (Test-Path -LiteralPath $OysterEnvFile) -and -not $env:OYSTER_ADMIN_TOKEN) {
        throw "backend\.env not found. Run .\scripts\setup.ps1 (it creates one from .env.example) or copy .env.example to backend\.env and fill it in."
    }
}

# Reads one setting the same way the backend does: the process environment wins, then backend\.env
# (python-dotenv rules for the simple KEY=VALUE lines used in .env.example). Never prints values.
function Get-OysterEnvValue {
    param([Parameter(Mandatory = $true)][string]$Name, [string]$Default = '')
    $fromProcess = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -ne $fromProcess) { return $fromProcess }
    $found = $null
    if (Test-Path -LiteralPath $OysterEnvFile) {
        foreach ($line in Get-Content -LiteralPath $OysterEnvFile) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            if ($t.StartsWith('export ')) { $t = $t.Substring(7).TrimStart() }
            $eq = $t.IndexOf('=')
            if ($eq -lt 1) { continue }
            if ($t.Substring(0, $eq).Trim() -ne $Name) { continue }
            $v = $t.Substring($eq + 1).Trim()
            if ($v.Length -ge 1 -and ($v[0] -eq '"' -or $v[0] -eq "'")) {
                $close = $v.IndexOf($v[0], 1)
                if ($close -gt 0) { $v = $v.Substring(1, $close - 1) } else { $v = $v.Substring(1) }
            } else {
                $v = ($v -replace '\s+#.*$', '').Trim()
            }
            $found = $v
        }
    }
    if ($null -ne $found) { return $found }
    return $Default
}

# OYSTER_DATA_DIR resolved like backend/oyster/settings.py (relative paths are relative to backend\).
function Get-OysterDataDir {
    $d = Get-OysterEnvValue -Name 'OYSTER_DATA_DIR' -Default './data'
    if (-not [System.IO.Path]::IsPathRooted($d)) { $d = Join-Path $OysterBackendDir $d }
    return [System.IO.Path]::GetFullPath($d)
}

function Get-OysterHttpBase {
    $port = Get-OysterEnvValue -Name 'OYSTER_HTTP_PORT' -Default '8000'
    return "http://127.0.0.1:$port"
}

# True when GET /v1/health on the local HTTP listener answers {"status":"ok"}.
function Test-OysterBackend {
    param([string]$BaseUrl = (Get-OysterHttpBase), [int]$TimeoutSec = 2)
    try {
        $r = Invoke-RestMethod -Uri "$BaseUrl/v1/health" -Method Get -TimeoutSec $TimeoutSec
        return ($null -ne $r -and $r.status -eq 'ok')
    } catch {
        return $false
    }
}

function Find-OysterWinLibsBin {
    if ($env:OYSTER_GCC_BIN -and (Test-Path -LiteralPath (Join-Path $env:OYSTER_GCC_BIN 'g++.exe'))) {
        return $env:OYSTER_GCC_BIN
    }
    $roots = @()
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') }
    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles 'WinGet\Packages') }
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $pkgs = @(Get-ChildItem -LiteralPath $root -Directory -Filter 'BrechtSanders.WinLibs*' -ErrorAction SilentlyContinue)
        foreach ($pkg in $pkgs) {
            $bin = Join-Path $pkg.FullName 'mingw64\bin'
            if (Test-Path -LiteralPath (Join-Path $bin 'g++.exe')) { return $bin }
        }
    }
    return $null
}

function Add-OysterPathEntry {
    param([Parameter(Mandatory = $true)][string]$Directory)
    $entries = @($env:PATH -split ';' | Where-Object { $_ -ne '' })
    foreach ($e in $entries) {
        if ($e.TrimEnd('\') -ieq $Directory.TrimEnd('\')) { return }
    }
    $env:PATH = "$Directory;$env:PATH"
}

# Creates .venv\oyster-ca-bundle.pem (certifi + Windows trust stores) when missing.
function Initialize-OysterCaBundle {
    param([switch]$Force)
    if ((Test-Path -LiteralPath $OysterCaBundle) -and -not $Force) { return $true }
    if (-not (Test-Path -LiteralPath $OysterPython)) { return $false }
    $script = Join-Path $OysterRoot 'scripts\make_ca_bundle.py'
    & $OysterPython $script $OysterCaBundle | Out-Host
    return ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $OysterCaBundle))
}

# ---- serial port and flash-backup helpers (firmware-backup.ps1, firmware-flash.ps1) ----

# True when the COM port is listed, or when .NET cannot enumerate ports at all (then nothing is blocked).
function Test-OysterPortPresent {
    param([Parameter(Mandatory = $true)][string]$Port)
    $ports = $null
    try { $ports = @([System.IO.Ports.SerialPort]::GetPortNames()) } catch { return $true }
    return ($ports -contains $Port)
}

function Assert-OysterPort {
    param([Parameter(Mandatory = $true)][string]$Port)
    if (Test-OysterPortPresent -Port $Port) { return }
    $seen = @()
    try { $seen = @([System.IO.Ports.SerialPort]::GetPortNames()) } catch { $seen = @() }
    if ($seen.Count -eq 0) { $seen = @('(none)') }
    throw "$Port is not present. Ports seen now: $($seen -join ', '). Check the cable (data, not charge-only) and the power switch; see docs/TROUBLESHOOTING.md."
}

# Waits until the port is listed again (the ESP32-S3 USB port can drop for a moment after a reset).
function Wait-OysterPort {
    param([Parameter(Mandatory = $true)][string]$Port, [int]$TimeoutSec = 15)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-OysterPortPresent -Port $Port)) {
        if ((Get-Date) -gt $deadline) { return $false }
        Start-Sleep -Milliseconds 500
    }
    return $true
}

# esptool.py bundled with PlatformIO (package tool-esptoolpy). Nothing is downloaded; $null when it is not installed.
function Find-OysterEsptool {
    $cores = @()
    if ($env:PLATFORMIO_CORE_DIR) { $cores += $env:PLATFORMIO_CORE_DIR }
    $userHome = [Environment]::GetFolderPath('UserProfile')
    if ($userHome) { $cores += (Join-Path $userHome '.platformio') }
    foreach ($core in $cores) {
        $p = Join-Path $core 'packages\tool-esptoolpy\esptool.py'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Assert-OysterEsptool {
    $p = Find-OysterEsptool
    if (-not $p) {
        throw 'esptool.py (PlatformIO package tool-esptoolpy) not found in the PlatformIO packages folder. Run .\scripts\setup.ps1; it installs the firmware platform together with its tools.'
    }
    return $p
}

# Runs esptool.py with the venv Python, shows its output and returns @{ExitCode; Text}. For short, parseable output.
# Long transfers (read_flash) use Invoke-Native instead, so their progress line is not held back.
function Invoke-OysterEsptoolCapture {
    param([Parameter(Mandatory = $true)][string]$Esptool, [Parameter(Mandatory = $true)][string[]]$ArgumentList)
    $lines = New-Object System.Collections.Generic.List[string]
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # Windows PowerShell 5.1 turns native stderr text into a terminating error otherwise
    $code = 1
    try {
        & $OysterPython $Esptool @ArgumentList 2>&1 | ForEach-Object { $s = "$_"; $lines.Add($s); Write-Host "    $s" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{ ExitCode = $code; Text = ($lines -join "`n") }
}

# Picks chip description, MAC, flash size and esptool version out of esptool output (banner + flash_id).
function Get-OysterEsptoolInfo {
    param([Parameter(Mandatory = $true)][string]$Text)
    $r = [ordered]@{ Chip = $null; Mac = $null; FlashSize = $null; EsptoolVersion = $null }
    if ($Text -match '(?im)^Chip is (.+?)\s*$') { $r.Chip = $Matches[1] }
    if ($Text -match '(?im)^MAC:\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s*$') { $r.Mac = $Matches[1].ToLowerInvariant() }
    if ($Text -match '(?im)^Detected flash size:\s*(\S+)') { $r.FlashSize = $Matches[1] }
    if ($Text -match '(?im)^esptool\.py v(\S+)') { $r.EsptoolVersion = $Matches[1] }
    return [pscustomobject]$r
}

# "AA:BB:CC:DD:EE:FF" -> "aa-bb-cc-dd-ee-ff" (a valid folder name).
function ConvertTo-OysterMacKey {
    param([Parameter(Mandatory = $true)][string]$Mac)
    $k = $Mac.Trim().ToLowerInvariant().Replace(':', '-')
    if ($k -notmatch '^[0-9a-f]{2}(-[0-9a-f]{2}){5}$') { throw "Not a MAC address: $Mac" }
    return $k
}

# Newest backup that passes the flash gate for this chip MAC: <root>\<mac>\<stamp>\factory-8MB.bin is exactly
# $OysterFlashBytes long and its SHA-256 equals the one recorded in factory-8MB.bin.sha256. $null when none.
function Find-OysterValidBackup {
    param([Parameter(Mandatory = $true)][string]$Mac, [string]$BackupRoot = $OysterBackupRoot)
    $macDir = Join-Path $BackupRoot (ConvertTo-OysterMacKey -Mac $Mac)
    if (-not (Test-Path -LiteralPath $macDir)) { return $null }
    foreach ($d in @(Get-ChildItem -LiteralPath $macDir -Directory | Sort-Object Name -Descending)) {
        $bin = Join-Path $d.FullName $OysterBackupFileName
        $shaFile = "$bin.sha256"
        if (-not (Test-Path -LiteralPath $bin) -or -not (Test-Path -LiteralPath $shaFile)) { continue }
        if ((Get-Item -LiteralPath $bin).Length -ne $OysterFlashBytes) { continue }
        $first = "$(Get-Content -LiteralPath $shaFile -TotalCount 1)".Trim()
        $recorded = ($first -split '\s+')[0].ToLowerInvariant()
        if ($recorded -notmatch '^[0-9a-f]{64}$') { continue }
        $actual = (Get-FileHash -LiteralPath $bin -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -eq $recorded) { return [pscustomobject]@{ Path = $bin; Sha256 = $actual; Stamp = $d.Name } }
    }
    return $null
}

# ---- applied on dot-source ----
if (Test-Path -LiteralPath $OysterVenvScripts) { Add-OysterPathEntry -Directory $OysterVenvScripts }

if (Initialize-OysterCaBundle) {
    $env:REQUESTS_CA_BUNDLE = $OysterCaBundle
    $env:SSL_CERT_FILE = $OysterCaBundle
} elseif (Test-Path -LiteralPath $OysterPython) {
    Write-OysterWarn "Could not create $OysterCaBundle. Downloads by PlatformIO may fail with certificate errors (docs/TROUBLESHOOTING.md)."
}

$OysterGccBin = Find-OysterWinLibsBin
if ($OysterGccBin) { Add-OysterPathEntry -Directory $OysterGccBin }
