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

.PARAMETER SkipDeps
    Reuse the installed Python packages. Only for iterating: the installer's
    features are whatever is installed when the core is frozen, so skipping
    this on a fresh environment produces a build missing voice and the
    document readers.

.EXAMPLE
    pwsh -File scripts/build_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipCore,
    [switch]$SkipTests,
    [switch]$SkipDeps
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
Step 'Checking the lockfile carries the Windows native binaries'
& python (Join-Path $root 'scripts/check_lockfile_platforms.py')
if ($LASTEXITCODE -ne 0) {
    throw 'The lockfile has no Windows native binaries; npm ci would install the JS wrappers without them and the Tauri CLI would fail to load. See the output above.'
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

# ── 1. Core dependencies ─────────────────────────────────────────────────
# Installed here, not assumed. The README's own setup is `.[dev,secrets]`,
# which has neither the voice stack nor the document readers — so following it
# and then running this script produced an installer that silently lacked both.
# These are the extras the shipped installer carries, and CI installs the same
# set, so a local build and a release build contain the same features.
#
# `build_core.py` asks the frozen binary what it ended up with, and the answer
# is only as good as what was installed to begin with.
if (-not $SkipDeps) {
    Step 'Installing core dependencies (including the voice stack)'
    Push-Location $core
    try {
        & python -m pip install -e '.[dev,documents,secrets,voice]'
        if ($LASTEXITCODE -ne 0) { throw 'Installing the core dependencies failed.' }
        & python -m pip install pyinstaller
        if ($LASTEXITCODE -ne 0) { throw 'Installing PyInstaller failed.' }
    } finally { Pop-Location }
}

# ── 2. Frontend dependencies ─────────────────────────────────────────────
# Before the tests, not after: on a clean checkout there is no node_modules, so
# `npm test` fails with "vitest is not recognised" and the old ordering reported
# that as a test failure, which sent you looking at the tests instead of at the
# install that never happened.
Step 'Installing frontend dependencies'
Push-Location $desktop
try {
    & npm ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
} finally { Pop-Location }

# ── 3. Tests before artefacts ────────────────────────────────────────────
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

# ── 4. The core sidecar ──────────────────────────────────────────────────
if (-not $SkipCore) {
    Step 'Building the core (PyInstaller)'
    & python (Join-Path $root 'scripts/build_core.py')
    if ($LASTEXITCODE -ne 0) { throw 'Core build failed.' }
}

$binaries = Join-Path $desktop 'src-tauri/binaries'
if (-not (Test-Path $binaries) -or -not (Get-ChildItem $binaries -Filter 'jarvis-core-*.exe')) {
    throw "No core binary in $binaries. Run without -SkipCore."
}

# ── 5. The shell and the installer ───────────────────────────────────────
Step 'Building the installer (Tauri + NSIS)'
Push-Location $desktop
try {
    & npm run tauri build
    if ($LASTEXITCODE -ne 0) { throw 'Tauri build failed.' }
} finally { Pop-Location }

# ── 6. Report what was produced ──────────────────────────────────────────
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
