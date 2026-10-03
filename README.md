# Juno

A local-first personal AI assistant for Windows. It understands natural
language, controls the computer through native Windows APIs, and keeps you in
control of anything sensitive or destructive.

Say **"Juno"** — or press `Ctrl+Space`.

> **Current state: Phase 1 of 12 — the desktop interface.**
> The shell, tray, global shortcut, live machine stats, consent dialog and
> emergency stop are built and tested. The AI core, the tools, voice and
> security monitoring are **not implemented yet**, and the UI says so on every
> surface rather than showing placeholder data. See
> [docs/PHASES.md](docs/PHASES.md).

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
  prompt-inject Juno can only *propose* harm, and hits the same gate you would.
- **Native APIs first.** A four-rung ladder — Win32/COM/WMI → UI Automation →
  keyboard synthesis → screen coordinates — and each action records which rung
  it used, so flaky automation is attributable rather than mysterious.
- **Local by default.** Nothing leaves the machine unless you enable a cloud
  provider. Credentials, security findings and the audit log can never leave,
  enforced by type rather than by a setting.

### What Juno will never do
No keylogging, no credential harvesting, no covert screen or mic capture, no
hidden persistence, no antivirus or UAC evasion, no touching other people's
machines or accounts. These are absent by design, not switched off — see
ARCHITECTURE.md §6.

---

## Repository layout

```
apps/desktop/          Tauri 2 shell (Rust) + React/TypeScript UI
  src/                 components, views, state, bridge
  src-tauri/           window, tray, global shortcut, metrics, emergency stop
services/juno/         Python core — Phase 2
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
cd apps/desktop
npm install
npm run tauri dev      # full app: window, tray, global shortcut, live metrics
```

### UI only, in a browser
```bash
npm run dev            # http://localhost:5183
```
Useful for UI work. There is no desktop shell, so the status panel reports that
live metrics are unavailable instead of inventing numbers — that is the
intended behaviour, not a bug.

### Checks
```bash
npm run typecheck                        # tsc
npm test                                 # 45 frontend tests
npm run build                            # tsc + production bundle
cd src-tauri && cargo test               # 7 Rust tests, incl. real metrics
cd src-tauri && cargo clippy --all-targets
```

### Build the installer (Windows only)
```bash
cd apps/desktop
npm run tauri build     # → src-tauri/target/release/bundle/nsis/*-setup.exe
```
Unsigned builds trigger SmartScreen and may be flagged by antivirus heuristics —
an app that spawns processes and synthesises input looks like malware to a
scanner. Code signing is part of Phase 12; see ARCHITECTURE.md §16 and §18.

---

## Keyboard

| | |
|---|---|
| `Ctrl+Space` | show / hide Juno from anywhere (configurable in Settings) |
| `Ctrl+Shift+Esc` | emergency stop, while Juno has focus¹ |
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
