# Jarvis — Personal AI Computer Assistant

**Architecture & Design Document**
Target platform: Windows 10 21H2+ / Windows 11 (x64, ARM64 best-effort)
Status: architecture approved → Phase 1 implemented

---

## 0. The name

**Jarvis.** Wake phrase: *"Jarvis"* or *"Hey Jarvis"* (configurable).

The wake word is load-bearing — a bad one makes voice mode unusable — and this
one happens to be the strongest available choice on the merits, not only the
most familiar:

| Criterion | Why `Jarvis` works |
|---|---|
| **Pretrained models already exist** | `openWakeWord` ships `hey_jarvis` as one of its official pretrained ONNX models, and Picovoice Porcupine has `jarvis` as a built-in keyword. No training data to collect, no custom model to build, and detection accuracy comes from a model trained on far more speakers than we could gather. This is the decisive advantage. |
| Acoustically distinct | `/ˈdʒɑːr.vɪs/` — affricate onset (`dʒ`), open back vowel, rhotic centre, fricative coda (`s`). The onset/coda contrast is easy for a keyword spotter to lock onto. |
| Rare in conversation | Practically never occurs in ordinary English speech, so the false-accept rate stays low. |
| Two syllables | Long enough for a reliable spotting window (~1s), short enough to say all day. |

Rejected: *Iris* / *Atlas* / *Nova* (common words or heavily branded → false
triggers), *Vera* / *Kai* (too close to common names, and single-syllable
confusables). Any name without a pretrained model would mean recording wake-word
samples before voice mode worked at all.

The wake word lives in config (`voice.wake_word`), so it can be changed without a
rebuild — at the cost of needing a custom model for anything outside each
engine's built-in keyword list.

---

## 1. Recommended technology stack

### Decision summary

| Layer | Choice | Version |
|---|---|---|
| Desktop shell | **Tauri 2** (Rust + WebView2) | 2.x |
| UI | **React 18 + TypeScript + Vite** | 18 / 5.x / 5.x |
| Styling | Hand-rolled CSS custom properties (no UI framework) | — |
| Backend / agent runtime | **Python 3.11+ + FastAPI + Uvicorn** (Tauri *sidecar*) | 3.11–3.12 |
| Transport | HTTP (localhost) + WebSocket for streaming | — |
| Windows integration | `pywin32`, `comtypes`, `uiautomation`, `psutil`, `pythonnet` (optional) | — |
| LLM abstraction | Custom provider interface → llama.cpp / Ollama / OpenAI-compatible / Anthropic | — |
| STT | **faster-whisper** (CTranslate2, local) | — |
| TTS | **Piper** (local, ONNX) with Windows SAPI5 fallback | — |
| Wake word | **openWakeWord** (ONNX, local) | — |
| Browser automation | **Playwright** (Chromium, opt-in) | — |
| Database | **SQLite** via `sqlite3` + WAL, with `sqlite-vec` for embeddings | — |
| Secrets | **Windows Credential Manager** (DPAPI-backed) via `keyring` | — |
| Packaging | Tauri NSIS → `AI-Assistant-Setup.exe` + PyInstaller for the sidecar | — |

### Why this stack, and what was rejected

**Backend: Python, not C#/.NET.**
This is the single most consequential choice. C# would give cleaner, first-party
access to Win32/WinRT/UI Automation and a single-runtime deployment. But *every*
local-AI component this project needs — `faster-whisper`, `piper`, `openWakeWord`,
`llama-cpp-python`, `onnxruntime`, `sentence-transformers`, document parsers
(`pypdf`, `python-docx`, `openpyxl`, `python-pptx`), `playwright` — is Python-first.
Choosing C# means either reimplementing that stack or shipping a Python runtime
*anyway* and paying for a second IPC hop. Python's Windows access is genuinely
good: `pywin32` is a thin, complete Win32 binding, and `uiautomation`/`comtypes`
wrap the same `IUIAutomation` COM interfaces C# would call. The ecosystem argument
decisively outweighs the integration-purity argument.

**Shell: Tauri 2, not Electron.**
| | Tauri 2 | Electron |
|---|---|---|
| Installed size (shell only) | ~10 MB | ~150 MB |
| Idle RAM (tray-resident) | ~40–80 MB | ~200–300 MB |
| Renderer | System WebView2 (patched by Windows Update) | Bundled Chromium (our patch burden) |
| Security model | Capability/permission manifest, deny-by-default | Full Node in main; contextIsolation is opt-in discipline |
| Installer | NSIS/MSI generated natively | electron-builder (also good) |
| Sidecar | First-class `externalBin` | Manual `child_process` |
| Build prerequisite | Rust toolchain + WebView2 | Node only |

This app is **tray-resident all day**, so idle RAM is a real cost, not a benchmark
stat. Tauri's deny-by-default capability manifest also lets the OS-level permission
surface mirror the app's own permission model instead of fighting it. The cost is a
Rust toolchain in the build chain — acceptable, and Rust is a good place for the
privileged IPC bridge and the emergency-stop kill switch.
*Fallback:* if WebView2 or Rust friction becomes blocking, the frontend is plain
React and ports to Electron in roughly a day — nothing in `apps/desktop/src`
depends on Tauri except a single thin `src/lib/bridge.ts` adapter.

**No UI component framework.** Tailwind/MUI/shadcn would add build weight and a
design language we'd then fight to re-theme. The UI is ~15 components with a strict
two-colour palette; CSS custom properties are a better fit and keep the bundle tiny.

**Transport: localhost HTTP, not stdio.** The sidecar binds `127.0.0.1` on an
ephemeral port, prints the port + a per-launch bearer token on stdout, and Tauri
reads them. HTTP/WS gives us streaming, standard tooling, and the ability to run
the backend standalone during development. Loopback-only + token + `Origin` check
closes the local-attacker gap.

---

## 2. Complete system architecture

Jarvis is a **four-plane** system. The planes are separated so that the plane which
can *do damage* (Execution) is the smallest, most audited, and most constrained.

1. **Presentation plane** — Tauri shell + React UI. Holds no secrets, has no
   OS authority, renders state and collects consent.
2. **Cognition plane** — planner / reasoner / memory. Produces *proposals*. Can
   never touch the OS directly. Treats model output as untrusted.
3. **Execution plane** — the tool registry and its implementations. The only code
   that touches the filesystem, processes, COM, or the network. Every call passes
   the Policy Engine first.
4. **Governance plane** — Policy Engine, consent broker, audit log, emergency stop.
   Sits *between* cognition and execution and is not bypassable: the executor has
   no code path to a tool that does not route through it.

The critical invariant: **the LLM never executes anything.** It emits a structured
tool-call proposal; the Policy Engine decides; the user confirms when required;
only then does the Executor invoke the tool. An LLM that is prompt-injected by a
malicious webpage or document can therefore only *propose* harm, and the proposal
hits the same gate a user request would.

---

