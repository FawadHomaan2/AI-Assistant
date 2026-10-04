//! Checking for and installing updates.
//!
//! Driven from Rust rather than from the webview. `tauri-plugin-updater` ships
//! a JavaScript API, and granting `updater:default` would hand the interface
//! the whole check/download/install surface. The capability file is
//! deny-by-default for a reason, so the webview gets two commands instead: one
//! that reports what is available, and one that installs. Nothing in the
//! webview can point the updater somewhere else or hand it bytes.
//!
//! **Updates are off until a signing key exists.** `plugins.updater.pubkey` in
//! `tauri.conf.json` is empty in the repository, because a plausible-looking
//! key nobody generated is worse than none — the same reason the model
//! catalogue leaves an unverified checksum empty. With it empty, every function
//! here reports "not configured" and makes no network request at all.
//!
//! See `docs/UPDATES.md` for generating the key and publishing a release.

use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_updater::UpdaterExt;

/// What the interface needs to render the Updates panel.
#[derive(Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct UpdateStatus {
    /// Whether a public key and an endpoint are both present.
    pub configured: bool,
    /// The running version, from `tauri.conf.json`.
    pub current_version: String,
    /// The version offered, when one is.
    pub available_version: Option<String>,
    /// Release notes, when the manifest carries them.
    pub notes: Option<String>,
    /// Publication date, as the manifest reported it.
    pub date: Option<String>,
    /// Why there is nothing to offer, in a form fit to show someone.
    pub detail: String,
}

/// Whether the updater has both halves of its configuration.
///
/// An endpoint with no key would download something it cannot verify; a key
/// with no endpoint has nothing to check. Either alone is a misconfiguration
/// rather than a working updater, so both are required.
fn configured<R: Runtime>(app: &AppHandle<R>) -> bool {
    app.config()
        .plugins
        .0
        .get("updater")
        .is_some_and(is_usable_updater_config)
}

/// The configuration check, separated from the app so it can be tested.
///
/// Both halves are required on purpose. An endpoint with no key would download
/// something it cannot verify; a key with no endpoint has nothing to check.
/// Either on its own is a half-finished setup, and treating it as working is
/// how an unverified download gets installed.
fn is_usable_updater_config(updater: &serde_json::Value) -> bool {
    let has_key = updater
        .get("pubkey")
        .and_then(|v| v.as_str())
        .is_some_and(|k| !k.trim().is_empty());
    let has_endpoint = updater
        .get("endpoints")
        .and_then(|v| v.as_array())
        .is_some_and(|e| !e.is_empty());
    has_key && has_endpoint
}

fn current_version<R: Runtime>(app: &AppHandle<R>) -> String {
    app.package_info().version.to_string()
}

/// Ask the endpoint whether a newer release exists.
///
/// Returns a populated `available_version` when one does. A check that finds
/// nothing is not an error: it is the normal case, and reporting it as a
/// failure would train people to ignore the panel.
#[tauri::command]
pub async fn check_for_update(app: AppHandle) -> Result<UpdateStatus, String> {
    let mut status = UpdateStatus {
        configured: configured(&app),
        current_version: current_version(&app),
        ..Default::default()
    };

    if !status.configured {
        status.detail = "Automatic updates are not set up for this build. \
             See docs/UPDATES.md."
            .into();
        return Ok(status);
    }

    let updater = app
        .updater()
        .map_err(|e| format!("The updater could not start: {e}"))?;

    match updater.check().await {
        Ok(Some(update)) => {
            status.available_version = Some(update.version.clone());
            status.notes = update.body.clone();
            status.date = update.date.map(|d| d.to_string());
            status.detail = format!("Version {} is available.", update.version);
        }
        Ok(None) => {
            status.detail = format!("Jarvis {} is up to date.", status.current_version);
        }
        Err(e) => {
            // Surfaced rather than swallowed: a check that silently fails
            // looks identical to one that found nothing, and the difference
            // matters when a release is overdue.
            return Err(format!("Could not check for updates: {e}"));
        }
    }
    Ok(status)
}

