# Build phases

A phase is done when it runs, its tests pass, and the errors found while
testing are fixed. The next phase does not start before that.

Legend: **done** · *in progress* · planned

---

## Phase 1 — Desktop interface · **done**

Shipped:
- Tauri 2 shell: window, system tray with the full menu, configurable global
  shortcut (`Ctrl+Space`), single-instance guard, close-to-tray, autostart plugin.
- React UI: five views (Assistant, Security, Activity, Privacy, Settings), chat
  surface, composer, voice button, quick actions, activity log.
- **Real** CPU / memory / disk / network / uptime / battery from the Rust
  `sysinfo` crate — no mocked values anywhere.
- Consent dialog implementing the §7 contract, including typed-phrase
  confirmation for critical actions.
- Emergency stop: UI button, `Ctrl+Shift+Esc`, tray item, Rust-side latch.
- 45 frontend tests, 7 Rust tests. `tsc`, `vite build`, `cargo clippy` clean.

Deliberately absent, and stated as such in the UI: the AI core, every tool,
voice capture, security checks, persistence.

**Verified on Linux:** frontend build + tests, Rust compile + tests.
**Needs Windows to verify:** tray rendering, global shortcut registration,
WebView2 rendering, the NSIS installer.

---

## Phase 2 — AI core · **done**

Shipped:
- **Python core** (`services/jarvis`) spawned and supervised by the Rust shell.
  It binds an ephemeral loopback port, mints a one-time bearer token, and writes
  a single JSON handshake line to stdout; the token never touches disk.
- **Transport:** FastAPI over loopback, bearer auth with constant-time compare,
  Origin/Host checks, and a WebSocket that streams agent events into the UI.
- **Provider gateway:** one interface, four implementations — Anthropic,
  OpenAI-compatible (covers LM Studio / vLLM / llama.cpp server / OpenRouter /
  Azure), Ollama, and a development echo. Per-job routing, typed errors.
- **Privacy classes + egress gate:** `SENSITIVE` payloads raise rather than being
  filtered; cloud and file-content egress are separate opt-ins; secrets are
  redacted and payloads size-capped before any network call.
- **Persistence:** SQLite with forward-only migrations, sessions and turns, and a
  hash-chained audit log whose chain is verified through the API.
- **Agents:** rule-based router plus an orchestrator that intercepts every intent
  needing tools and answers with a capability notice.
- Structured logging with a redaction processor; secrets in the OS credential
  store, never in `config.toml`.

**Why the router intercepts.** A capable model asked to "open Chrome" replies
"Done!". Routing a computer task to the model would make Jarvis lie the moment a
real provider is configured, so the agent layer — not a prompt instruction —
stops those requests before any model sees them. There are tests for exactly this.

**Verified on Linux:** 128 core tests (incl. a test that spawns the real process
and checks the handshake, port and token), 71 frontend tests, strict `mypy`,
`ruff`, `tsc`, `cargo clippy`, plus an end-to-end run against the real core over a
real WebSocket: 72 streamed deltas, computer task intercepted, 4 turns persisted,
audit chain intact, emergency stop honoured.
**Needs Windows to verify:** sidecar spawn from the packaged `externalBin`, and
the Credential Manager backend (`keyring` reports no usable backend headlessly).

## Phase 3 — Filesystem & documents · next
`FileSystemTool`, path jail, Policy Engine, consent broker, hash-chained audit
log, `DocumentTool` (PDF/DOCX/XLSX/TXT/CSV read + summarise).
*Gate:* traversal attempts rejected by tests; no tool reachable without the gate.

## Phase 4 — Applications & windows · planned
`ApplicationTool`, `WindowTool`, `ProcessTool` over Win32 (control layer L1).
*Gate:* launch → focus → close round-trip on Windows.

## Phase 5 — System tools & diagnostics · planned
`SystemInfoTool`, `NetworkTool`, allowlisted `PowerShellTool`, `ClipboardTool`,
`NotificationTool`, `ScreenshotTool`, and "why is my PC slow" with evidence.
*Gate:* a diagnostic report that cites measurements, not guesses.

## Phase 6 — Voice · planned
VAD → faster-whisper → core → Piper, openWakeWord ("Jarvis"), push-to-talk,
barge-in, visible mic state.
*Gate:* a spoken command executes end to end on real hardware.

## Phase 7 — Browser · planned
Playwright-driven navigation, extraction, gated form fill, `WebSearchTool`.
*Gate:* navigate + extract + a confirmation-gated form submission.

## Phase 8 — Memory · planned
Four tiers, local embeddings, retrieval, preference learning, Privacy Dashboard
CRUD over stored memory.
*Gate:* a preference stated once is recalled in a later session.

## Phase 9 — Security Center · planned
Defender, firewall, startup items, network connections, USB history, updates,
BitLocker, event log; baseline learning; four-level classification.
*Gate:* findings carry evidence and the correct classification — unfamiliar is
reported as unfamiliar, never as malware.

## Phase 10 — Permissions · planned
Full risk × scope × mode matrix, scope-grant UI, typed-phrase tier-5 confirm,
read-only mode, rate limits.
*Gate:* red-team the gate — no tool reachable without passing it.

## Phase 11 — Plugins · planned
Manifest, process isolation, scoped tool proxy, enable/disable UI, one reference
plugin.
*Gate:* a plugin cannot exceed its declared scopes.

## Phase 12 — Packaging · planned
PyInstaller sidecar, Tauri NSIS installer, shortcuts, uninstaller, autostart,
first-run model fetcher, code signing, update channel.
*Gate:* `AI-Assistant-Setup.exe` installs and runs on a clean Windows 10 and 11 VM.