## 3. Component diagram

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                           PRESENTATION PLANE                                 │
│                  Tauri 2 shell (Rust)  +  React 18 / TS (WebView2)           │
│                                                                              │
│   ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌───────────┐ ┌───────────────┐   │
│   │   Chat   │ │  Voice   │ │  System    │ │  Quick    │ │  Activity     │   │
│   │  Thread  │ │  Button  │ │  Status    │ │  Actions  │ │  Log          │   │
│   └──────────┘ └──────────┘ └────────────┘ └───────────┘ └───────────────┘   │
│   ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌───────────┐ ┌───────────────┐   │
│   │ Security │ │ Privacy  │ │  Settings  │ │  Consent  │ │ ■ EMERGENCY   │   │
│   │  Center  │ │Dashboard │ │            │ │  Dialog   │ │   STOP        │   │
│   └──────────┘ └──────────┘ └────────────┘ └───────────┘ └───────────────┘   │
│                                                                              │
│   Rust-native: system tray · global hotkey (Ctrl+Space) · single-instance    │
│                autostart · window mgmt · sidecar supervisor · sysinfo        │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │  HTTP + WebSocket, 127.0.0.1:<ephemeral>
                                │  Bearer <per-launch token>, Origin-checked
┌───────────────────────────────┴──────────────────────────────────────────────┐
│                     JARVIS CORE  —  Python sidecar (FastAPI)                    │
│                                                                              │
│  ┌────────────────────────── COGNITION PLANE ──────────────────────────────┐ │
│  │                                                                          │ │
│  │   ┌────────────┐    ┌────────────┐    ┌────────────┐   ┌─────────────┐  │ │
│  │   │  ROUTER    │──▶ │  PLANNER   │──▶ │  EXECUTOR  │──▶│  OBSERVER   │  │ │
│  │   │ intent /   │    │ task graph │    │ step loop  │   │ pre/post    │  │ │
│  │   │ complexity │    │ (DAG)      │    │ + repair   │   │ state diff  │  │ │
│  │   └────────────┘    └────────────┘    └─────┬──────┘   └──────┬──────┘  │ │
│  │          │                 ▲                │                 │         │ │
│  │          ▼                 │                │                 ▼         │ │
│  │   ┌────────────┐    ┌──────┴─────┐          │          ┌─────────────┐  │ │
│  │   │  CRITIC    │    │  MEMORY    │◀─────────┴─────────▶│ REFLECTOR   │  │ │
│  │   │ plan review│    │ 4-tier     │                     │ outcome →   │  │ │
│  │   └────────────┘    └────────────┘                     │ memory      │  │ │
│  │                            │                           └─────────────┘  │ │
│  └────────────────────────────┼──────────────────────────────────────────────┘ │
│                               │                                               │
│  ┌──────────────── GOVERNANCE PLANE  (NOT BYPASSABLE) ─────────────────────┐ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌────────────┐ ┌─────────────────┐   │ │
│  │  │   POLICY     │ │   CONSENT    │ │   AUDIT    │ │  EMERGENCY      │   │ │
│  │  │   ENGINE     │ │   BROKER     │ │   LOG      │ │  STOP           │   │ │
│  │  │ risk · scope │ │ prompt user  │ │ append-    │ │ cancel-token    │   │ │
│  │  │ path jail    │ │ await answer │ │ only       │ │ broadcast       │   │ │
│  │  │ rate limit   │ │ TTL cache    │ │ hash chain │ │ + tool kill     │   │ │
│  │  └──────────────┘ └──────────────┘ └────────────┘ └─────────────────┘   │ │
│  └───────────────────────────────┬──────────────────────────────────────────┘ │
│                                  │  every call. no exceptions.                │
│  ┌──────────────── EXECUTION PLANE — TOOL REGISTRY ────────────────────────┐ │
│  │  BaseTool: name · description · in/out JSON Schema · risk · scopes ·     │ │
│  │            dry_run() · execute() · undo()? · structured logging          │ │
│  │                                                                          │ │
│  │  FileSystem  Window    Process   Application  Clipboard   Notification   │ │
│  │  Screenshot  OCR       Document  Terminal     PowerShell  Network        │ │
│  │  SystemInfo  Security  Browser   WebSearch    Voice       Calendar/Email │ │
│  └───────────┬──────────────────────────────────────────────────────────────┘ │
│              │                                                                │
│  ┌───────────┴──────────┐  ┌──────────────┐  ┌──────────────┐ ┌────────────┐ │
│  │   WINDOWS ADAPTERS   │  │  AI PROVIDER │  │    VOICE     │ │  PLUGIN    │ │
│  │ Win32 (pywin32)      │  │   GATEWAY    │  │   PIPELINE   │ │  HOST      │ │
│  │ UIA3  (comtypes)     │  │ llama.cpp /  │  │ VAD→STT→Core │ │ manifest + │ │
│  │ WMI/CIM · psutil     │  │ Ollama /     │  │ →TTS, barge- │ │ scoped     │ │
│  │ Shell/COM · WinRT    │  │ OpenAI-compat│  │ in capable   │ │ perms      │ │
│  │ PS (constrained)     │  │ / Anthropic  │  │ wake word    │ │            │ │
│  └──────────┬───────────┘  └──────┬───────┘  └──────┬───────┘ └─────┬──────┘ │
│             │                     │                 │               │        │
│  ┌──────────┴─────────────────────┴─────────────────┴───────────────┴──────┐ │
│  │  PERSISTENCE   SQLite (WAL) + sqlite-vec  │  Windows Credential Manager  │ │
│  │  %LOCALAPPDATA%\Jarvis\jarvis.db · logs\ · models\ · config.toml             │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
                                  │
          ┌───────────────────────┴───────────────────────┐
          ▼                                               ▼
   ┌─────────────┐                                 ┌─────────────┐
   │   WINDOWS   │  Win32 · UIA · WMI · Defender    │  OPTIONAL   │
   │     OS      │  Firewall · Event Log · BitLocker│   CLOUD     │
   └─────────────┘                                 │  (opt-in)   │
                                                   └─────────────┘
```

### Request lifecycle (the hot path)

```text
 "Organize my Downloads folder"
        │
        ▼
 [1] Transport  ──▶ session + turn created, audit entry opened
        ▼
 [2] Router     ──▶ complexity=multi-step, domain=filesystem
        ▼
 [3] Memory     ──▶ retrieve: prefs ("sort by type"), past runs, pinned facts
        ▼
 [4] Planner    ──▶ DAG of steps, each a typed tool-call proposal
        ▼
 [5] Critic     ──▶ "step 5 deletes — downgrade to recycle-bin move"  (plan revised)
        ▼
 [6] Policy     ──▶ per-step: risk, scope, path-jail  →  2 steps need consent
        ▼
 [7] Consent    ──▶ UI: "Move 37 files into 5 new folders?"  [Confirm][Cancel][Details]
        ▼
 [8] Observer   ──▶ pre-state snapshot (file inventory + hashes)
        ▼
 [9] Executor   ──▶ run steps; on failure → repair loop (max N) → or surface error
        ▼
