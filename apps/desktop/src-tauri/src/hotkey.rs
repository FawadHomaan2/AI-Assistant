//! Configurable global activation shortcut.
//!
//! This registers exactly one OS-level hotkey. It is not a keyboard hook and it
//! cannot observe any other keystroke — the distinction matters, because a
//! global input hook is what a keylogger uses, and Jarvis does not install one.

use std::str::FromStr;
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

pub const DEFAULT_ACCELERATOR: &str = "CmdOrCtrl+Space";

/// The accelerator currently registered, so Settings can display the truth.
#[derive(Default)]
pub struct ActiveShortcut(pub Mutex<Option<String>>);

/// Register `accelerator`, replacing any previous one.
///
/// Returns an error if the string does not parse or the OS refuses it (another
/// application already owns that combination — common for Ctrl+Space, which
/// some IMEs claim). On failure the previous shortcut is restored so the user is
/// never left with no way in.
pub fn register(app: &AppHandle, accelerator: &str) -> Result<String, String> {
    let shortcut = Shortcut::from_str(accelerator)
        .map_err(|e| format!("'{accelerator}' is not a valid shortcut: {e}"))?;

    let gs = app.global_shortcut();
    let previous = app
        .state::<ActiveShortcut>()
        .0
        .lock()
        .ok()
        .and_then(|g| g.clone());

    let _ = gs.unregister_all();

    if let Err(e) = gs.register(shortcut) {
        // Put the working shortcut back before reporting the failure.
        if let Some(prev) = previous.as_deref() {
            if let Ok(s) = Shortcut::from_str(prev) {
                let _ = gs.register(s);
            }
        }
        return Err(format!("Windows refused '{accelerator}': {e}"));
    }

    if let Ok(mut guard) = app.state::<ActiveShortcut>().0.lock() {
        *guard = Some(accelerator.to_string());
    }
    log::info!("global shortcut registered: {accelerator}");
    Ok(accelerator.to_string())
}

#[tauri::command]
pub fn set_global_shortcut(app: AppHandle, accelerator: String) -> Result<String, String> {
    register(&app, accelerator.trim())
}

/// Plugin wiring. Only `Pressed` is acted on, so the handler fires once per
/// press rather than twice.
pub fn plugin() -> tauri::plugin::TauriPlugin<tauri::Wry> {
    tauri_plugin_global_shortcut::Builder::new()
        .with_handler(|app, _shortcut, event| {
            if event.state() == ShortcutState::Pressed {
                crate::window::toggle(app);
                let _ = app.emit("jarvis://activated", ());
            }
        })
        .build()
}
