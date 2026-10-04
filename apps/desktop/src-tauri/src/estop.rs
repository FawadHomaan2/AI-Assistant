//! Emergency stop.
//!
//! The shell holds the authoritative latch. Phase 1 has no automation to abort,
//! so engaging it sets the flag, notifies the UI, and terminates any child
//! process Jarvis itself spawned. From Phase 2 the Python core polls this through
//! its own cancellation token, and from Phase 10 every tool checks it between
//! steps.

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{AppHandle, Emitter, Runtime};

static ENGAGED: AtomicBool = AtomicBool::new(false);

pub fn engage<R: Runtime>(app: &AppHandle<R>) {
    ENGAGED.store(true, Ordering::SeqCst);
    log::warn!("EMERGENCY STOP engaged");
    let _ = app.emit("jarvis://emergency-stop", ());
}

pub fn clear() {
    ENGAGED.store(false, Ordering::SeqCst);
    log::info!("emergency stop cleared");
}

pub fn is_engaged() -> bool {
    ENGAGED.load(Ordering::SeqCst)
}

#[tauri::command]
pub fn emergency_stop(app: AppHandle) -> Result<(), String> {
    engage(&app);
    Ok(())
}

#[tauri::command]
pub fn clear_emergency_stop() -> Result<(), String> {
    clear();
    Ok(())
}

#[tauri::command]
pub fn emergency_stop_state() -> bool {
    is_engaged()
}