[10] Observer   ──▶ post-state diff; verify the claim matches reality
        ▼
[11] Reflector  ──▶ write outcome + learned preference to memory
        ▼
[12] Report     ──▶ text + voice + activity-log entries + audit close
```

Step **[10]** is what keeps the assistant honest: the success message is generated
from the observed diff, not from the model's belief that it worked.

---

## 4. Project folder structure

```text
AI-Assistant/
├── apps/
│   └── desktop/                      # Tauri 2 + React frontend
│       ├── src/
│       │   ├── main.tsx
│       │   ├── App.tsx
│       │   ├── components/           # ChatThread, VoiceButton, SystemStatus,
│       │   │                         # QuickActions, ActivityLog, ConsentDialog,
│       │   │                         # EmergencyStop, StatusBadge, Sidebar
│       │   ├── views/                # ChatView, SecurityView, PrivacyView,
│       │   │                         # SettingsView, ActivityView
│       │   ├── lib/
│       │   │   ├── bridge.ts         # ONLY file that talks to Tauri (portability seam)
│       │   │   ├── api.ts            # typed client for the Python core
│       │   │   └── ws.ts             # streaming event channel
│       │   ├── state/                # zustand stores
│       │   ├── styles/               # tokens.css + component CSS
│       │   └── types/                # generated from backend JSON Schema
│       ├── src-tauri/
│       │   ├── src/
│       │   │   ├── main.rs
│       │   │   ├── lib.rs
│       │   │   ├── tray.rs           # system tray + menu
│       │   │   ├── hotkey.rs         # configurable global shortcut
│       │   │   ├── sidecar.rs        # spawn/supervise/health-check Python core
│       │   │   ├── sysinfo_cmd.rs    # real CPU/RAM/disk via `sysinfo` crate
│       │   │   └── window.rs         # show/hide/focus/centre
│       │   ├── capabilities/         # Tauri deny-by-default permission manifests
│       │   ├── icons/
│       │   └── tauri.conf.json
│       ├── index.html
│       ├── package.json
│       ├── tsconfig.json
│       └── vite.config.ts
│
├── services/
│   └── jarvis/                         # Python core (the sidecar)
│       ├── jarvis/
│       │   ├── __main__.py           # entrypoint: bind loopback, emit port+token
│       │   ├── app.py                # FastAPI app factory
│       │   ├── config/               # settings model, TOML load/merge, paths
│       │   ├── transport/            # routes/, ws.py, auth.py
│       │   ├── agents/               # router, planner, critic, executor,
│       │   │                         # observer, reflector, orchestrator
│       │   ├── ai/                   # provider ABC + local/openai_compat/
│       │   │                         # anthropic/ollama, prompts/, budget.py
│       │   ├── tools/                # base.py, registry.py, + one module per tool
│       │   ├── governance/           # policy.py, consent.py, audit.py,
│       │   │                         # scopes.py, estop.py, pathjail.py
│       │   ├── windows/              # win32/, uia/, wmi/, powershell/ adapters
│       │   │                         # + posix_stub/ so it imports on non-Windows
│       │   ├── voice/                # vad.py, stt.py, tts.py, wake.py, pipeline.py
│       │   ├── security/             # defender.py, firewall.py, startup.py,
│       │   │                         # netconn.py, updates.py, bitlocker.py,
│       │   │                         # usb.py, eventlog.py, analyzer.py
│       │   ├── documents/            # readers/, writers/, ocr.py, summarize.py
│       │   ├── memory/               # store.py, embeddings.py, retrieval.py,
│       │   │                         # preferences.py, redaction.py
│       │   ├── db/                   # engine.py, migrations/, repositories/
│       │   ├── plugins/              # host.py, manifest.py, sandbox.py
│       │   ├── diagnostics/          # collectors/, analyzer.py, report.py
│       │   └── util/                 # logging, errors, cancellation, subprocess
│       ├── tests/                    # unit/ integration/ fixtures/
│       └── pyproject.toml
│
├── packages/
│   └── shared/                       # schema source of truth → TS + Python types
│
├── installer/                        # NSIS hooks, model fetcher, LICENSE
├── scripts/                          # dev.ps1, build.ps1, gen-types.ts
├── docs/                             # ARCHITECTURE.md, SECURITY.md, PHASES.md
├── .github/workflows/                # CI: typecheck, tests, Windows build
└── README.md
```

**Why a monorepo with `apps/` + `services/`:** the frontend and the core have
different toolchains, test runners, and release cadences, but one version number.
`packages/shared` holds the JSON Schemas that generate *both* the TypeScript types
and the Python Pydantic models, so a tool's I/O contract can never drift between
the two sides.

**Why `windows/posix_stub/`:** the core must remain importable and unit-testable on
non-Windows CI. Every Windows adapter has a stub that raises a typed
`PlatformUnsupported` error, so the ~70% of the codebase that is platform-neutral
(agents, governance, memory, documents, AI providers) gets tested on every push.

---

## 5. Required dependencies

### Frontend (`apps/desktop/package.json`)
```
react  react-dom  zustand                     # UI + state
@tauri-apps/api  @tauri-apps/plugin-*         # shell bridge
typescript  vite  @vitejs/plugin-react        # build
vitest  @testing-library/react  jsdom         # test
```

### Tauri core (Rust, `src-tauri/Cargo.toml`)
```
tauri 2                      serde / serde_json
tauri-plugin-shell           # sidecar spawn
tauri-plugin-global-shortcut # Ctrl+Space
tauri-plugin-single-instance # one Jarvis only
tauri-plugin-autostart       # optional launch at login
tauri-plugin-notification
sysinfo                      # REAL cpu/ram/disk — no mocks
```

### Python core (`services/jarvis/pyproject.toml`)

Split into extras so a minimal install stays small and offline-capable:

| Extra | Packages | Purpose |
|---|---|---|
| *(base)* | `fastapi` `uvicorn[standard]` `pydantic` `pydantic-settings` `httpx` `structlog` `keyring` `tomlkit` `psutil` | core, always installed |
| `windows` | `pywin32` `comtypes` `uiautomation` `wmi` `pythoncom` | Win32 + UIA3 + WMI |
| `voice` | `faster-whisper` `sounddevice` `webrtcvad` `piper-tts` `openwakeword` `onnxruntime` | local STT/TTS/VAD/wake |
| `local-llm` | `llama-cpp-python` | fully offline LLM |
| `documents` | `pypdf` `python-docx` `openpyxl` `python-pptx` `pillow` `pytesseract` `markdownify` | file intelligence |
| `memory` | `sentence-transformers` *or* `onnxruntime` + MiniLM ONNX | embeddings |
| `browser` | `playwright` | browser automation |
| `dev` | `pytest` `pytest-asyncio` `ruff` `mypy` `coverage` | tooling |

**External, non-pip prerequisites** (these must be stated plainly — they are the
honest limits of a one-click install):

| Requirement | Needed for | Notes |
|---|---|---|
| **WebView2 Runtime** | the UI | Preinstalled on Win11; NSIS bootstrapper installs it on Win10 |
| **VC++ 2015–2022 Redistributable** | onnxruntime, llama.cpp | bundled in installer |
| **Tesseract OCR** | `OCRTool` | separate ~60 MB install; OCR is *disabled with a clear message* until present |
| **Whisper + Piper + wake-word models** | voice | ~200 MB–1.5 GB, downloaded on first run with consent, never bundled |
| **Chromium** | `BrowserTool` | `playwright install chromium`, on demand |
| **Admin elevation** | firewall changes, some Defender reads, service control | requested per-action via UAC, never held persistently |
| **Avoid "unknown publisher" SmartScreen** | installer trust | needs a real code-signing certificate (EV or OV + reputation) |

### Dependency discipline
No dependency enters the tree unless it (a) replaces >200 lines we'd otherwise
write, or (b) wraps a C/COM API we should not hand-roll. Everything in the lists
above clears that bar. Explicitly **not** used: LangChain / LlamaIndex (the agent
loop here needs tight governance coupling that a generic framework fights),
heavyweight UI kits, ORMs (hand-written SQL against SQLite is clearer and faster
for this schema).

---

## 6. Security model

The threat model is explicit, because "an AI that can run commands on my PC" is
only safe if you say what you're defending against.

### Assets
Your files; your credentials; OS integrity; your network; your privacy (what leaves
the machine).

### Adversaries considered

| # | Adversary | Attack | Mitigation |
|---|---|---|---|
| **T1** | A malicious web page / PDF / filename | **Prompt injection** → model proposes `delete C:\Users\me` or exfiltration | Model output is *untrusted data*. It can only emit proposals; Policy Engine + consent gate every one. Content from files/web is wrapped in delimited, clearly-labelled untrusted blocks and the system prompt states that instructions inside them are never obeyed. Egress tools (email/upload/browser-POST) are **always** confirm-required, regardless of how the request arrived. |
| **T2** | The LLM itself | Hallucinated destructive path, wrong file set, silent failure | Dry-run + Observer pre/post diff; destructive ops default to **Recycle Bin**, not unlink; counts shown in the consent prompt ("37 files"); success text derived from observed diff. |
| **T3** | Another local process / user | Hitting the loopback API to drive the assistant | Bind `127.0.0.1` only; ephemeral port; per-launch random bearer token passed out-of-band via stdout to the Tauri parent; `Origin`/`Host` validation; token never written to disk. |
| **T4** | A malicious plugin | Reading secrets, escalating scope | Plugins declare scopes in a manifest, run in a separate process with no ambient credentials, get a capability-restricted tool proxy, and are **off by default**. They cannot register `risk: critical` tools. |
| **T5** | Local disk attacker / theft | Reading API keys, conversation history | Keys only in Windows Credential Manager (DPAPI, per-user). Never in `config.toml`, never in logs, never in env dumps. DB holds no secrets. BitLocker status surfaced in Security Center. |
| **T6** | Cloud provider | Receiving more data than intended | Local-first default. Egress preview + per-provider redaction. Privacy Dashboard shows exactly what categories go out. Cloud is one switch away from off. |
| **T7** | Us, accidentally | Logging a password; path traversal via `..` | Structured logging with a redaction filter (secrets, tokens, emails, long base64). All paths canonicalised through a **path jail** before use. |

### Hard prohibitions (architectural, not policy text)

The following are **not implemented and will not be**. They are absent by design,
not merely disabled:

- No keylogging. Global input hooks are not installed; the only keyboard listener
  is a single registered OS hotkey, which receives one keypress, not a stream.
- No credential harvesting. No browser-password-store reading, no LSASS access,
  no DPAPI blob mining, no Windows Vault enumeration.
- No covert surveillance. Screenshots/microphone/camera are **user-initiated only**
  and raise a visible, non-dismissable tray + UI indicator while active.
- No hidden persistence. Autostart uses only the documented, user-visible
  Run-key/Startup mechanism and is toggleable from Settings.
- No AV/EDR evasion, no unsigned-driver loading, no AMSI/ETW tampering.
- No access to other machines or accounts. Network tools are read-only
  diagnostics on *this* host: no scanning, no lateral movement, no remote exec.
- Windows security prompts (UAC, SmartScreen, Defender) are **never** suppressed
  or auto-clicked. If a UAC prompt appears, the user answers it.

### Defence in depth
1. Tauri capability manifest (deny-by-default at the shell).
2. Loopback + token + Origin at the transport.
3. Policy Engine: risk tier × enabled-tool × scope grant × path jail × rate limit.
4. Consent broker for anything above low risk.
5. Path jail: every filesystem path canonicalised (`realpath`, symlink/junction
   resolved, 8.3-name expanded) and tested against an allowlist. `%WINDIR%`,
   `%PROGRAMFILES%`, `System32`, boot files, and the Jarvis install dir are on a
   deny-list that no grant can override.
6. Constrained subprocess execution: argument **lists** only — never a shell
   string, never `shell=True`. PowerShell runs `-NoProfile -NonInteractive` with
   an operator allowlist; `Invoke-Expression`, `DownloadString`, encoded commands,
   and AV/firewall-disabling cmdlets are rejected before spawn.
7. Append-only, hash-chained audit log — tamper-evident.
8. Emergency stop: a process-wide cancellation token every tool must poll, plus
   termination of child processes Jarvis spawned.

---

## 7. Permission model

Three orthogonal axes. An action runs only if **all three** permit it.

### Axis 1 — Risk tier (a property of the tool/action)

| Tier | Behaviour | Examples |
|---|---|---|
| **1 · SAFE** | auto-run, logged | read system info, list processes, search files, read a file in an allowed folder, screenshot (with indicator), get clipboard, web search |
| **2 · LOW** | auto-run (configurable), logged, undoable | launch app, create folder, write a *new* file in an allowed folder, copy file, set clipboard, notify |
| **3 · MEDIUM** | **confirm by default**, batched preview | move/rename many files, close an app, Recycle-Bin delete, run an allowlisted command, browser navigation + form fill, create a document in a new place |
| **4 · HIGH** | **always confirm**, explicit diff/count, no "remember" | permanent delete, overwrite existing file, registry write, service start/stop, scheduled-task creation, install software, arbitrary PowerShell, file upload, send email/message |
| **5 · CRITICAL** | **confirm + typed phrase**, re-auth, never remembered, never plugin-exposed | disable Defender/firewall, format/partition a disk, change a password, modify system files, bulk delete >N files, UAC-elevated arbitrary execution |

Tiers may be *raised* by the user but never silently lowered. Tier 5 cannot be
auto-approved by any setting, any "always allow", or any plugin.

### Axis 2 — Scopes (capability grants, like OAuth)

```text
fs.read:<path>      fs.write:<path>     fs.delete:<path>
app.launch          app.control         window.manage
process.read        process.kill
system.info         system.settings     system.admin
net.read            net.configure
shell.run           shell.powershell
browser.use         browser.download    browser.upload
screen.capture      screen.ocr
mic.listen          speaker.speak
security.read       security.modify
cloud.llm           cloud.send
memory.read         memory.write
```

Scopes are granted in Settings/Privacy Dashboard, persisted, revocable, and
individually visible. Path scopes are hierarchical: granting `fs.write:C:\Users\me\Desktop`
covers children but not siblings. Default grant set on install is deliberately narrow:
`fs.read` + `fs.write` on Desktop/Documents/Downloads/Pictures, `app.launch`,
`system.info`, `process.read`, `security.read`. Everything else is off.

### Axis 3 — Mode (a global posture)

| Mode | Effect |
|---|---|
| **Paused** | nothing executes; chat and explanation still work |
| **Guarded** *(default)* | tier 1–2 auto, 3+ confirm |
| **Assisted** | tier 1–3 auto within granted scopes, 4+ confirm |
| **Developer** | as Assisted + shell/git/package-manager allowlists in project dirs; every command echoed to the Activity Log before it runs |

`Read-only` is available as a hard override that forces every mutating tool to
return a "would have done X" dry-run result.

### Consent UX contract
Every prompt must answer: **what** (verb + exact object count), **where** (full
paths, truncated with expand), **why** (the originating request + plan step),
**reversibility** (Recycle Bin? undoable? permanent?), and **blast radius**
(biggest thing that changes). Buttons: `Confirm` · `Cancel` · `Review details`.
Where it's meaningful, `Confirm` carries a scoped-remember option
(`this session` / `this folder` / `always`) — never offered for tier 4–5.

---

## 8. AI agent architecture

Seven cooperating roles, not one chat loop. Each is a separate prompt + schema +
budget, so each can be independently tested, swapped, or run on a different model.

```text
         ┌──────────┐
 input ─▶│  ROUTER  │  cheap/local model or rules. Classifies:
         └────┬─────┘  chat | single-tool | multi-step | diagnostic | security
              │        Short-circuits trivia and chit-chat (no agent loop at all).
   ┌──────────┼───────────────┐
   │          │               │
   ▼          ▼               ▼
 CHAT     SINGLE-TOOL     ┌─────────┐
