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
# Everything the installer ships. Drop `voice` for a faster install if you do
# not need the wake word; drop `documents` if you will not read PDFs or Word
# files. Both degrade a feature rather than breaking anything, and Settings
# says which are missing.
.venv/Scripts/pip install -e ".[dev,secrets,documents,voice]"   # macOS/Linux: .venv/bin/pip

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

For real answers, use **Settings → AI models**. Pick a service from the list,
paste a key if it needs one, press Add, and switch to it — no restart, and no
editing files. The list is served by the core, so it is the same set of services
the adapters actually support rather than a second list that drifts.

Cloud services need one more deliberate step: **Allow cloud models**, a separate
switch. A stored key is not consent, so a key kept for later does not start
sending conversations off the machine on its own. Allowing document and screen
*content* to reach a cloud model is a third switch again.

Editing `config.toml` in the Jarvis data folder (`%LOCALAPPDATA%\Jarvis` on
Windows) still works and is equivalent — the panel writes the same file, keeping
its comments:

```toml
[ai]
default = "ollama"

[ai.providers.ollama]
kind = "ollama"
model = "qwen2.5:14b-instruct"
```

API keys are never in that file. They go to the Windows Credential Manager under
the name in `credential`, and pasting a key into that field instead of a name is
rejected with an explanation. The panel never reads a key back — it reports
"Ready", not a value.

### Connecting Claude, ChatGPT, Gemini and the rest

Five adapters cover them, and **Settings → AI models** offers each one by name.
The table below is the same set for anyone configuring it by hand: `config.toml`
ships every block commented out — uncomment one, set `default` to its name, and
set `allow_cloud = true`.

| Service | `kind` | Notes |
|---|---|---|
| ChatGPT, Claude or Gemini with **no key** | `jarvis_cloud` | Relayed through the Jarvis web app, which holds the key — and sees what you send |
| Claude | `anthropic` | `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` |
| ChatGPT | `openai_compat` | `base_url = "https://api.openai.com/v1"` |
| Gemini | `google` | Its own adapter — Gemini's wire format is neither of the others |
| OpenRouter | `openai_compat` | One key, hundreds of models from many vendors |
| Groq, Together, DeepSeek, Mistral, Azure | `openai_compat` | Same adapter, different `base_url` |
| Ollama | `ollama` | Local, no key, nothing leaves the machine |
| LM Studio, vLLM, llama.cpp | `openai_compat` or `llama_cpp` | Local, `base_url` on 127.0.0.1 |

```toml
[ai]
default = "anthropic"
allow_cloud = true          # a separate, deliberate decision

[ai.providers.anthropic]
kind = "anthropic"
model = "claude-opus-5-5"
credential = "jarvis/anthropic"   # the NAME of a credential entry, never the key
```

#### Without a key of your own

`jarvis_cloud` is the one cloud option that needs no key here. It posts the
conversation to the Jarvis web app's `/api/chat`, which holds a key and forwards
to whichever model the `model` field names — `openai/gpt-6-astra`,
`anthropic/claude-sonnet-5`, `google/gemini-3.5-flash`.

That is a different trade from the adapters above, and worth being clear about
rather than reading "no key" as "free":

- **The web app sees every prompt.** With your own key, the conversation reaches
  the vendor and nobody else. Here there is an extra party in between.
- **The quota is whoever published the web app's**, so rate limits and spend land
  on them, not on you.
- **The model names are the relay's**, not a vendor's. One that works against
  OpenAI directly need not work here. Press **Test** and it says so.
- **It is still a cloud provider.** `allow_cloud` gates it exactly as it gates
  the rest, and sensitive requests never reach it at all.

```toml
[ai]
default = "jarvis"
allow_cloud = true

[ai.providers.jarvis]
kind = "jarvis_cloud"
model = "openai/gpt-6-astra"
# base_url = ""     # set only to point at a different deployment
```

For a conversation that reaches the vendor and nobody else, use `anthropic`,
`openai_compat` or `google` with a key you own. For one that leaves no machine
at all, use `ollama`.

**OpenRouter is the one to reach for if you want to try many models** without an
account for each — it speaks the OpenAI API, so it needs no new adapter:

```toml
[ai.providers.openrouter]
kind = "openai_compat"
model = "anthropic/claude-opus-4.1"
base_url = "https://openrouter.ai/api/v1"
credential = "jarvis/openrouter"
```

Model names move faster than this file, so the values above are starting points
rather than promises. `GET /providers/health` reports what each configured
provider can actually reach; for Gemini it lists the models your key serves and
names alternatives when the configured one has been retired, because Google
retires IDs on its own schedule and a bare 404 explains that badly.

