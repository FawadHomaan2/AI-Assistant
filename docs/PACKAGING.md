# Packaging Jarvis for Windows

Produces `AI-Assistant-Setup.exe`: a per-user NSIS installer containing the
Tauri shell, the Python core as a single-file sidecar, and the reference
plugin.

> **The Phase 12 gate is not met.** The installer has never been built, because
> this repository is authored in a Linux container and neither PyInstaller nor
> the WebView2 shell cross-compiles. Everything below is written and
> structurally tested; none of it has produced a `.exe`, and no `.exe` has been
> installed on a clean Windows machine. The checklist at the end is what would
> have to pass.

---

## What the build needs

| Tool | Why | Note |
|---|---|---|
| Windows 10 1809+ or 11 | PyInstaller and the shell both target the host | x64 |
| Python 3.11 | the core | 3.12 untested |
| Node 20+ | the frontend | |
| Rust stable + MSVC build tools | the shell | `rustup default stable-msvc` |
| WebView2 | the webview | Already present on Windows 11; the installer downloads a bootstrapper for Windows 10 |
| A code-signing certificate | see **Signing** | Not optional for a release |

```powershell
pwsh -File scripts/build_windows.ps1
```

The script runs both test suites first and refuses to build an installer if
either fails, because an installer built from failing tests is worse than no
installer: it looks finished.

---

## How it fits together

```
AI-Assistant-Setup.exe
└── %LOCALAPPDATA%\Programs\Jarvis\
    ├── Jarvis.exe                  the Tauri shell (Rust + WebView2)
    ├── jarvis-core.exe             the Python core, frozen by PyInstaller
    └── plugins\word-count\         the reference plugin
```

The shell spawns `jarvis-core.exe`, reads a single JSON line from its stdout —
port, token, pid — and talks to it over loopback HTTP. In a development
checkout the shell falls back to running the Python package directly, so
`npm run tauri dev` works without building the sidecar first.

### Why one file, and why a console

`jarvis-core.exe` is a one-file PyInstaller build. A folder build starts faster
but lays down a couple of thousand files, which an antivirus scans on first
run and an installer has to track. One executable is also one thing to sign.

It is built with `console=True`. The handshake is a line on stdout, and a
windowed build on Windows has no stdout at all — the symptom is a core that
appears to start and never answers. The shell spawns it with `CREATE_NO_WINDOW`,
so no console window is ever visible.

### Why it is ~50 MB and not ~300 MB

The heavy optional dependencies are **excluded**: Whisper, Piper, ONNX Runtime,
Playwright and the document readers. Each is a capability that already reports
itself unavailable with a reason and an install command, so excluding one
degrades a feature instead of crashing the core.

---

## Models

Nothing is bundled. Voice is ~140 MB and semantic memory another ~90 MB, mostly
for features a given install may never turn on.

Models are downloaded from **Settings → Downloadable models**, which shows the
size before anything starts. Every file is verified against a recorded SHA-256:
a model is loaded and executed by an inference runtime, so a substituted
download is a code-execution problem rather than a quality problem. A mismatch
deletes the file and refuses. Downloads land in a `.part` file and are renamed
only after verification, so an interrupted fetch cannot leave a truncated model
that fails mysteriously at load time.

**The checksums in `jarvis/config/models.py` are empty**, and an empty checksum
means the model is not fetchable — Jarvis refuses rather than downloading
something it cannot check, and says so in the interface. Filling them in is a
release task that needs the real files:

```powershell
# For each model, after downloading it by hand from its upstream source:
Get-FileHash -Algorithm SHA256 .\model.bin
```

Record the hash and the URL together. A hash that was never checked against a
real download is worse than an empty one, because it looks finished.

---

## Signing

An unsigned build will:

- trigger SmartScreen on every download, with a dialog most people read as "this
  is malware";
- be quarantined by some antivirus products, because an unsigned binary that
  enumerates processes and spawns PowerShell is indistinguishable from the real
  thing by heuristics alone;
- accumulate no SmartScreen reputation, since reputation attaches to the
  signing certificate.

An EV certificate gets reputation immediately; a standard OV certificate earns
it over weeks of downloads. Set `TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`; the build script warns loudly when they
are absent and continues, because an unsigned build is fine for development.

---

## Uninstalling

The uninstaller stops the core, removes the program, and then asks **once**
whether to delete `%LOCALAPPDATA%\jarvis` — conversation history, learned
memory, the audit log, downloaded models and installed plugins. It defaults to
keeping them.

That is a deliberate choice in both directions. Deleting silently would destroy
a year of history nobody agreed to lose; leaving silently would be lying about
having uninstalled.

---

## The Phase 12 checklist

None of this has been done. It is what "the gate is met" would mean.

**Clean Windows 10 22H2 and Windows 11 23H2 VMs, no developer tools:**

- [ ] The installer runs without administrator rights and without a UAC prompt.
- [ ] WebView2 is fetched on Windows 10, and not on Windows 11 where it ships.
- [ ] The app starts, the tray icon appears, `Ctrl+Space` shows and hides it.
- [ ] The core starts: Settings shows a version and "Connected".
- [ ] A real action works end to end — "list my downloads" lists files.
- [ ] Defender does not quarantine either executable. (Expect a SmartScreen
      warning on an unsigned build; note whether it blocks or merely warns.)
- [ ] The **Windows-only** surfaces that have never run get exercised:
      launching an application, window management, the Defender/firewall/
      BitLocker checks, and USB device history. See `docs/PHASES.md` for the
      full list of what is written but unverified.
- [ ] Autostart registers and survives a reboot.
- [ ] Installing over an existing version replaces the core while it is running.
- [ ] Uninstalling removes the program, asks about data, and honours the answer.
- [ ] Reinstalling after keeping data finds the old database and migrates it.

Record what actually happened against each line. A checklist with ticks nobody
earned is how software ships broken.