(answer)  (one proposal   │ PLANNER │  strong model. Emits a typed DAG:
              + gate)     └────┬────┘  [{id, tool, args, depends_on, rationale,
                               │         expected_observation, reversible}]
                               ▼
                         ┌─────────┐
                         │ CRITIC  │  reviews plan BEFORE execution:
                         └────┬────┘  destructive-without-need? missing dry-run?
                               │      over-broad glob? cheaper path? → revise
                               ▼
                         ┌──────────┐
                         │ POLICY + │  ← governance plane, not the model
                         │ CONSENT  │
                         └────┬─────┘
                               ▼
                         ┌──────────┐      ┌──────────┐
                         │ EXECUTOR │◀────▶│ OBSERVER │  pre/post state capture
                         └────┬─────┘      └──────────┘  + claim verification
                               │  on error: classify → repair (≤2) → or ask user
                               ▼
                         ┌───────────┐
                         │ REFLECTOR │  outcome → memory (prefs, recipes, failures)
                         └───────────┘
```

### Why a planner *and* a critic
A single model pass that both plans and executes is where agents cause damage: the
model that wants to finish the task is the wrong one to ask "is this too
destructive?". The Critic runs with a different prompt and a different objective
(minimise blast radius), and it has veto power to force a revision. It is cheap —
one call against a plan, not per step.

### Observer: the anti-hallucination layer
Before a step: snapshot exactly the state the step claims to change (file list +
sizes + hashes, window handles, process set, registry value). After: diff it.
If the diff doesn't match `expected_observation`, the step is **failed**, not
succeeded — even if the tool returned 0. This is what satisfies "never silently
pretend the task succeeded".

### Memory — four tiers, all user-controllable

| Tier | Lifetime | Contents | Retrieval |
|---|---|---|---|
| **Working** | one turn | current plan, step results, observations | in-context |
| **Episodic** | per session, archived | conversation turns, actions taken | recency + vector |
| **Semantic** | durable | learned preferences, aliases ("my portfolio" → path), folder habits, app choices, custom commands | vector + exact key |
| **Procedural** | durable | successful plans promoted to reusable recipes, and failures with their cause | tool/intent signature match |

Promotion to Semantic/Procedural requires either an explicit user statement
("always use VS Code") or **three** consistent observations — one-off behaviour
never becomes a durable belief. Every memory row stores its provenance (which turn
created it) and is listed, searchable, editable, and deletable in the Privacy
Dashboard. A redaction pass strips secrets before anything is written.

### Model-tier routing (cost + latency + privacy)
| Job | Model class |
|---|---|
| Router, redaction, short summaries | small local (3–4B) or rules |
| Planner, Critic, diagnostics reasoning | strong (local 14–32B, or cloud) |
| Document summarisation | mid, chunked map-reduce |
| Embeddings | local MiniLM/BGE ONNX — **never** cloud |

Budgets are enforced per turn (tokens, wall-clock, tool-call count, repair
attempts) so a confused loop terminates instead of grinding.

---

## 9. Voice architecture

```text
 Microphone (16 kHz mono)
        │
        ▼
 ┌─────────────────┐   Ring buffer, ~1.5 s pre-roll, so the first
 │  AUDIO CAPTURE  │   word is never clipped when the wake word fires.
 └────────┬────────┘
          ├───────────────────────────────┐
          ▼                               ▼
 ┌─────────────────┐            ┌──────────────────┐
 │  WAKE WORD      │            │  PUSH-TO-TALK    │
 │  openWakeWord   │            │  Ctrl+Space hold │
 │ hey_jarvis ONNX │            │  (always avail.) │
 │  ~15 MB, local  │            └────────┬─────────┘
 └────────┬────────┘                     │
          └──────────────┬───────────────┘
                         ▼
               ┌──────────────────┐   webrtcvad + energy/hangover.
               │       VAD        │   Ends the utterance on ~700 ms silence,
               │  (speech bounds) │   hard cap 30 s.
               └────────┬─────────┘
                        ▼
               ┌──────────────────┐   faster-whisper (CTranslate2).
               │       STT        │   base.en int8 default (~75 MB, real-time
               │  LOCAL default   │   on CPU); small/medium optional.
               └────────┬─────────┘   Cloud STT = opt-in only.
                        ▼
               ┌──────────────────┐
               │   JARVIS CORE      │  (router → planner → … )
               └────────┬─────────┘
                        ▼
               ┌──────────────────┐   Piper ONNX, streamed by sentence so
               │       TTS        │   speech starts before the full answer
               │  Piper (local)   │   exists. SAPI5 fallback if models absent.
               └────────┬─────────┘
                        ▼
                    Speaker
                        │
                 ┌──────┴───────┐
                 │  BARGE-IN    │  Mic stays open while speaking. Wake word or
                 │ (interrupt)  │  sustained speech → cancel TTS mid-sentence,
                 └──────────────┘  flush queue, start listening. Also Esc / click.
