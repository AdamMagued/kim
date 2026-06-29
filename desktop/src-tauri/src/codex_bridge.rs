// codex_bridge.rs — Codex file-bridge command watcher.
// Extracted from http_bridge.rs — behavior unchanged.

use crate::*;
use tauri::Manager;
use crate::http_bridge::capitalize;
use crate::screenshot_flash::show_screenshot_flash_impl;

/// Return the per-user, 0700 bridge directory (#14).
/// Uses `$HOME/.kim/codex_bridge` instead of the world-readable `/tmp/codex_bridge`
/// so other local users cannot inject commands or read bridge status.
fn codex_bridge_dir() -> std::path::PathBuf {
    let dir = dirs::home_dir()
        .map(|h| h.join(".kim").join("codex_bridge"))
        .unwrap_or_else(|| std::path::PathBuf::from("/tmp/codex_bridge"));
    // Best-effort: create with 0700 on Unix so other users cannot read/write.
    if !dir.exists() {
        if let Err(e) = std::fs::create_dir_all(&dir) {
            eprintln!("[Kim] codex_bridge_dir: create_dir_all failed: {}", e);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
        }
    }
    dir
}

/// Spawn a background thread that:
///  - Polls `$HOME/.kim/codex_bridge/browser_cmd.json` every 500 ms and dispatches
///    show/hide/switch_site actions to the Kim webview window.
///  - Writes `$HOME/.kim/codex_bridge/bridge_status.json` every 5 s so Codex's
///    `/browser status` command can report the current bridge state.
pub(crate) fn start_bridge_file_watcher(app_handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        let bridge_dir = codex_bridge_dir();
        let cmd_path = bridge_dir.join("browser_cmd.json");
        let status_path = bridge_dir.join("bridge_status.json");

        let mut last_cmd_mtime: Option<std::time::SystemTime> = None;
        let mut last_status_write = std::time::Instant::now();

        loop {
            std::thread::sleep(std::time::Duration::from_millis(500));

            // ── Handle browser_cmd.json ─────────────────────────────────────
            if let Ok(meta) = fs::metadata(&cmd_path) {
                if let Ok(modified) = meta.modified() {
                    let is_new = last_cmd_mtime.is_none_or(|prev| modified > prev);
                    if is_new {
                        last_cmd_mtime = Some(modified);
                        if let Ok(text) = fs::read_to_string(&cmd_path) {
                            let _ = fs::remove_file(&cmd_path); // consume once
                            if let Ok(cmd) = serde_json::from_str::<serde_json::Value>(&text) {
                                let action = cmd.get("action")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_string();
                                let app = app_handle.clone();
                                match action.as_str() {
                                    "show_window" => {
                                        show_browser_window_impl(&app);
                                    }
                                    "hide_window" => {
                                        if let Some(win) = app.get_webview_window("kim-browser-signin") {
                                            hide_browser_window_offscreen(&win);
                                        }
                                    }
                                    "switch_site" => {
                                        let site = cmd.get("site")
                                            .and_then(|v| v.as_str())
                                            .unwrap_or("claude");
                                        let url = site_to_url(site);
                                        let provider = Some(format!("{} (via /model)", capitalize(site)));
                                        let _ = open_browser_signin_window_impl(&url, provider, &app);
                                    }
                                    "screenshot_flash" => {
                                        show_screenshot_flash_impl(&app);
                                    }
                                    _ => {
                                        eprintln!("[Kim] Unknown browser cmd action: {action}");
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // ── Write bridge_status.json every 5 s ─────────────────────────
            if last_status_write.elapsed() >= std::time::Duration::from_secs(5) {
                last_status_write = std::time::Instant::now();
                let (window_open, current_url) = if let Some(win) =
                    app_handle.get_webview_window("kim-browser-signin")
                {
                    let url = win.url().map(|u| u.to_string()).unwrap_or_default();
                    (true, url)
                } else {
                    (false, String::new())
                };
                let current_site = url_to_site(&current_url);
                let signed_in = !current_url.is_empty()
                    && !current_url.contains("login")
                    && !current_url.contains("signin")
                    && !current_url.contains("sign-in")
                    && !current_url.contains("auth");
                let status = serde_json::json!({
                    "window_open": window_open,
                    "current_site": current_site,
                    "current_url": current_url,
                    "bridge_version": 8,
                    "signed_in": signed_in,
                });
                let _ = fs::create_dir_all(&bridge_dir);
                if let Ok(text) = serde_json::to_string_pretty(&status) {
                    let _ = fs::write(&status_path, text);
                }
            }
        }
    });
}

fn site_to_url(site: &str) -> String {
    match site {
        "chatgpt" => "https://chatgpt.com/".to_string(),
        "gemini" => "https://gemini.google.com/app".to_string(),
        "deepseek" => "https://chat.deepseek.com/".to_string(),
        "grok" => "https://grok.com/".to_string(),
        _ => "https://claude.ai/new".to_string(),
    }
}

fn url_to_site(url: &str) -> &'static str {
    if url.contains("chatgpt.com") || url.contains("chat.openai.com") {
        "chatgpt"
    } else if url.contains("gemini.google.com") {
        "gemini"
    } else if url.contains("deepseek.com") {
        "deepseek"
    } else if url.contains("grok.com") || url.contains("grok.x.com") || url.contains("x.com/i/grok") {
        "grok"
    } else if url.contains("claude.ai") {
        "claude"
    } else {
        "unknown"
    }
}
