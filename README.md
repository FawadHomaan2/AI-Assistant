# Jarvis

A local-first personal AI assistant for Windows. It understands natural
language, controls the computer through native Windows APIs, and keeps you in
control of anything sensitive or destructive.

Say **"Jarvis"** — or press `Ctrl+Space`.

> **Current state: all 12 phases written. Phase 12's gate is not met.**
> Jarvis works with your files, reads documents, starts installed programs,
> manages windows, reports what is running, diagnoses why the machine is slow
> by measuring it, browses the web — navigating, reading pages, and filling in
> forms behind a confirmation gate, remembers preferences you state and recalls
> them in later conversations, and checks this machine's security posture.
> Every action goes through a path jail, a policy engine and a confirmation
> gate, and lands in a tamper-evident audit log.
>
> The browser runs in **Jarvis's own profile, never yours**, and can only
> visit hosts on an allowlist — so a page that tells the model to go somewhere
> else and paste what it just read is refused by the gate rather than by the
> model's good judgement.
>
> Everything through Phase 11 runs and is tested, and **the Windows installer
> now builds** — CI produces a ~30.7 MB `jarvis-installer` artifact on every
> pull request. **It has never been run.** Nothing has been installed, and no
> part of the app has started on Windows: this repository is authored in a Linux
> container, so the tray, the global shortcut, launching applications, window
> management, the Defender/firewall/BitLocker checks, USB history and autostart
> have only ever executed against fakes. See
> [docs/PACKAGING.md](docs/PACKAGING.md) for the checklist that would have to
> pass — nothing on it is ticked.
>
> Plugins run in their own process with no inherited credentials, and can only
> do what their manifest declares, what you approved, and what Jarvis itself
> holds — whichever is narrowest. It is a process boundary, not a sandbox, and
> the interface says so.
>
> Permissions are **folder-scoped, expiring and persisted**: granting write
> access to Desktop does not grant it to Documents, and revoking something
> stays revoked across a restart. Read-only mode makes Jarvis describe an
> action instead of performing it, and rate limits stop a confused loop
> grinding through approvals.
>
> The Security Center **reads and never writes** — it cannot turn Defender or
> the firewall on or off — and something it has not seen before is reported as
> unfamiliar, never as malware. It always says how many checks actually ran.
>
> Memory only acts on things you **stated**, or that it has observed three
> times consistently. Everything it believes is listed, searchable and
> deletable in the Privacy dashboard, with where it came from and how sure it
> is.
>
> **The Windows-specific parts of Phase 4 are written but unverified** — window
> control and application launching call Win32 APIs that cannot be executed in
> this project's Linux CI. The process layer is fully verified, because psutil
> behaves identically on both. See [docs/PHASES.md](docs/PHASES.md) for exactly
> what is and is not confirmed.

---

## Why it's built this way

Full design rationale is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** —
stack choices and what was rejected, the four-plane architecture, threat model,
permission model, agent design, voice pipeline, the computer-control fallback
ladder, database schema, roadmap and known limitations.

The short version:

- **Tauri 2 + React/TS** for the shell. ~10 MB and ~60 MB idle instead of
  Electron's ~150 MB and ~250 MB, which matters for something that sits in the
  tray all day. Deny-by-default capability manifest. NSIS installer built in.
- **Python core** as a supervised sidecar. Every local-AI component this needs
  (whisper, Piper, llama.cpp, ONNX, document parsers, Playwright) is
  Python-first. C# would integrate with Windows more neatly and then force us to
  ship a Python runtime anyway.
- **The model never executes anything.** It emits a typed proposal; a policy
  engine decides; you confirm when it matters. A web page or PDF that tries to
  prompt-inject Jarvis can only *propose* harm, and hits the same gate you would.
- **Native APIs first.** A four-rung ladder — Win32/COM/WMI → UI Automation →
  keyboard synthesis → screen coordinates — and each action records which rung
  it used, so flaky automation is attributable rather than mysterious.
- **Local by default.** Nothing leaves the machine unless you enable a cloud
  provider. Credentials, security findings and the audit log can never leave,
  enforced by type rather than by a setting.

### What Jarvis will never do
No keylogging, no credential harvesting, no covert screen or mic capture, no
hidden persistence, no antivirus or UAC evasion, no touching other people's
machines or accounts. These are absent by design, not switched off — see
ARCHITECTURE.md §6.

---

## Design

Dark-first, because this lives in your tray all day. Deep blue-neutral surfaces,
a cyan accent, and four semantic colours that are used only where they mean
something:

| Token | Used for |
|---|---|
| `--accent` cyan | interactive elements, active nav, focus |
| `--success` green | healthy state, approved actions |
| `--warning` amber | 75–90% resource load, medium-risk confirmations |
| `--danger` rose | emergency stop, high/critical confirmations, permanent actions |
| `--notice` indigo | "not implemented" markers, informational badges |

Two rules hold the system together:

- **Colour never carries state alone.** Every badge, meter and dialog also has
  an icon and a text label, so it stays readable in greyscale, for colour-blind
  users, and in a screenshot.
- **Contrast is verified, not assumed.** `scripts/check_contrast.py` checks every
  foreground/background pair in both themes against WCAG AA and fails CI on a
  regression.

The consent dialog escalates with the risk tier — a medium-risk file move and a
permanent delete are not supposed to look alike — and "this cannot be undone" is
the only field in the whole dialog that gets its own colour.

## Repository layout

