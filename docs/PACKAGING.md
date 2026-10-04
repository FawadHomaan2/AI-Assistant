# Packaging Jarvis for Windows

Produces `AI-Assistant-Setup.exe`: a per-user NSIS installer containing the
Tauri shell, the Python core as a single-file sidecar, and the reference
plugin.

> **The installer builds. The Phase 12 gate is still not met.**
>
> The first attempt failed on a lockfile with no Windows native binaries
> (`Cannot find module './cli.win32-x64-msvc.node'`); that is fixed and guarded
> by `scripts/check_lockfile_platforms.py` — see **A lockfile is platform-shaped**
> below. The job now completes, and CI uploads a `jarvis-installer` artifact of
> about 30.7 MB.
>
> So the build is proven and the packaging holds up on a real Windows runner.
> **Nothing after the double-click is.** No installer has been run, nothing has
> been installed, and no part of the app has started on Windows. A CI runner is
> not a clean machine either — it carries the whole build toolchain, which is
> precisely what the clean VMs below do not. The checklist at the end is what
> the gate means, and nothing on it is ticked.

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

### Why it is large, and what is in it

The voice stack is bundled, and it dominates the download. Measured on a Linux
build of the same spec, by package:

| | Content |
|---|---|
| CTranslate2 (runs the Whisper model) | ~133 MB |
| PyAV (ffmpeg bindings, imported by faster-whisper) | ~100 MB |
| ONNX Runtime (wake word and the Piper voice) | ~92 MB |
| Piper | ~60 MB |
| SciPy and NumPy (openWakeWord's feature pipeline) | ~112 MB |

That compresses to a one-file binary of 216 MB on Linux. **Windows is smaller:
164 MB for the core and about 165 MB for the installer**, measured in CI — no
uvloop, and leaner ffmpeg and CTranslate2 builds. Windows is the shipping
target, so that is the number that counts.

PyAV is the galling one: Jarvis feeds faster-whisper raw PCM frames and never
decodes a media file, but `faster_whisper/__init__.py` imports `decode_audio` on
its first line, so the ffmpeg bindings load whether or not anything uses them.

**What is trimmed.** Piper ships a neural diacritiser per script that needs one,
and they are not equally droppable: `piper/voice.py` imports Hebrew's inside the
branch that uses it and Arabic's at module level. So Nakdimon (21 MB) is
filtered out and a Hebrew Piper voice needs a source install, while Tashkeel
stays — dropping it makes `import piper` fail and there is no spoken reply at
all. That was the first attempt, and the self-check caught it.

**What is still excluded.** Playwright, because it needs a browser engine rather
than just a package — bundling the Python half would add weight without making
web browsing work. And torch, which nothing uses: faster-whisper runs on
CTranslate2.

**What is not bundled at all: the models.** The wake word (~4 MB), the Piper
voice (~62 MB) and Whisper (~74 MB) are downloaded on first use and verified
against a recorded SHA-256, which is why the installer is smaller than the sum
of what it can do.

### The build checks what it actually bundled

PyInstaller finds imports by scanning source, and several of these are loaded by
entry point or by name at runtime, so a bundle can be missing one and still
build, start and pass a smoke test — the feature is simply absent.

Two real cases: the installer shipped for a release without `keyring`, so no API
key could be stored; and `jarvis.tools.documents` loads its readers with
`__import__(module)` where `module` is a variable, so `pypdf`, python-docx,
openpyxl and python-pptx were installed for every build and bundled by none of
them. The app reported "the 'pypdf' package is not installed" for a dependency
that was.

So the frozen binary is asked rather than trusted. `jarvis-core --selfcheck`
prints a JSON line naming every optional package and whether it imports, and
`scripts/build_core.py` fails the build when something importable in the build
environment is missing from the bundle. The expected set is not written down: it
is whatever the build environment has, so installing an extra is enough to
require it, and dropping one leaves no stale assertion behind.

---

## A lockfile is platform-shaped

`npm ci` is reproducible, which is why both CI and `build_windows.ps1` use it.
It is reproducible per platform, though, not across them: npm records the
optional dependencies it actually resolved, and when a tree is already installed
it reads that tree rather than the registry. Regenerate the lockfile on Linux
with `node_modules` present and it keeps `@tauri-apps/cli-linux-x64-*` and drops
the other eleven platforms. Nothing looks wrong — it installs perfectly on
Linux — until `npm ci` runs on Windows and installs a JavaScript wrapper with no
native module behind it.

That is what broke the first Windows build. It is checked explicitly now,
because no Linux job can observe it by running `npm ci`:

```powershell
python scripts/check_lockfile_platforms.py
```

It runs in the CI frontend job and as a pre-flight step in
`build_windows.ps1`. To regenerate the lockfile correctly, give npm no tree to
read from:

```powershell
cd apps/desktop
Move-Item node_modules $env:TEMP\nm ; Remove-Item package-lock.json
npm install --package-lock-only
Remove-Item -Recurse $env:TEMP\nm ; npm ci
```

The package count grows by about fifty. Those are other platforms' binaries,
which npm skips at install time on any host they do not match — so a Windows
install still fetches only Windows binaries.

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

Building the installer is done; it is not on this list, because producing an
artifact says nothing about what happens when someone runs it. None of the
following has been done. It is what "the gate is met" would mean.

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
