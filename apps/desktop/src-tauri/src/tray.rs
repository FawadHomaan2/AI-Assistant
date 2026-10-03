//! System tray icon and menu.
//!
//! Menu items that correspond to unimplemented features navigate to the
//! relevant page, where the UI states plainly what is missing. They do not
//! silently do nothing.

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter};

use crate::window;

pub fn build(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Assistant", true, None::<&str>)?;
    let voice = MenuItem::with_id(app, "voice", "Start Voice Mode", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "Pause Assistant", true, None::<&str>)?;
    let security = MenuItem::with_id(app, "security", "Security Status", true, None::<&str>)?;
    let activity = MenuItem::with_id(app, "activity", "View Activity", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "Settings", true, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "STOP ALL ACTIONS", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Exit", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;

    let menu = Menu::with_items(
        app,
        &[
            &open, &voice, &pause, &sep, &security, &activity, &settings, &sep, &stop, &sep, &quit,
        ],
    )?;

    TrayIconBuilder::with_id("juno-tray")
        .icon(
            app.default_window_icon()
                .cloned()
                .ok_or_else(|| tauri::Error::AssetNotFound("default window icon missing".into()))?,
        )
        .tooltip("Juno — AI assistant")
        .menu(&menu)
        // The menu must not also open on left click, or the click-to-show
        // gesture below never fires.
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| on_menu(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                window::show_and_focus(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "open" => window::show_and_focus(app),
        "voice" => {
            window::show_and_focus(app);
            let _ = app.emit("juno://navigate", "chat");
            let _ = app.emit("juno://start-voice", ());
        }
        "pause" => {
            let _ = app.emit("juno://pause", ());
        }
        "security" => {
            window::show_and_focus(app);
            let _ = app.emit("juno://navigate", "security");
        }
        "activity" => {
            window::show_and_focus(app);
            let _ = app.emit("juno://navigate", "activity");
        }
        "settings" => {
            window::show_and_focus(app);
            let _ = app.emit("juno://navigate", "settings");
        }
        "stop" => {
            // Latch the stop in the shell as well as the UI, so it holds even
            // if the webview is not currently showing.
            crate::estop::engage(app);
            window::show_and_focus(app);
            let _ = app.emit("juno://emergency-stop", ());
        }
        "quit" => app.exit(0),
        other => log::warn!("unhandled tray menu id: {other}"),
    }
}
