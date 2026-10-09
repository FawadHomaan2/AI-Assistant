<#
.SYNOPSIS
    Collect everything needed to diagnose a Jarvis install, into one text file.

.DESCRIPTION
    "Many features are not working" cannot be acted on, and the person reporting
    it should not have to know which of four places to look. This gathers the
    four that matter:

      * what the installed core says it can do (`--selfcheck`)
      * which models are on disk, since every voice feature needs three of them
      * config.toml, which says which AI provider is selected and whether cloud
        is allowed
      * the end of the log, where a refusal explains itself

    It writes one file and prints its path. Nothing is uploaded; the file is
    yours to read or send on.

    **It does not collect secrets.** API keys live in the Windows Credential
    Manager, never in config.toml, and this script reads no credentials. The log
    is written through a redaction processor. Even so, skim the file before
    sending it anywhere — it names your install paths and the models you have.

.EXAMPLE
    pwsh -File scripts/collect_diagnostics.ps1

.EXAMPLE
    # Without a repository checkout, from the installed copy:
    pwsh -c "iwr -useb <raw url of this file> | iex"
#>
[CmdletBinding()]
param(
    # Where to write the report. Defaults to the desktop, so it is easy to find.
    [string] $OutFile = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'jarvis-diagnostics.txt'),

    # How many log lines to include. The log is JSON, one object per line.
    [int] $LogLines = 400
)

$ErrorActionPreference = 'Continue'

$dataDir = Join-Path $env:LOCALAPPDATA 'Jarvis'
$configDir = Join-Path $env:APPDATA 'Jarvis'
$installDir = Join-Path $env:LOCALAPPDATA 'Programs\Jarvis'

$report = [System.Collections.Generic.List[string]]::new()
function Section($title) {
    $report.Add('')
    $report.Add('=' * 72)
    $report.Add("== $title")
    $report.Add('=' * 72)
}
function Line($text) { $report.Add([string]$text) }

Section 'When and where'
Line "collected  : $(Get-Date -Format o)"
Line "os         : $([Environment]::OSVersion.VersionString)"
Line "install    : $installDir"
Line "data       : $dataDir"
Line "config     : $configDir"

# ── Is it even installed, and which version ──────────────────────────────
Section 'The installed app'
if (Test-Path $installDir) {
    Get-ChildItem $installDir -File |
        ForEach-Object { Line ("{0,12:N0}  {1}" -f $_.Length, $_.Name) }
    # The core is a one-folder PyInstaller bundle under core/. If this folder is
    # missing or nearly empty the installer did not carry it, which is the one
    # failure that makes every feature at once appear broken.
    $core = Join-Path $installDir 'core'
    if (Test-Path $core) {
        $files = @(Get-ChildItem $core -Recurse -File)
        Line ''
        Line "core/ holds $($files.Count) files, $('{0:N0}' -f (($files | Measure-Object Length -Sum).Sum)) bytes"
        Line "core/jarvis-core.exe present: $(Test-Path (Join-Path $core 'jarvis-core.exe'))"
    } else {
        Line ''
        Line "core/ IS MISSING. The app cannot start its backend without it."
    }
} else {
    Line "Not found. Either Jarvis is not installed for this user, or it was"
    Line "installed somewhere other than the default per-user location."
}

# ── What the core can actually do ────────────────────────────────────────
Section 'Optional features in this build (core --selfcheck)'
$coreExe = Join-Path $installDir 'core\jarvis-core.exe'
if (Test-Path $coreExe) {
    try {
        # One JSON line on stdout, exit 0, and it touches no data directory.
        $out = & $coreExe --selfcheck 2>&1
        Line ($out -join [Environment]::NewLine)
        Line ''
        Line "exit code: $LASTEXITCODE"
    } catch {
        # The message only. The default rendering appends the failing line of
        # this script and its own path, which buries the one useful sentence.
        Line "Could not run it: $($_.Exception.Message)"
    }
} else {
    Line "Skipped: $coreExe does not exist."
}

# ── Models: every voice feature needs all three ──────────────────────────
Section 'Downloaded models'
$models = Join-Path $dataDir 'models'
if (Test-Path $models) {
    Get-ChildItem $models -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            # Both separators: Windows uses \, and this script is worth being
            # able to run on the other platforms the core supports.
            Line ("{0,12:N0}  {1}" -f $_.Length, $_.FullName.Substring($models.Length).TrimStart('\', '/'))
        }
    if (-not (Get-ChildItem $models -Recurse -File)) { Line '(the folder exists but is empty)' }
} else {
    Line "No models folder. Nothing has been downloaded yet, so voice cannot be"
    Line "ready: it needs the wake word, the Piper voice and the speech model."
}

# ── Which provider is selected, and is cloud allowed ─────────────────────
Section 'config.toml (no keys are stored in it)'
$configFile = Join-Path $configDir 'config.toml'
if (-not (Test-Path $configFile)) { $configFile = Join-Path $dataDir 'config.toml' }
if (Test-Path $configFile) {
    Line "from: $configFile"
    Line ''
    Get-Content $configFile | Where-Object { $_ -notmatch '^\s*#' -and $_.Trim() } | ForEach-Object { Line $_ }
} else {
    Line 'Not found. The core writes it on first start, so this suggests it has'
    Line 'never started successfully.'
}

# ── The log, where refusals explain themselves ───────────────────────────
Section "Last $LogLines log lines"
$log = Join-Path $dataDir 'logs\jarvis.log'
if (Test-Path $log) {
    Line "from: $log  ($('{0:N0}' -f (Get-Item $log).Length) bytes, modified $((Get-Item $log).LastWriteTime))"
    Line ''
    Get-Content $log -Tail $LogLines | ForEach-Object { Line $_ }
} else {
    Line "No log at $log."
    Line 'The core writes one as soon as it starts, so its absence means it has'
    Line 'not started — check the app window for an error about the core.'
}

Section 'Errors and refusals in the whole log'
if (Test-Path $log) {
    $hits = @(Select-String -Path $log -Pattern '"level":\s*"(error|critical|warning)"' -AllMatches |
        Select-Object -Last 80 -ExpandProperty Line)
    if ($hits) { $hits | ForEach-Object { Line $_ } } else { Line '(none)' }
} else {
    Line '(no log to search)'
}

$report | Set-Content -Path $OutFile -Encoding utf8
Write-Host ''
Write-Host "Wrote $OutFile" -ForegroundColor Green
Write-Host 'Skim it, then send it on. It holds no API keys or passwords.'
