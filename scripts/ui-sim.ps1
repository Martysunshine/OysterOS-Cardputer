<#
.SYNOPSIS
  Run the OysterOS 240x135 UI simulator: interactive window, headless PNG export, key-script replay, tests.

.DESCRIPTION
  The simulator draws the firmware's screens from the firmware's own design tokens (firmware\include\ui_theme.h)
  with mock data. It needs no Cardputer, no backend and no network, and it never touches the device.
  Python 3.12+ with Pillow (the repo venv) and tkinter (for the window) are required.
    (no switch)           open the interactive window (scale 4); Alt = Fn, arrows / Esc / Home / F1 also work
    -Headless -Out DIR    write every route in every state as PNG (DIR\firmware, DIR\preview) + contact sheets
    -Script FILE [-Out DIR]  replay a key script (see simulator\examples\navigation.keys.txt); shots go to DIR
    -Test                 run the simulator tests (tests\simulator)
    -Routes / -Keys       print the route table / the key table as Markdown
    -Preview              mock screens for the modules the firmware does not have (Linux, Control Deck, Smart Home)
  Layout, geometry and colours follow the firmware; the glyph bitmaps are extracted from the pinned M5GFX source.
  Exact pixel parity with the real display is UNVERIFIED until compared with photos taken on a device.

.EXAMPLE
  .\scripts\ui-sim.ps1
  .\scripts\ui-sim.ps1 -Headless -Out .\out\ui
  .\scripts\ui-sim.ps1 -Script .\simulator\examples\navigation.keys.txt -Out .\out\nav
  .\scripts\ui-sim.ps1 -Test
#>
[CmdletBinding()]
param(
    [switch]$Headless,
    [string]$Out = '',
    [string]$Script = '',
    [switch]$Test,
    [switch]$Routes,
    [switch]$Keys,
    [switch]$Preview,
    [ValidateRange(1, 16)][int]$Scale = 4
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_env.ps1')
Assert-OysterVenv

$SimDir = Join-Path $OysterRoot 'simulator'
if (-not (Test-Path -LiteralPath (Join-Path $SimDir 'oyster_ui_sim'))) {
    throw "simulator\oyster_ui_sim not found under $OysterRoot."
}

# Paths given by the caller are relative to the caller's location, not to simulator\.
function Resolve-CallerPath([string]$Path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

if ($Test) {
    Write-OysterStep 'Simulator tests (headless, no device, no network)'
    Invoke-Native -FilePath $OysterPython -WorkingDirectory $OysterRoot `
        -ArgumentList @('-m', 'pytest', 'tests/simulator', '-o', 'addopts=', '-q', '-p', 'no:cacheprovider')
    return
}

$simArgs = @('-m', 'oyster_ui_sim', '--scale', [string]$Scale)
if ($Preview) { $simArgs += '--preview' }

if ($Routes) {
    $simArgs += '--routes'
} elseif ($Keys) {
    $simArgs += '--keys'
} elseif ($Headless) {
    if (-not $Out) { throw '-Headless needs -Out <directory>.' }
    $simArgs += @('--headless', '--out', (Resolve-CallerPath $Out))
    Write-OysterStep "Rendering every route and state to PNG in $Out (mock data, 2 profiles)"
} elseif ($Script) {
    $scriptPath = Resolve-CallerPath $Script
    if (-not (Test-Path -LiteralPath $scriptPath)) { throw "Script not found: $scriptPath" }
    $simArgs += @('--script', $scriptPath)
    if ($Out) { $simArgs += @('--out', (Resolve-CallerPath $Out)) }
    Write-OysterStep "Replaying $Script"
} else {
    Write-OysterStep 'Opening the simulator window (mock data; nothing is sent anywhere)'
}

Invoke-Native -FilePath $OysterPython -ArgumentList $simArgs -WorkingDirectory $SimDir