Two things worth knowing before you add a key:

**Nothing is sent anywhere until `allow_cloud = true`.** Storing a key is not
consent to use it; the gate is separate and off by default, and a configured
cloud provider is refused while it is off. `allow_cloud_content` is a second,
narrower switch for sending file and document *contents* rather than just your
messages.

**Local and cloud are not interchangeable on privacy.** With Ollama or a local
OpenAI-compatible server, conversations stay on the machine. With any cloud
provider they do not, and the Privacy dashboard records each time that happens.

### Turning on the wake word

**The installer includes voice.** The wake word, speech-to-text and the spoken
reply are in the packaged build, so "Hey Jarvis" needs no checkout. Earlier
builds excluded them to stay small, which made the feature unreachable from the
thing people actually download.

The *models* are not bundled — they are fetched on first use and checked against
a recorded SHA-256, which is why the installer is far smaller than the sum of
what it can do.

Running from source, install the extra yourself:

```bash
cd services/jarvis
.venv/Scripts/pip install -e ".[dev,secrets,voice]"   # macOS/Linux: .venv/bin/pip
```

On Linux also install PortAudio (`sudo apt install libportaudio2`); on Windows
it comes with the Python package, which is why the installer needs nothing
extra.

Either way, with Jarvis running:

1. **Permissions → "Use the microphone"**, and turn it on. It is off by default
   and the microphone button will not open it for you — a microphone that stays
   open is the most invasive thing this program does, so the permission is given
   once, deliberately, rather than as a side effect of pressing the button that
   uses it.
2. **Settings → Downloadable models**, and fetch the openWakeWord models
   (about 4 MB, three files) and the Piper voice (about 62 MB) if you want
   spoken replies. Each is verified against a recorded SHA-256.
3. For speech recognition, `POST /voice/stt/prepare` once — about 74 MB. This is
   the one download Jarvis does not checksum itself; faster-whisper fetches its
   own weights through `huggingface_hub`, which verifies them against the Hub's
   hashes.
4. **Settings → Voice**, and turn on *Listen for "Hey Jarvis" at startup* if you
   want it to behave like a wake word should — live from the moment Jarvis
   starts, with no button to press each session. Off, step 5 is needed every
   time.
5. Press the microphone (or just say **"Hey Jarvis"**, if you did step 4).

All three of the setting, the permission and the models have to be in place
before Jarvis listens on its own. With any of them missing it starts without
listening and says which, rather than failing to start — refusing to launch over
an unavailable microphone would be the worse bargain.

`[voice] enabled` was dead configuration until now: it was read from
`config.toml`, passed into the pipeline, reported over the API, and acted on by
nothing. Setting it did nothing at all, which is why the wake word had to be
started by hand on every launch.

The button shows what the microphone is actually doing — waiting, recording,
thinking, speaking — because every transition in the core goes through one
method that emits the state. It cannot show "listening" while the core says
otherwise. Press it again, use the tray, or hit the emergency stop to close the
microphone; the emergency stop always closes it.

What works today: the wake word fires, the pre-roll buffer keeps the first word
of "Jarvis, open Chrome", and the utterance goes through the same orchestrator
as a typed message — so a spoken request gets no more authority than a typed
one, and every tool it proposes is gated identically. What does not: playing the
spoken reply back, which needs audio output in the shell.

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

### Downloading and updating

Tagged releases are on the [releases page](https://github.com/FawadHomaan2/AI-Assistant/releases)
with a permanent link, which a CI artifact is not — those expire and need a
GitHub login.

Installing a new build over an old one keeps everything in
`%LOCALAPPDATA%\jarvis`: conversations, learned memory, the audit log,
permissions, plugins and every downloaded model. Only the program is replaced.

The app can also **update itself**, but that is switched off until a signing
key exists — the repository ships none, and a placeholder key nobody generated
would be worse than nothing. Settings → Updates says which state you are in,
and an unconfigured build makes no network request looking for updates. The
update endpoint is already configured; a public key is the only missing piece.

Turning it on is one command, `python scripts/setup_updates.py`: it generates
the keypair on your machine, writes the public half into the config, and sets
the two repository secrets. `docs/UPDATES.md` has the detail, and the manual
steps if you would rather do them yourself. Releasing works without any of it
— a key only adds the in-app update. The update path has not been run end to
end yet, and the doc says so.

If you are running from source, updating is `git pull` and `npm ci`. No
reinstall.

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
