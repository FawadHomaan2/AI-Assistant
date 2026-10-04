//! Jarvis desktop shell.
//!
//! Responsibilities kept in Rust rather than the webview:
//!   * the window, the system tray and the single global shortcut
//!   * live machine metrics (`sysinfo`)
//!   * the authoritative emergency-stop latch
//!   * from Phase 2, supervising the Python core as a sidecar
//!
//! Everything privileged lives behind an explicit `#[tauri::command]`, and the
//! capability manifest in `capabilities/` is deny-by-default, so the webview
//! can reach nothing that is not listed there.

mod estop;
mod hotkey;
mod sidecar;
mod system;
mod tray;
mod window;

use serde::Serialize;
use tauri::{Emitter, Manager};

#[derive(Serialize)]
struct ShellInfo {
    version: String,
    tauri: String,
    platform: String,
}

#[tauri::command]
fn shell_info() -> ShellInfo {
    ShellInfo {
        version: env!("CARGO_PKG_VERSION").to_string(),
        tauri: tauri::VERSION.to_string(),
        platform: std::env::consts::OS.to_string(),
    }
}

pub fn run() {
    let mut builder = tauri::Builder::default();

    // One Jarvis at a time: a second launch focuses the running instance instead
    // of starting a rival tray icon and fighting over the global shortcut.
    #[cfg(not(any(target_os = "android", target_os = "ios")))]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            window::show_and_focus(app);
            let _ = app.emit("jarvis://activated", ());
        }));
        builder = builder.plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ));
    }

    builder
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(hotkey::plugin())
        .manage(system::Sampler::new())
        .manage(hotkey::ActiveShortcut::default())
        .manage(sidecar::Sidecar::default())
        .invoke_handler(tauri::generate_handler![
            shell_info,
            system::system_snapshot,
            window::hide_to_tray,
            hotkey::set_global_shortcut,
            estop::emergency_stop,
            estop::clear_emergency_stop,
            estop::emergency_stop_state,
            sidecar::core_endpoint,
            sidecar::core_status,
            sidecar::restart_core,
        ])
        .setup(|app| {
            let handle = app.handle();

            tray::build(handle)?;

            // A refused shortcut is logged and surfaced in Settings rather than
            // aborting startup — the window and tray still work without it.
            if let Err(e) = hotkey::register(handle, hotkey::DEFAULT_ACCELERATOR) {
                log::warn!("could not register default global shortcut: {e}");
            }

            // Start the Python core in the background. A failure here is
            // reported in the UI rather than preventing the app from opening,
            // so the user can still read the error and reach Settings.
            let core_handle = handle.clone();
            std::thread::spawn(move || {
                if let Err(e) = sidecar::start(&core_handle) {
                    log::error!("core failed to start: {e}");
                    let _ = core_handle.emit("jarvis://core-error", e);
                }
            });

            Ok(())
        })
        .on_window_event(|win, event| {
            // Closing the window hides Jarvis to the tray. Exit is deliberate,
            // via the tray menu, so the assistant is not killed by a stray
            // click on the title bar.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = win.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Jarvis")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                app.state::<sidecar::Sidecar>().shutdown();
            }
        });
}
