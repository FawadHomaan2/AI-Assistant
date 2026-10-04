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

/// Latch the stop, notifying the interface only on a real transition.
///
/// The guard is not an optimisation. The webview listens for
/// `jarvis://emergency-stop` and responds by engaging the stop, which calls
/// straight back into here — so emitting unconditionally turned one button
/// press into an unbounded loop: engage, emit, listen, engage. Every turn of it
/// also posted to the core and appended a message, so the window stopped
/// responding and the only way out was killing the process.
///
/// `swap` returns the previous value, so the event fires once per genuine
/// false-to-true change and a second engage while already stopped is silent.
pub fn engage<R: Runtime>(app: &AppHandle<R>) -> bool {
    if ENGAGED.swap(true, Ordering::SeqCst) {
        log::debug!("emergency stop already engaged; not re-notifying");
        return false;
    }
    log::warn!("EMERGENCY STOP engaged");
    let _ = app.emit("jarvis://emergency-stop", ());
    true
}

/// Release the latch, notifying the interface only on a real transition.
///
/// Guarded for the same reason as `engage`: without it, a `cleared` event that
/// the interface responds to by clearing would loop in the other direction.
pub fn clear_and_notify<R: Runtime>(app: &AppHandle<R>) -> bool {
    if !ENGAGED.swap(false, Ordering::SeqCst) {
        return false;
    }
    log::info!("emergency stop cleared");
    let _ = app.emit("jarvis://emergency-stop-cleared", ());
    true
}

/// Release the latch without notifying anyone. Only the tests need this; the
/// interface goes through `clear_and_notify` so it learns the state changed.
#[cfg(test)]
fn clear() {
    ENGAGED.store(false, Ordering::SeqCst);
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
pub fn clear_emergency_stop(app: AppHandle) -> Result<(), String> {
    // This is the only thing that resets the latch. It used to be unreachable:
    // the interface had no binding for it, so resuming cleared the core's stop
    // and left the shell's set, and a static flag is only reset by restarting
    // the process — which is what people ended up doing.
    clear_and_notify(&app);
    Ok(())
}

#[tauri::command]
pub fn emergency_stop_state() -> bool {
    is_engaged()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The latch is process-global, so these run in one test to keep them
    /// ordered rather than racing each other through a shared static.
    #[test]
    fn transitions_are_reported_once_each() {
        clear();
        assert!(!is_engaged());

        // First engage transitions and so would notify; the second does not,
        // which is what stops the engage/emit/listen/engage loop.
        assert!(!ENGAGED.swap(true, Ordering::SeqCst));
        assert!(is_engaged());
        assert!(ENGAGED.swap(true, Ordering::SeqCst));

        // Clearing transitions once; clearing again is a no-op.
        assert!(ENGAGED.swap(false, Ordering::SeqCst));
        assert!(!is_engaged());
        assert!(!ENGAGED.swap(false, Ordering::SeqCst));
    }

    #[test]
    fn clear_resets_the_latch() {
        ENGAGED.store(true, Ordering::SeqCst);
        clear();
        assert!(
            !is_engaged(),
            "a latch only a restart could reset is the bug"
        );
    }
}
