use tauri::Manager;

#[tauri::command]
pub(crate) async fn show_main_window(app_handle: tauri::AppHandle) -> Result<(), String> {
    if let Some(win) = app_handle.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
    Ok(())
}

#[tauri::command]
pub(crate) async fn set_task_active_mode(app_handle: tauri::AppHandle, active: bool) -> Result<(), String> {
    let cancel_label = "cancel-widget";
    if active {
        // Hide main window
        if let Some(main_win) = app_handle.get_webview_window("main") {
            let _ = main_win.hide();
        }

        // Show or create cancel widget
        if let Some(cancel_win) = app_handle.get_webview_window(cancel_label) {
            let _ = cancel_win.show();
            let _ = cancel_win.set_focus();
        } else {
            let cancel_win = tauri::WebviewWindowBuilder::new(
                &app_handle,
                cancel_label,
                tauri::WebviewUrl::App("/?window=cancel".into()),
            )
            .title("Cancel Task")
            .inner_size(180.0, 50.0)
            .resizable(false)
            .always_on_top(true)
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .build()
            .map_err(|e| format!("Failed to build cancel widget: {}", e))?;

            // Position at bottom center
            if let Ok(Some(monitor)) = cancel_win.current_monitor() {
                let size = monitor.size();
                let scale_factor = monitor.scale_factor();
                let width = 180.0 * scale_factor;
                let height = 50.0 * scale_factor;
                let x = (size.width as f64 - width) / 2.0;
                let y = size.height as f64 - height - (80.0 * scale_factor); // 80px from bottom
                let _ = cancel_win.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(x as i32, y as i32)));
            }
        }
    } else {
        // Destroy (not just hide) the cancel widget so a stale instance can
        // never end up stacked under a new one on the next run. (issue #3 §5)
        if let Some(cancel_win) = app_handle.get_webview_window(cancel_label) {
            let _ = cancel_win.close();
        }

        // Show main window
        if let Some(main_win) = app_handle.get_webview_window("main") {
            let _ = main_win.show();
            let _ = main_win.set_focus();
        }
    }
    Ok(())
}