```
apps/desktop/          Tauri 2 shell (Rust) + React/TypeScript UI
  src/                 components, views, state, bridge
  src-tauri/           window, tray, global shortcut, metrics, emergency stop
services/jarvis/         Python core — Phase 2
packages/shared/       JSON Schemas generating both TS and Python types
docs/                  ARCHITECTURE.md, PHASES.md
scripts/make_icons.py  regenerates the app icons from source
```

---

## Running it

### Prerequisites
| | |
|---|---|
| Node.js | 20+ |
| Rust | 1.77+ (`rustup`) |
| Windows | 10 21H2+ / 11, with the WebView2 runtime (preinstalled on Win11) |
| Linux dev only | `libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev` |

### Desktop app
```bash
# One-time: set up the Python core the shell will spawn.
cd services/jarvis
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,secrets]"    # macOS/Linux: .venv/bin/pip

cd ../../apps/desktop
npm install
npm run tauri dev      # starts the shell, which starts the core
```

### Core on its own
```bash
cd services/jarvis
.venv/bin/python -m jarvis        # prints {"jarvis":"ready","port":…,"token":…}
```
Useful for poking the API with `curl`. Logs go to stderr so stdout stays a clean
handshake line for the shell.

### Choosing a model

Out of the box the core runs a **development echo provider**: it reflects your
message back and is explicitly *not* a language model. The UI says so, because a
provider that fabricated plausible answers would make the system look like it
worked when it did not.

For real answers, edit `config.toml` in the Jarvis data folder
(`%LOCALAPPDATA%\Jarvis` on Windows) — the easiest local option:

```toml
[ai]
default = "ollama"

[ai.providers.ollama]
kind = "ollama"
model = "qwen2.5:14b-instruct"
```

Cloud providers additionally need `allow_cloud = true` and an API key, which is
stored in the Windows Credential Manager by name. `config.toml` never holds a
key, and pasting one into the `credential` field is rejected with an explanation.

### UI only, in a browser
```bash
npm run dev            # http://localhost:5183
```
Useful for UI work. There is no desktop shell, so the status panel reports that
live metrics are unavailable instead of inventing numbers — that is the
intended behaviour, not a bug.

### What it can do right now

```
list my downloads
find my pdf files from last month in documents
create a folder called University on my desktop
find duplicate files in downloads
read budget.csv
what programs are running
what is using my cpu
why is my computer slow              → measured findings, each with its threshold
is my internet working               → separates DNS failure from routing failure
how much disk space do I have
open chrome                           → resolved against installed software
close notepad                         → posts WM_CLOSE so it can prompt to save
delete report.pdf from downloads      → refused: fs.delete isn't granted by default
read /etc/passwd                      → refused: protected location
end csrss.exe                         → refused: ending it would crash Windows
```

Open-ended requests ("organise my downloads however you think best") are
declined with a list of what it *can* do. Multi-step planning from free-form
instructions needs the model-driven planner, and guessing would mean moving the
wrong files.

### Checks
```bash
# Frontend
cd apps/desktop
npm run typecheck && npm test && npm run build     # tsc, 79 tests, bundle

# Rust shell
cd src-tauri && cargo clippy --all-targets -- -D warnings && cargo test

# Python core
cd services/jarvis
.venv/bin/python -m pytest -q      # 527 tests, incl. 52 path-jail escape attempts
.venv/bin/ruff check . && .venv/bin/mypy jarvis    # strict

# Palette
python3 scripts/check_contrast.py                  # WCAG AA, both themes
```

### Build the installer (Windows only)
```powershell
pwsh -File scripts/build_windows.ps1    # → apps/desktop/src-tauri/target/release/bundle/nsis/*-setup.exe
```
Use the script rather than calling Tauri directly. `npm run tauri build` on its
own fails: Tauri resolves the sidecar binary at compile time, so
`jarvis-core-<target-triple>.exe` has to be frozen by PyInstaller and placed in
`src-tauri/binaries/` *before* the bundle is built. The script does that in
order, runs both test suites first, and refuses to produce an installer if
either fails — an installer built from failing tests is worse than no installer,
because it looks finished.

It needs Python 3.11, Node 20+, Rust stable with the MSVC toolchain, and the
MSVC build tools; `docs/PACKAGING.md` lists them with versions.

Unsigned builds trigger SmartScreen and may be flagged by antivirus heuristics —
an app that spawns processes and synthesises input looks like malware to a
scanner. Code signing is part of Phase 12; see ARCHITECTURE.md §16 and §18.

---

## Keyboard

| | |
|---|---|
| `Ctrl+Space` | show / hide Jarvis from anywhere (configurable in Settings) |
| `Ctrl+Shift+Esc` | emergency stop, while Jarvis has focus¹ |
| `Enter` | send · `Shift+Enter` newline |
| `Esc` | cancel a confirmation prompt |

¹ Windows claims `Ctrl+Shift+Esc` for Task Manager system-wide, so the
always-visible **STOP ALL ACTIONS** button and the tray item are the reliable
paths.

---

## Honesty rules this project follows

1. No feature is presented as working before it is.
2. Unavailable data renders as `—` with a reason, never as `0`.
3. Security surfaces claim no status they have not actually checked.
4. Success messages are derived from observed state, not from the model's belief
   that something worked (ARCHITECTURE.md §3, the Observer).
5. Verified-on-Linux and needs-Windows-to-verify are stated separately in
   [docs/PHASES.md](docs/PHASES.md).
