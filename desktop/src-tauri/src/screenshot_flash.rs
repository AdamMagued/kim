// screenshot_flash.rs — fullscreen transparent screenshot-flash overlay window.
// Extracted from http_bridge.rs — behavior unchanged.

pub(crate) fn show_screenshot_flash_impl(app_handle: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(existing) = app_handle.get_webview_window("screenshot-flash") {
        let _ = existing.close();
    }
    // Use monitor logical size instead of fullscreen(true) to avoid the
    // macOS Spaces slide-in transition and the opaque backing-layer that
    // fullscreen mode forces (which produces the black fill).
    let (log_w, log_h, log_x, log_y) = app_handle
        .primary_monitor()
        .ok()
        .flatten()
        .map(|m| {
            let sf = m.scale_factor();
            let sz = m.size();
            let pos = m.position();
            (
                sz.width as f64 / sf,
                sz.height as f64 / sf,
                pos.x as f64 / sf,
                pos.y as f64 / sf,
            )
        })
        .unwrap_or((1920.0, 1080.0, 0.0, 0.0));

    match tauri::WebviewWindowBuilder::new(
        app_handle,
        "screenshot-flash",
        tauri::WebviewUrl::App("screenshot-flash.html".into()),
    )
    .title("")
    .inner_size(log_w, log_h)
    .position(log_x, log_y)
    .transparent(true)
    .decorations(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .visible_on_all_workspaces(true)
    .resizable(false)
    .build()
    {
        Ok(win) => {
            let win: tauri::WebviewWindow = win;
            let _ = win.set_ignore_cursor_events(true);
            let win_for_close = win.clone();
            let config = app_handle.state::<crate::config::AppConfig>();
            let delay_ms = config.screenshot_flash_duration_ms;
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(delay_ms));
                let _ = win_for_close.close();
            });
        }
        Err(e) => eprintln!("[Kim] screenshot flash window error: {e}"),
    }
}

#[tauri::command]
pub(crate) async fn show_screenshot_flash(app_handle: tauri::AppHandle) {
    show_screenshot_flash_impl(&app_handle);
}