```

### Design commitments
- **Local by default, end to end.** No audio leaves the machine unless the user
  explicitly enables a cloud STT provider. The mic is never "always streaming to
  a server" — wake-word detection is openWakeWord's pretrained `hey_jarvis`
  model, a local ~15 MB ONNX graph running on a ring buffer.
- **Barge-in is mandatory, not a nice-to-have.** An assistant you can't interrupt
  mid-sentence is unusable. Implemented by keeping capture live during playback
  with AEC-lite (ignore input correlated with current output) to avoid self-trigger.
- **Visible mic state, always.** Tray icon + UI indicator distinguish
  *off / listening-for-wake / recording / thinking / speaking*. There is no state
  in which the mic is live without an on-screen indicator.
- **Push-to-talk always works** even with the wake word disabled, because
  wake-word accuracy in noisy rooms is genuinely imperfect.
- **Degradation is explicit.** If voice models aren't downloaded, the voice button
  says so and offers the download — it does not silently fall back or pretend.

Barge-in, AEC, and wake-word false-accept tuning are the three parts of this design
most likely to need iteration against real hardware; they are Phase 6 work.

---

## 10. Computer-control architecture

A strict **four-layer fallback ladder**. Always descend from the most reliable
technique to the least; never start at the bottom.

```text
 ┌────────────────────────────────────────────────────────────────────────┐
 │ L1 · NATIVE API / PROTOCOL            reliability ★★★★★  preferred     │
 │ Win32 · Shell COM · WinRT · WMI/CIM · psutil · registry · Playwright   │
 │ CDP                                                                    │
 │ "Open Chrome"      → ShellExecuteEx / CreateProcess                    │
 │ "Create a folder"  → CreateDirectoryW                                  │
 │ "Delete to bin"    → IFileOperation (real Recycle Bin, real undo)      │
 │ "Minimise window"  → ShowWindow / SetForegroundWindow                  │
 │ "Which apps run?"  → psutil + EnumWindows                              │
 │ "CPU/RAM/disk"     → psutil / PDH counters                             │
 │ "Wi-Fi off"        → WinRT Radio API (no admin) / netsh (admin)        │
 │ "Defender status"  → MSFT_MpComputerStatus via CIM                     │
 │ "Open a website"   → Playwright CDP (deterministic, not pixels)        │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 │ no native path?
 ┌───────────────────────────────▼────────────────────────────────────────┐
 │ L2 · UI AUTOMATION (UIA3)             reliability ★★★★   good          │
 │ IUIAutomation via comtypes/uiautomation. Elements by AutomationId /    │
 │ Name / ControlType — semantic, resolution-independent, DPI-safe.       │
 │ Click = Invoke/Toggle pattern. Type = ValuePattern. Read = TextPattern.│
 │ Used for: legacy Win32/WinForms/WPF dialogs, Office dialogs,           │
 │ Settings pages without an API.                                         │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 │ element not exposed?
 ┌───────────────────────────────▼────────────────────────────────────────┐
 │ L3 · KEYBOARD-FIRST SYNTHESIS         reliability ★★★    acceptable    │
 │ SendInput of real shortcuts (Win+E, Ctrl+S, Alt+F4, Tab-order nav).    │
 │ Deterministic because it rides the app's own accelerators, not pixels. │
 └───────────────────────────────┬────────────────────────────────────────┘
                                 │ nothing else works?
 ┌───────────────────────────────▼────────────────────────────────────────┐
 │ L4 · VISION + COORDINATES             reliability ★★     last resort   │
 │ Screenshot → OCR/template match → SendInput click at point.            │
 │ ALWAYS: verify with a post-click UIA/pixel check, cap retries, and     │
 │ surface "I used screen automation here, it may be fragile."            │
 │ Never used when L1–L3 can do the job.                                  │
 └────────────────────────────────────────────────────────────────────────┘
