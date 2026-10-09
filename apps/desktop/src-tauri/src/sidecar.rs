//! Supervises the Python core.
//!
//! The core is spawned as a child process. It binds an ephemeral loopback port,
//! mints a one-time bearer token, and writes a single JSON handshake line to
//! stdout; everything else it logs goes to stderr. We parse that line, keep the
//! port and token in memory only, and hand them to the webview on request.
//!
//! The token is never written to disk, so it cannot be recovered after exit.

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

/// How long to wait for the handshake line before giving up.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Deserialize)]
struct Handshake {
    port: u16,
    token: String,
    pid: u32,
    version: String,
}

/// What the frontend needs to talk to the core.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CoreEndpoint {
    pub base_url: String,
    pub ws_url: String,
    pub token: String,
    pub version: String,
    pub pid: u32,
}

#[derive(Default)]
pub struct Sidecar {
    inner: Mutex<Option<Running>>,
}

struct Running {
    child: Child,
    endpoint: CoreEndpoint,
}

impl Sidecar {
    pub fn endpoint(&self) -> Option<CoreEndpoint> {
        self.inner
            .lock()
            .ok()
            .and_then(|guard| guard.as_ref().map(|r| r.endpoint.clone()))
    }

    /// True when the child is still alive. Reaps it if it has exited.
    pub fn is_running(&self) -> bool {
        let Ok(mut guard) = self.inner.lock() else {
            return false;
        };
        let Some(running) = guard.as_mut() else {
            return false;
        };
        match running.child.try_wait() {
            Ok(None) => true,
            _ => {
                *guard = None;
                false
            }
        }
    }

    pub fn shutdown(&self) {
        if let Ok(mut guard) = self.inner.lock() {
            if let Some(mut running) = guard.take() {
                log::info!("stopping core (pid {})", running.endpoint.pid);
                let _ = running.child.kill();
                let _ = running.child.wait();
            }
        }
    }
}

/// Locate the core executable.
///
/// In a packaged build it sits in `core/` under the resource directory: a
/// one-folder PyInstaller bundle, shipped through `resources` rather than
/// `externalBin`. It was one file until the extraction cost showed up as eight
/// seconds of an unresponsive window on every launch, and a one-folder bundle
/// is a tree, which `externalBin` does not carry.
///
/// `core/` first, then the old flat path, because an in-place update leaves the
/// previous layout on disk and a core that cannot be found is a core that
/// cannot report why.
///
/// During development we fall back to running the Python package from the repo,
/// so `tauri dev` works without a PyInstaller build first.
fn command(app: &AppHandle) -> Option<Command> {
    let name = if cfg!(windows) {
        "jarvis-core.exe"
    } else {
        "jarvis-core"
    };
    if let Ok(dir) = app.path().resource_dir() {
        for exe in [dir.join("core").join(name), dir.join(name)] {
            if exe.exists() {
                // From its own directory: the bundle finds `_internal` beside
                // the executable, and inherits our working directory otherwise.
                let mut cmd = Command::new(&exe);
                if let Some(parent) = exe.parent() {
                    cmd.current_dir(parent);
                }
                return Some(cmd);
            }
        }
    }

    // Development: run the package from the repository checkout.
    let repo_core = std::env::current_dir().ok()?.ancestors().find_map(|dir| {
        let candidate = dir.join("services").join("jarvis");
        candidate
            .join("pyproject.toml")
            .exists()
            .then_some(candidate)
    })?;

    let venv_python = repo_core.join(if cfg!(windows) {
        ".venv/Scripts/python.exe"
    } else {
        ".venv/bin/python"
    });
    let python = if venv_python.exists() {
        venv_python
    } else {
        std::path::PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
    };

    let mut cmd = Command::new(python);
    cmd.arg("-m").arg("jarvis").current_dir(&repo_core);
    log::info!("using development core at {}", repo_core.display());
    Some(cmd)
}

/// Spawn the core and block until it reports its port and token.
pub fn start(app: &AppHandle) -> Result<CoreEndpoint, String> {
    let state = app.state::<Sidecar>();
    if let Some(existing) = state.endpoint() {
        if state.is_running() {
            return Ok(existing);
        }
    }

    let mut cmd = command(app).ok_or_else(|| {
        "Could not find the Jarvis core. In a packaged build it ships beside the app; \
         in development it runs from services/jarvis."
            .to_string()
    })?;

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let mut child = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Could not start the Jarvis core: {e}"))?;

    // Forward the core's stderr into our log rather than discarding it; a
    // startup failure is otherwise invisible.
    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                log::info!("[core] {line}");
            }
        });
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "core stdout was not captured".to_string())?;

    // Read the handshake on a worker so a core that never prints one cannot
    // block the UI thread forever.
    let (tx, rx) = std::sync::mpsc::channel::<Result<Handshake, String>>();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let result = match reader.read_line(&mut line) {
            Ok(0) => Err("core exited before sending its handshake".to_string()),
            Ok(_) => serde_json::from_str::<Handshake>(line.trim())
                .map_err(|e| format!("could not parse the core handshake ({e}): {}", line.trim())),
            Err(e) => Err(format!("could not read the core handshake: {e}")),
        };
        let _ = tx.send(result);
        // Keep draining stdout so the child never blocks on a full pipe.
        for extra in reader.lines().map_while(Result::ok) {
            log::debug!("[core stdout] {extra}");
        }
    });

    let started = Instant::now();
    let handshake = match rx.recv_timeout(HANDSHAKE_TIMEOUT) {
        Ok(Ok(h)) => h,
        Ok(Err(e)) => {
            let _ = child.kill();
            return Err(e);
        }
        Err(_) => {
            let _ = child.kill();
            return Err(format!(
                "The Jarvis core did not start within {}s.",
                HANDSHAKE_TIMEOUT.as_secs()
            ));
        }
    };

    let endpoint = CoreEndpoint {
        base_url: format!("http://127.0.0.1:{}", handshake.port),
        ws_url: format!("ws://127.0.0.1:{}/ws", handshake.port),
        token: handshake.token,
        version: handshake.version,
        pid: handshake.pid,
    };

    log::info!(
        "core ready on port {} in {}ms (pid {})",
        handshake.port,
        started.elapsed().as_millis(),
        handshake.pid
    );

    if let Ok(mut guard) = state.inner.lock() {
        *guard = Some(Running {
            child,
            endpoint: endpoint.clone(),
        });
    }

    let _ = app.emit("jarvis://core-ready", &endpoint);
    Ok(endpoint)
}

/// Where the core is listening, starting it if it is not already up.
#[tauri::command]
pub fn core_endpoint(app: AppHandle) -> Result<CoreEndpoint, String> {
    let state = app.state::<Sidecar>();
    if state.is_running() {
        if let Some(endpoint) = state.endpoint() {
            return Ok(endpoint);
        }
    }
    start(&app)
}

#[tauri::command]
pub fn core_status(app: AppHandle) -> serde_json::Value {
    let state = app.state::<Sidecar>();
    serde_json::json!({
        "running": state.is_running(),
        "endpoint": state.endpoint(),
    })
}

#[tauri::command]
pub fn restart_core(app: AppHandle) -> Result<CoreEndpoint, String> {
    app.state::<Sidecar>().shutdown();
    start(&app)
}
