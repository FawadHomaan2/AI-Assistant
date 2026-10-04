//! Window visibility. Closing the window hides Jarvis to the tray; it does not
//! quit, so the global shortcut keeps working.

use tauri::{AppHandle, Manager, WebviewWindow};

pub const MAIN: &str = "main";

pub fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(MAIN)
}

/// Show, unminimise and focus. Used by the tray and the global shortcut.
pub fn show_and_focus(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Toggle: a second press of the global shortcut hides the window again.
pub fn toggle(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let visible = w.is_visible().unwrap_or(false);
        let focused = w.is_focused().unwrap_or(false);
        if visible && focused {
            let _ = w.hide();
        } else {
            show_and_focus(app);
        }
    }
}

#[tauri::command]
pub fn hide_to_tray(app: AppHandle) -> Result<(), String> {
    main_window(&app)
        .ok_or_else(|| "main window not found".to_string())?
        .hide()
        .map_err(|e| e.to_string())
}