```

Each tool declares the highest layer it uses. The Activity Log shows the layer, so
a flaky action is immediately attributable. This directly answers "use native
Windows APIs rather than unreliable screen-coordinate automation": L4 exists, but
it is quarantined, labelled, and verified.

### Subprocess discipline
One chokepoint (`util/subprocess.py`). Rules enforced in code:
argument lists only; `shell=False` always; explicit `cwd`; scrubbed environment;
timeout mandatory; output size-capped; child PIDs registered with the emergency-stop
registry so STOP can kill them. PowerShell additionally: `-NoProfile
-NonInteractive -ExecutionPolicy Bypass -Command` with a parsed-and-allowlisted
command, rejecting `Invoke-Expression`, `iex`, `-EncodedCommand`,
`DownloadString/DownloadFile`, `Set-MpPreference -Disable*`,
`Set-ExecutionPolicy`, and firewall-disable patterns.

---

## 11. Local-vs-cloud AI architecture

```text
                       ┌──────────────────────────┐
  agent request  ────▶ │   PROVIDER GATEWAY       │
                       │  route by: job class,    │
                       │  privacy class, budget,  │
                       │  availability            │
                       └────┬────────────┬────────┘
                            │            │
              ┌─────────────▼──┐   ┌─────▼──────────────────┐
              │ LOCAL (default)│   │ CLOUD (opt-in per job) │
              │ llama.cpp GGUF │   │ OpenAI-compatible      │
              │ or Ollama      │   │ Anthropic              │
              │ + MiniLM embed │   │ custom base_url        │
              └────────────────┘   └────────────────────────┘
                            │            │
                            └─────┬──────┘
                                  ▼
                        ┌────────────────────┐
                        │  EGRESS FIREWALL   │ runs BEFORE any network call:
                        │  · privacy class   │  redact secrets/paths/PII
                        │  · redaction       │  per rules
                        │  · size cap        │  show preview if asked
                        │  · audit the fact  │  log that data left + what class
                        └────────────────────┘
```

### Provider interface
A single ABC — `complete()`, `stream()`, `embed()`, `count_tokens()`,
`capabilities()` — with four implementations (llama.cpp in-process, Ollama HTTP,
OpenAI-compatible, Anthropic). Anything OpenAI-shaped (LM Studio, vLLM, Groq,
OpenRouter, Azure, llama.cpp's own server) works through the one adapter with a
custom `base_url`. Adding a provider is one file.

### Privacy classes (the routing key that matters)
| Class | Contents | Cloud-eligible? |
|---|---|---|
| `PUBLIC` | generic knowledge questions | yes |
| `METADATA` | file names, counts, extensions, process names | yes, if cloud enabled |
| `CONTENT` | document text, screenshots, clipboard | **only** with explicit per-category consent |
| `SENSITIVE` | credentials, security findings, audit log, memory store | **never** — hard-coded, not a setting |

A cloud call carrying `SENSITIVE` is a programming error and raises, rather than
being filtered. That's deliberate: a filter can be mis-tuned, a type error cannot.

### Settings page (§8 of the request)
AI Provider ▸ `Local model` · `Cloud API` · `Custom OpenAI-compatible endpoint`,
with: model picker (local = GGUF file browser + auto-detected Ollama tags),
per-job-class overrides, context size, temperature, token/£ budget caps, and a
connectivity test button. API keys are entered in a masked field, written straight
to **Windows Credential Manager**, and never read back into the UI — the field
shows "configured", not the value. `config.toml` contains no secrets, only a
reference like `credential = "jarvis/openai"`. Nothing is hard-coded.

### Offline guarantee
With no network: UI, all computer control, filesystem/process/window/system tools,
diagnostics, security checks, local STT/TTS/wake word, memory, and a local LLM all
function. Only web search, browser navigation to the internet, and cloud providers
degrade — and they report *why*, rather than hanging.

---

## 12. Database design

SQLite, WAL mode, `%LOCALAPPDATA%\Jarvis\jarvis.db`. Versioned, forward-only
migrations. No ORM.

```text
sessions(id PK, started_at, ended_at, title, mode, voice_used)

