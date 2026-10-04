<#
.SYNOPSIS
    Build the Jarvis Windows installer.

.DESCRIPTION
    Produces AI-Assistant-Setup.exe. Must run on Windows: PyInstaller does not
    cross-compile, and neither does the WebView2 shell.

    Order matters. The core is built and smoke-tested first, because a Tauri
    bundle that succeeds with a broken sidecar inside it is the worst outcome —
    it installs fine and then fails on first launch, which is where it is most
    expensive to diagnose.

.PARAMETER SkipCore
    Reuse an existing core build. Only for iterating on the shell.

.PARAMETER SkipTests
    Skip the test suites. Not for a release build.

.EXAMPLE
    pwsh -File scripts/build_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipCore,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$core = Join-Path $root 'services/jarvis'
$desktop = Join-Path $root 'apps/desktop'

function Step($message) { Write-Host "`n=== $message ===" -ForegroundColor Cyan }

# ── 0. The things that are easy to forget and expensive to miss ──────────
Step 'Checking the toolchain'
foreach ($tool in @('python', 'npm', 'cargo', 'rustc')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is not on PATH. See docs/PACKAGING.md for what this build needs."
    }
}
if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
    Write-Warning @'
No code-signing key is configured, so the installer will be unsigned.
SmartScreen will warn every user who downloads it, and some antivirus
products will quarantine an unsigned binary that spawns PowerShell.
This is expected for a development build and is not acceptable for release.
See docs/PACKAGING.md.
'@
}

# ── 1. Tests before artefacts ────────────────────────────────────────────
if (-not $SkipTests) {
    Step 'Running the core tests'
    Push-Location $core
    try {
        & python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw 'Core tests failed; not building an installer.' }
    } finally { Pop-Location }

    Step 'Running the frontend tests'
    Push-Location $desktop
    try {
        & npm test
        if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed; not building an installer.' }
    } finally { Pop-Location }
}

# ── 2. The core sidecar ──────────────────────────────────────────────────
if (-not $SkipCore) {
    Step 'Building the core (PyInstaller)'
    & python (Join-Path $root 'scripts/build_core.py')
    if ($LASTEXITCODE -ne 0) { throw 'Core build failed.' }
}

$binaries = Join-Path $desktop 'src-tauri/binaries'
if (-not (Test-Path $binaries) -or -not (Get-ChildItem $binaries -Filter 'jarvis-core-*.exe')) {
    throw "No core binary in $binaries. Run without -SkipCore."
}

# ── 3. The shell and the installer ───────────────────────────────────────
Step 'Installing frontend dependencies'
Push-Location $desktop
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }

    Step 'Building the installer (Tauri + NSIS)'
    & npm run tauri build
    if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed.' }
} finally { Pop-Location }

# ── 4. Report what was produced ──────────────────────────────────────────
$nsis = Join-Path $desktop 'src-tauri/target/release/bundle/nsis'
$installer = Get-ChildItem $nsis -Filter '*-setup.exe' -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $installer) { throw "No installer was produced in $nsis." }

$sizeMb = [math]::Round($installer.Length / 1MB, 1)
Step 'Done'
Write-Host "Installer: $($installer.FullName)"
Write-Host "Size:      $sizeMb MB"
Write-Host ''
Write-Host 'This has NOT been tested on a clean Windows install. The Phase 12 gate' -ForegroundColor Yellow
Write-Host 'is a clean Windows 10 and 11 VM; see docs/PACKAGING.md for the checklist.' -ForegroundColor Yellow