/// Download, verify and install the available update, then restart.
///
/// The verification is the point: the downloaded bytes are checked against the
/// public key compiled into this build before anything is written. A download
/// that fails that check is discarded.
#[tauri::command]
pub async fn install_update(app: AppHandle) -> Result<(), String> {
    if !configured(&app) {
        return Err("Automatic updates are not set up for this build.".into());
    }

    let updater = app
        .updater()
        .map_err(|e| format!("The updater could not start: {e}"))?;
    let update = updater
        .check()
        .await
        .map_err(|e| format!("Could not check for updates: {e}"))?
        .ok_or_else(|| "There is no update to install.".to_string())?;

    log::info!(
        "installing update {} over {}",
        update.version,
        update.current_version
    );

    // The core is a child process holding a loopback port and a SQLite file.
    // Leaving it running while the installer replaces its executable is how an
    // upgrade corrupts a database, so it is stopped first. The NSIS hook kills
    // it too, but by then the installer is already running.
    app.state::<crate::sidecar::Sidecar>().shutdown();

    update
        .download_and_install(|_chunk, _total| {}, || {})
        .await
        .map_err(|e| format!("The update could not be installed: {e}"))?;

    Ok(())
}

/// Report the current state without touching the network.
///
/// Called when the Settings view opens. Checking on every render would mean an
/// outbound request every time someone looks at a page, which is not something
/// to do on their behalf without asking.
#[tauri::command]
pub fn update_status(app: AppHandle) -> UpdateStatus {
    let is_configured = configured(&app);
    UpdateStatus {
        configured: is_configured,
        current_version: current_version(&app),
        detail: if is_configured {
            "Press Check for updates.".into()
        } else {
            "Automatic updates are not set up for this build. See docs/UPDATES.md.".into()
        },
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::is_usable_updater_config;
    use serde_json::json;

    #[test]
    fn both_halves_are_required() {
        let key = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk";
        let endpoint = "https://example.test/latest.json";

        assert!(is_usable_updater_config(&json!({
            "pubkey": key, "endpoints": [endpoint]
        })));

        // A key with nowhere to check has nothing to do.
        assert!(!is_usable_updater_config(&json!({
            "pubkey": key, "endpoints": []
        })));

        // An endpoint with no key would download something unverifiable.
        assert!(!is_usable_updater_config(&json!({
            "pubkey": "", "endpoints": [endpoint]
        })));

        // Whitespace is not a key.
        assert!(!is_usable_updater_config(&json!({
            "pubkey": "   ", "endpoints": [endpoint]
        })));
    }

    #[test]
    fn a_missing_or_malformed_block_is_not_configured() {
        assert!(!is_usable_updater_config(&json!({})));
        assert!(!is_usable_updater_config(&json!(null)));
        // Wrong types rather than a crash: this reads a config file a person
        // edited by hand.
        assert!(!is_usable_updater_config(&json!({
            "pubkey": 42, "endpoints": "https://example.test"
        })));
    }

    #[test]
    fn the_committed_config_ships_updates_switched_off() {
        // The repository must not carry a placeholder key that looks real. The
        // model catalogue leaves an unverified checksum empty for the same
        // reason: something plausible nobody generated is worse than nothing.
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater = &conf["plugins"]["updater"];
        assert_eq!(
            updater["pubkey"], "",
            "a key in the repo must be a real one"
        );
        assert!(!is_usable_updater_config(updater));

        // The endpoint is not a secret and ships filled in, so adding updates
        // is one paste rather than two. Asserted so it is not quietly dropped:
        // with it missing, filling in only the key would still read as
        // unconfigured and the reason would not be obvious.
        let endpoints = updater["endpoints"].as_array().expect("endpoints array");
        assert_eq!(endpoints.len(), 1, "expected exactly the releases endpoint");
        let endpoint = endpoints[0].as_str().unwrap();
        assert!(
            endpoint.starts_with("https://"),
            "an update endpoint must be TLS"
        );
        assert!(
            endpoint.ends_with("/latest.json"),
            "the endpoint must point at the manifest, not the release page"
        );

        // Downgrade protection has to be on from the first release. Enabling it
        // later rejects every release signed before the CLI recorded a version.
        assert_eq!(updater["requireSignedVersion"], true);
        assert_eq!(updater["allowDowngrades"], false);
    }

    #[test]
    fn filling_in_only_the_key_completes_the_configuration() {
        // The handoff this is built around: the endpoint ships filled in, so
        // pasting a public key is the whole remaining step.
        let conf: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let mut updater = conf["plugins"]["updater"].clone();
        updater["pubkey"] =
            serde_json::json!("dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk");
        assert!(is_usable_updater_config(&updater));
    }
}