turns(id PK, session_id FK→sessions, role, content, privacy_class,
      provider, model, tokens_in, tokens_out, latency_ms, created_at)

plans(id PK, turn_id FK→turns, goal, status, dag_json, critic_notes,
      created_at, completed_at)

steps(id PK, plan_id FK→plans, idx, tool_name, args_json, risk,
      status, depends_on_json,
      consent_id FK→consents NULL, observation_pre_json,
      observation_post_json, result_json, error_json,
      duration_ms, control_layer)        -- L1..L4, for flakiness triage

audit(id PK, ts, actor, tool_name, args_digest, risk, decision,
      scopes_used_json, outcome, error_code, step_id FK→steps NULL,
      prev_hash, hash)                   -- append-only, hash-chained

consents(id PK, ts, action_digest, prompt_text, decision,
         remembered_scope, expires_at)   -- NULL scope = one-shot

scope_grants(id PK, scope, target, granted_at, expires_at, source)

memory(id PK, tier, key, value_json, confidence, observation_count,
       source_turn_id FK→turns, created_at, updated_at, pinned)
memory_vec(memory_id FK→memory, embedding BLOB)        -- sqlite-vec

preferences(key PK, value_json, updated_at, set_by)    -- 'user' | 'learned'

security_findings(id PK, detected_at, category, severity, classification,
                  title, evidence_json, explanation, status,
                  acknowledged_at, fingerprint UNIQUE)
-- classification ∈ {confirmed_event, suspicious_behavior, potential_risk,
--                   normal_activity}  ← §6 of the request, enforced at the schema
security_baseline(fingerprint PK, category, first_seen, last_seen, trusted)

plugins(id PK, name, version, enabled, manifest_json, scopes_json, installed_at)

