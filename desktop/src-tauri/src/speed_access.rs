//! K2 + K7: global quick-ask shortcut and the system tray.
//!
//! These are OS integrations driven entirely from Rust (no JS capability needed).
//! Behaviour can't be exercised in CI/headless — see the manual checklist in the
//! Prompt 11 report.

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager};

const QUICK_ASK_LABEL: &str = "quick-ask";

/// K2: show/hide the frameless always-on-top quick-ask window, creating it on
/// first use. Routed to the app with `?window=quick-ask` (mirrors the existing
/// cancel-widget pattern).
pub(crate) fn toggle_quick_ask(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(QUICK_ASK_LABEL) {
        // Toggle visibility.
        match win.is_visible() {
            Ok(true) => {
                let _ = win.hide();
            }
            _ => {
                let _ = win.show();
                let _ = win.set_focus();
            }
        }
        return;
    }
    match tauri::WebviewWindowBuilder::new(
        app,
        QUICK_ASK_LABEL,
        tauri::WebviewUrl::App("/?window=quick-ask".into()),
    )
    .title("Kim Quick Ask")
    .inner_size(560.0, 120.0)
    .resizable(false)
    .decorations(false)
    .always_on_top(true)
    .center()
    .build()
    {
        Ok(win) => {
            let _ = win.set_focus();
        }
        Err(e) => eprintln!("[Kim] quick-ask window failed: {e}"),
    }
}

/// K2: register the default quick-ask shortcut (Alt+Space). Failure (hotkey
/// already taken) is logged, never fatal — the frontend surfaces a toast via the
/// Settings → System pane when rebinding.
pub(crate) fn register_quick_ask_shortcut(app: &AppHandle) {
    use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};
    let shortcut = Shortcut::new(Some(Modifiers::ALT), Code::Space);
    if let Err(e) = app.global_shortcut().register(shortcut) {
        eprintln!("[Kim] quick-ask shortcut (Alt+Space) registration failed: {e}");
    }
}

/// K7: build the system tray. Status line is refreshed from the running-task
/// state via `set_tray_status`.
pub(crate) fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", "Kim — idle", false, None::<&str>)?;
    let quick = MenuItem::with_id(app, "quick_ask", "Quick ask", true, None::<&str>)?;
    let cancel = MenuItem::with_id(app, "cancel_run", "Cancel current run", true, None::<&str>)?;
    let privacy = MenuItem::with_id(
        app,
        "privacy_pause",
        "Toggle privacy pause",
        true,
        None::<&str>,
    )?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = PredefinedMenuItem::quit(app, Some("Quit Kim"))?;
    let menu = Menu::with_items(
        app,
        &[&status, &sep, &quick, &cancel, &privacy, &sep, &quit],
    )?;

    TrayIconBuilder::with_id("kim-tray")
        .tooltip("Kim")
        .menu(&menu)
        .on_menu_event(|app, event| match tray_action_for(event.id.as_ref()) {
            TrayAction::QuickAsk => toggle_quick_ask(app),
            TrayAction::CancelRun => {
                // F-H-5: cancel the running task DIRECTLY in Rust. The previous
                // `kim-tray-cancel` event had NO frontend listener, so the menu
                // item was dead. cancel_task performs the same SIGTERM->SIGKILL
                // the UI cancel button triggers (ChatView/CancelWidget invoke
                // it), and the UI still updates via the kim-agent-cancelled /
                // kim-agent-done events cancel_task emits — no JS listener needed.
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(e) = crate::subprocess::cancel_task(app).await {
                        eprintln!("[Kim] tray cancel: {e}");
                    }
                });
            }
            TrayAction::PrivacyPause => {
                // Toggle the K9 sentinel.
                // L-TRAY-1: surface a failed toggle (e.g. ~/.kim not writable)
                // instead of silently leaving capture in its previous state.
                let on = crate::session_commands::get_privacy_pause();
                if let Err(e) = crate::session_commands::set_privacy_pause(!on) {
                    eprintln!("[Kim] privacy-pause toggle failed (capture state unchanged): {e}");
                    let _ = app.emit(
                        "kim-agent-error",
                        format!("Privacy pause toggle failed: {e}"),
                    );
                }
            }
            TrayAction::Unknown => {}
        })
        .build(app)?;
    Ok(())
}

/// Tray menu actions. F-H-5: extracted from the `on_menu_event` closure so the
/// id→action wiring is unit-testable (the closure itself needs a live app).
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum TrayAction {
    QuickAsk,
    CancelRun,
    PrivacyPause,
    Unknown,
}

pub(crate) fn tray_action_for(id: &str) -> TrayAction {
    match id {
        "quick_ask" => TrayAction::QuickAsk,
        "cancel_run" => TrayAction::CancelRun,
        "privacy_pause" => TrayAction::PrivacyPause,
        _ => TrayAction::Unknown,
    }
}

/// K7: update the tray status line. Called from the running-task tracking.
pub(crate) fn set_tray_status(app: &AppHandle, running_task: Option<&str>) {
    if let Some(tray) = app.tray_by_id("kim-tray") {
        let label = match running_task {
            Some(task) => {
                let mut t: String = task.chars().take(40).collect();
                if task.chars().count() > 40 {
                    t.push('…');
                }
                format!("Running: {t}")
            }
            None => "Kim — idle".to_string(),
        };
        let _ = tray.set_tooltip(Some(&label));
    }
}

#[cfg(test)]
mod tray_tests {
    use super::*;

    #[test]
    fn tray_cancel_maps_to_direct_cancel_action() {
        // F-H-5: the "Cancel run" tray item must resolve to the direct-cancel
        // action (cancel_task), not the removed orphaned kim-tray-cancel event.
        assert_eq!(tray_action_for("cancel_run"), TrayAction::CancelRun);
        assert_eq!(tray_action_for("quick_ask"), TrayAction::QuickAsk);
        assert_eq!(tray_action_for("privacy_pause"), TrayAction::PrivacyPause);
        assert_eq!(tray_action_for("something_else"), TrayAction::Unknown);
    }
}
