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
    let privacy = MenuItem::with_id(app, "privacy_pause", "Toggle privacy pause", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit = PredefinedMenuItem::quit(app, Some("Quit Kim"))?;
    let menu = Menu::with_items(app, &[&status, &sep, &quick, &cancel, &privacy, &sep, &quit])?;

    TrayIconBuilder::with_id("kim-tray")
        .tooltip("Kim")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "quick_ask" => toggle_quick_ask(app),
            "cancel_run" => {
                // Frontend owns the TaskState; ask it to cancel.
                let _ = app.emit("kim-tray-cancel", ());
            }
            "privacy_pause" => {
                // Toggle the K9 sentinel.
                let on = crate::session_commands::get_privacy_pause();
                let _ = crate::session_commands::set_privacy_pause(!on);
            }
            _ => {}
        })
        .build(app)?;
    Ok(())
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