tool_stats(tool_name PK, calls, failures, avg_ms, last_called)
documents(id PK, path, hash, kind, size, mtime, summary, indexed_at)
document_vec(document_id FK→documents, chunk_idx, text, embedding BLOB)
reminders(id PK, due_at, text, status, created_at, notified_at)
```

Design notes worth stating:
- **`audit` is hash-chained** (`hash = H(prev_hash ‖ row)`), so tampering is
  detectable — an audit log you can quietly edit isn't an audit log.
- **`audit.args_digest`, not raw args**: the log proves *what happened* without
  becoming a secondary copy of your file contents.
- **`security_findings.classification`** is a constrained column, so the "don't
  call it a virus because it's unfamiliar" rule is enforced by the schema, not by
  prompt wording. `security_baseline` is what lets Jarvis say "this is new" instead
  of "this is malicious".
- **`steps.control_layer`** records which rung of the §10 ladder was used, making
  automation flakiness measurable rather than anecdotal.
- Secrets appear in **no** table. Credentials live only in Credential Manager.

---

## 13. Development roadmap

Phase gate: a phase is done only when it runs, its tests pass, and errors found
are fixed. No phase starts before the previous one is functional.

| Phase | Deliverable | Gate |
|---|---|---|
| **1** | Desktop UI: shell, tray, global hotkey, chat surface, real system status, quick actions, activity log, emergency stop, consent dialog, 5 views | `npm run build` + `vitest` green; app launches on Windows |
| **2** | Python core: FastAPI sidecar, loopback auth, provider gateway, router/planner/executor skeleton, SQLite + migrations, structured logging, streaming WS to UI | round-trip chat UI ↔ core; provider tests |
| **3** | FileSystemTool + path jail + Policy Engine + consent broker + audit chain + DocumentTool (read) | path-jail traversal tests; consent gating tests |
| **4** | ApplicationTool, WindowTool, ProcessTool (L1 Win32) | launch/focus/close round-trip on Windows |
| **5** | SystemInfoTool, NetworkTool, PowerShellTool (allowlisted), ClipboardTool, NotificationTool, ScreenshotTool, diagnostics v1 ("why is my PC slow") | diagnostics report with evidence |
| **6** | Voice: VAD → STT → core → TTS, wake word, push-to-talk, barge-in, mic indicator | end-to-end voice command on real hardware |
| **7** | BrowserTool (Playwright), WebSearchTool, page extraction, sensitive-action gating | navigate + extract + gated form fill |
| **8** | Memory: 4 tiers, embeddings, retrieval, preference learning, Privacy Dashboard CRUD | preference recall across sessions |
| **9** | Security Center: Defender/firewall/startup/net/USB/updates/BitLocker/event log + baseline + classified findings + notifications | findings with correct classification + explanation |
| **10** | Full permission matrix, modes, scope grants UI, typed-phrase tier-5 confirm, read-only mode, rate limits | red-team the gate: no tool reachable un-gated |
| **11** | Plugin host: manifest, process isolation, scoped tool proxy, enable/disable UI; reference plugin | plugin cannot exceed declared scopes |
| **12** | Packaging: PyInstaller sidecar, Tauri NSIS, shortcuts, uninstaller, autostart, model fetcher, signing, update channel | `AI-Assistant-Setup.exe` installs & runs on clean Win10/11 VM |

Cross-cutting, every phase: tests for new logic, audit coverage for new tools,
docs updated, `ruff`/`mypy`/`tsc` clean.

---

## 14. MVP features (Phases 1–5 + 10)

The smallest build that is genuinely useful daily:
- Chat UI + tray + `Ctrl+Space` + emergency stop.
- Natural-language multi-step planning with visible plans.
- File ops: create/rename/move/copy/Recycle-Bin-delete/search, in allowed folders.
- Read PDF/DOCX/XLSX/TXT/CSV and summarise.
- Launch/focus/close apps; list and inspect windows.
- Process list, CPU/RAM/disk/network, "why is my PC slow" with evidence.
- Screenshots. Clipboard. Notifications.
- Permissions: risk tiers, scopes, modes, consent prompts, audit log.
- Local LLM **or** cloud provider, keys in Credential Manager.
- Activity log + privacy dashboard.

Explicitly *not* in MVP: voice, browser automation, security monitoring, memory
learning, plugins. They're Phases 6–11 — valuable, but the MVP must be trustworthy
before it's broad.

---

## 15. Advanced features (post-v1)

Tier A (v1.1): custom named commands/macros; scheduled tasks; multi-monitor-aware
window layouts; document comparison + conversion; duplicate finder with
content hashing; folder-organisation rules you can edit; Gmail/Calendar/GitHub
plugins; richer developer mode (scaffold, run, debug loops).

Tier B (v1.2+): proactive suggestions from observed habits ("you open these three
apps every morning — make it a macro?"); vision-grounded GUI agent for apps with
no API; local fine-tune/LoRA on your own command history; encrypted multi-device
memory sync; natural-language SQL over your own document index; Office COM
automation; smart-home and Discord/Telegram plugins; an on-device anomaly model
for the Security Center (baseline-learned, still classification-constrained).

---

## 16. Potential technical limitations

Stated plainly, because the honest version of this project has limits:

1. **Prompt injection is mitigated, not solved.** Any system where a model reads
   untrusted content and proposes actions carries residual risk. The gate is the
   real defence; a user who clicks Confirm on everything defeats it. This is why
   tier 4–5 never offer "remember".
2. **UIA coverage is uneven.** Electron apps, some games, custom-rendered UIs
   (Flutter, Qt without the bridge), and canvas apps expose little or nothing.
   Those fall to L3/L4 and will be less reliable; the layer is labelled so you know.
3. **Elevation can't be inherited.** A non-elevated Jarvis cannot drive an elevated
   window (UIPI). Admin actions need a per-action elevated helper + UAC prompt.
   Running Jarvis elevated permanently would be worse, so we don't.
4. **Local LLM quality/latency tradeoff is real.** Reliable tool-calling from a
   7–8B model is achievable but noticeably worse at multi-step planning than a
   frontier model. 14B+ with ≥8 GB VRAM is where local planning gets good. The
   gateway exists precisely so you can put the Planner on cloud and keep
   everything else local.
5. **Wake-word accuracy varies** with mic quality, room noise, and accent.
   Push-to-talk is the dependable path; the wake word is a convenience.
6. **OCR is approximate.** Tesseract on screenshots of non-standard fonts/low
   contrast will misread. OCR output is never used for a destructive decision.
7. **Antivirus will flag us.** An unsigned app that spawns PowerShell, enumerates
   processes, and synthesises input looks exactly like malware to heuristics. Needs
   code signing plus, realistically, user-added exclusions. Without a signing cert,
   SmartScreen will warn on install.
8. **Defender/firewall *changes* need admin**, and some enterprise/Intune-managed
   machines block them entirely via policy. Jarvis reports the policy block rather
   than failing opaquely.
9. **Disk health (SMART) access is inconsistent** across controllers, especially
   NVMe behind RAID/VMD. Reported as unavailable when it is, not guessed.
10. **Browser automation uses its own Chromium profile.** It will not share your
    logged-in sessions by default — deliberately, so a web agent cannot act as
    you in your real accounts without an explicit, separate decision.
11. **Event Log reads** for security events (e.g. failed logons, 4625) need admin
    and the relevant audit policy enabled; often unavailable on Home editions.
12. **Cross-platform build caveat:** this repository is authored in a Linux
    container. Platform-neutral code is fully tested here; Win32/UIA/WMI paths,
    the Rust/WebView2 shell build, and the NSIS installer **can only be compiled
    and verified on Windows**. Phase gates for those parts are explicitly
    "verified on Windows by the user", and nothing untested is described as working.

---

## 17. Windows-specific requirements

| Area | Requirement |
|---|---|
| OS | Windows 10 21H2 (19044) or later; Windows 11 recommended. x64 primary. |
| Runtime | WebView2 Evergreen Runtime; VC++ 2015–2022 redistributable. |
| Python | 3.11/3.12 — frozen into the sidecar, not required on the user's machine. |
| Privileges | Installs and runs **per-user, non-elevated**. Elevation requested per-action for: firewall config, Defender preference changes, service control, some Event Log channels, driver queries. |
| APIs used | Win32 (`user32`, `kernel32`, `shell32`, `advapi32`), Shell COM (`IFileOperation`, `IShellLink`), UI Automation 3, WMI/CIM (`root\cimv2`, `root\Microsoft\Windows\Defender`, `root\StandardCimv2`), PDH counters, WinRT (`Windows.Devices.Radios`, `Windows.UI.Notifications`), Credential Manager (DPAPI). |
| Long paths | `LongPathsEnabled` honoured; `\\?\` prefix used for >260-char paths. |
| DPI | Per-monitor-v2 DPI awareness declared in the manifest (UIA is DPI-safe; L4 coordinate work is scaled explicitly). |
| Defender | Likely needs an exclusion for the install dir, and the installer should say so rather than hiding it. |
| Store/MSIX | Not targeted for v1 — MSIX's container restrictions conflict with system-wide automation. NSIS per-user install instead. |
| Data locations | `%LOCALAPPDATA%\Jarvis\` (db, logs, models, cache); `%APPDATA%\Jarvis\config.toml`; Credential Manager for secrets; `%LOCALAPPDATA%\Programs\Jarvis\` for the program. |

---

## 18. Installation & packaging strategy

```text
 services/jarvis  ──PyInstaller(onedir)──▶  jarvis-core.exe + _internal/
                                                   │
 apps/desktop   ──vite build──▶ dist/              │ bundled as Tauri externalBin
                                   └──tauri build──┤
                                                   ▼
                                      AI-Assistant-Setup.exe   (NSIS, per-user)
```

**Why PyInstaller `onedir` + Tauri sidecar:** `onedir` starts much faster than
`onefile` (no per-launch temp extraction) and keeps the AV surface calmer. The
sidecar is a normal child process, supervised by Rust, with health checks and
auto-restart.

**Installer does:**
per-user install to `%LOCALAPPDATA%\Programs\Jarvis`; Desktop + Start Menu
shortcuts; optional "Start with Windows"; creates `%LOCALAPPDATA%\Jarvis\`;
writes a default `config.toml` with **no secrets**; bootstraps WebView2 + VC++ if
missing; registers a clean uninstaller (with a "keep or delete my data" choice);
and does **not** bundle AI models.

**Models on first run, with consent:** a setup wizard shows each download, its
size, and its purpose, then fetches whisper/piper/wake-word/GGUF to
`%LOCALAPPDATA%\Jarvis\models\`. This keeps the installer ~60–80 MB instead of
multi-gigabyte, and keeps the choice to download anything in the user's hands.

**Signing & updates:** code-sign the sidecar, the app, and the installer (needed
to avoid SmartScreen; an EV cert gets reputation immediately). Updates via Tauri's
updater against a signed manifest, user-approved, with the option to disable update
checks entirely for a fully offline install.

**Uninstall** removes the program, shortcuts, autostart entry, and Credential
Manager entries, and asks before deleting `%LOCALAPPDATA%\Jarvis\` (your data,
your call).
