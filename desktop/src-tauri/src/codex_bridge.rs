// codex_bridge.rs — Codex file-bridge command watcher.
// Extracted from http_bridge.rs — behavior unchanged.

use crate::http_bridge::capitalize;
use crate::screenshot_flash::show_screenshot_flash_impl;
use crate::*;
use tauri::Manager;

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

        let mut last_status_write = std::time::Instant::now();

        loop {
            std::thread::sleep(std::time::Duration::from_millis(500));

            // ── Handle browser_cmd.json ─────────────────────────────────────
            // L-BRIDGE-8: existence IS newness — the file is deleted after
            // every consume, so any present file is a fresh command. The old
            // mtime comparison silently dropped commands written within the
            // same coarse (1s-granularity) mtime tick as the previous one.
            if cmd_path.exists() {
                if let Ok(text) = fs::read_to_string(&cmd_path) {
                    let _ = fs::remove_file(&cmd_path); // consume once
                    if let Ok(cmd) = serde_json::from_str::<serde_json::Value>(&text) {
                        let action = cmd
                            .get("action")
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
                                let site =
                                    cmd.get("site").and_then(|v| v.as_str()).unwrap_or("claude");
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

            // ── Write bridge_status.json every 5 s ─────────────────────────
            if last_status_write.elapsed() >= std::time::Duration::from_secs(5) {
                last_status_write = std::time::Instant::now();
                let (window_open, current_url) =
                    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
                        let url = win.url().map(|u| u.to_string()).unwrap_or_default();
                        (true, url)
                    } else {
                        (false, String::new())
                    };
                let current_site = url_to_site(&current_url);
                let signed_in = url_looks_signed_in(&current_url);
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

/// L-BRIDGE-9: parsed-URL sign-in heuristic. The old raw-substring version
/// (`!url.contains("auth")` etc.) misreported signed_in=false for any slug
/// containing "author"/"login"/"authenticity" anywhere in the URL. Parse the
/// URL and only treat auth-ish HOSTS or auth-ish PATH SEGMENTS as signed-out.
fn url_looks_signed_in(raw_url: &str) -> bool {
    if raw_url.trim().is_empty() {
        return false;
    }
    let Ok(url) = url::Url::parse(raw_url) else {
        return false;
    };
    let host = url.host_str().unwrap_or_default().to_ascii_lowercase();
    // Dedicated auth hosts (Google sign-in, SSO redirects).
    if host == "accounts.google.com" || host.starts_with("auth.") || host.starts_with("login.") {
        return false;
    }
    let auth_segments = ["login", "signin", "sign-in", "auth", "authorize", "oauth"];
    let path_is_auth = url
        .path_segments()
        .into_iter()
        .flatten()
        .any(|seg| auth_segments.contains(&seg.to_ascii_lowercase().as_str()));
    !path_is_auth
}

fn url_to_site(url: &str) -> &'static str {
    if url.contains("chatgpt.com") || url.contains("chat.openai.com") {
        "chatgpt"
    } else if url.contains("gemini.google.com") {
        "gemini"
    } else if url.contains("deepseek.com") {
        "deepseek"
    } else if url.contains("grok.com") || url.contains("grok.x.com") || url.contains("x.com/i/grok")
    {
        "grok"
    } else if url.contains("claude.ai") {
        "claude"
    } else {
        "unknown"
    }
}

#[cfg(test)]
mod codex_bridge_tests {
    use super::*;

    // L-BRIDGE-9: signed-in heuristic must not trip on slugs merely containing
    // auth-ish substrings, but must catch real login/auth pages.
    #[test]
    fn signed_in_heuristic_uses_parsed_url() {
        // False positives under the old substring check — now signed in.
        assert!(url_looks_signed_in("https://claude.ai/chat/talk-about-authors"));
        assert!(url_looks_signed_in("https://chatgpt.com/c/login-page-design-help"));
        assert!(url_looks_signed_in("https://gemini.google.com/app/abc123"));

        // Real auth pages — signed out.
        assert!(!url_looks_signed_in("https://accounts.google.com/v3/signin/identifier"));
        assert!(!url_looks_signed_in("https://claude.ai/login"));
        assert!(!url_looks_signed_in("https://chatgpt.com/auth/login"));
        assert!(!url_looks_signed_in("https://auth.openai.com/authorize?x=1"));

        // Empty / unparsable.
        assert!(!url_looks_signed_in(""));
        assert!(!url_looks_signed_in("not a url"));
    }
}
