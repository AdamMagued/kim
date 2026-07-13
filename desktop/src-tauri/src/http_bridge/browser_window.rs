//! Window visibility / navigation routes: `/v1/hide`, `/v1/show`,
//! `/v1/browser/{show,hide,click,new-chat,current-url}` and `/v1/provider`.
//!
//! Extracted verbatim from the monolithic `http_bridge.rs` route match —
//! behavior unchanged.

use std::sync::Mutex as StdMutex;

use serde::Deserialize;
use tauri::Manager;
use tiny_http::Request;

use crate::browser_bridge::open_browser_signin_window_impl;
use crate::http_util::respond_json;
use crate::provider_url::{default_site_url, normalize_site};
use crate::{
    hide_browser_window_offscreen, is_bridge_task_running, show_browser_window_impl,
    webview_current_href, KIM_PREFERRED_SITE,
};

use super::read_body_capped;

/// `POST /v1/hide` — hide the main Kim window.
pub(super) fn hide_main(request: Request, app_handle: tauri::AppHandle) {
    if let Some(win) = app_handle.get_webview_window("main") {
        let _ = win.hide();
    }
    respond_json(request, 200, serde_json::json!({"ok": true}));
}

/// `POST /v1/show` — show and focus the main Kim window.
pub(super) fn show_main(request: Request, app_handle: tauri::AppHandle) {
    if let Some(win) = app_handle.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
    respond_json(request, 200, serde_json::json!({"ok": true}));
}

/// `GET /v1/browser/current-url` — report the provider webview's current href.
pub(super) fn current_url(request: Request, app_handle: tauri::AppHandle) {
    let current_url = app_handle
        .get_webview_window("kim-browser-signin")
        .map(|w| webview_current_href(&w))
        .filter(|u| !u.trim().is_empty());
    respond_json(
        request,
        200,
        serde_json::json!({
            "ok": true,
            "url": current_url,
        }),
    );
}

/// `POST /v1/browser/show` — bring the provider browser window on screen.
pub(super) fn browser_show(request: Request, app_handle: tauri::AppHandle) {
    if app_handle
        .get_webview_window("kim-browser-signin")
        .is_some()
    {
        show_browser_window_impl(&app_handle);
        respond_json(request, 200, serde_json::json!({"ok": true}));
    } else {
        respond_json(
            request,
            200,
            serde_json::json!({"ok": false, "message": "No browser window exists yet."}),
        );
    }
}

/// `POST /v1/browser/hide` — move the provider browser window offscreen.
pub(super) fn browser_hide(request: Request, app_handle: tauri::AppHandle) {
    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
        hide_browser_window_offscreen(&win);
    }
    respond_json(request, 200, serde_json::json!({"ok": true}));
}

/// `POST /v1/browser/click` — dispatch a click on a CSS selector in the webview.
pub(super) fn browser_click(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct ClickRequest {
        selector: String,
    }

    let parsed: ClickRequest = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            respond_json(
                request,
                400,
                serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}),
            );
            return;
        }
    };

    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
        let selector_json =
            serde_json::to_string(&parsed.selector).unwrap_or_else(|_| "\"\"".to_string());
        let js = format!(
            "(() => {{ const el = document.querySelector({}); if (el) {{ el.click(); return true; }} return false; }})()",
            selector_json
        );
        match win.eval(&js) {
            Ok(()) => {
                // M-BRIDGE-7: `eval` only confirms the script was
                // DISPATCHED — the "element found" boolean inside the
                // JS is not readable here. Report `dispatched` instead
                // of claiming `clicked: true` for selectors that may
                // have matched nothing.
                respond_json(
                    request,
                    200,
                    serde_json::json!({"ok": true, "dispatched": true}),
                );
            }
            Err(e) => {
                respond_json(
                    request,
                    500,
                    serde_json::json!({
                        "ok": false,
                        "error": format!("eval failed: {}", e),
                    }),
                );
            }
        }
    } else {
        respond_json(
            request,
            200,
            serde_json::json!({"ok": false, "clicked": false, "error": "No browser window."}),
        );
    }
}

/// `POST /v1/browser/new-chat` — reset bridge state and start a fresh chat.
pub(super) fn browser_new_chat(request: Request, app_handle: tauri::AppHandle) {
    // Reset bridge state and start a fresh conversation in the active
    // chat UI. Uses provider-specific selectors with sensible fallbacks.
    if is_bridge_task_running() {
        respond_json(
            request,
            409,
            serde_json::json!({
                "ok": false,
                "error": "Cannot start a new provider chat while Kim is running a task; this would lose LLM context.",
            }),
        );
        return;
    }

    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
        let js = r#"(() => {
            try {
                if (window.__kimBridge) {
                    window.__kimBridge._lastHash = null;
                    window.__kimBridge._currentReqId = null;
                }
                if (typeof window.__kimBridgeStore === 'object' && window.__kimBridgeStore) {
                    window.__kimBridgeStore = {};
                }
            } catch (_) {}

            const candidates = [
                'button[aria-label*="New chat" i]',
                'a[aria-label*="New chat" i]',
                '[data-testid*="new-chat" i]',
                'button[aria-label*="New conversation" i]',
                'a[href$="/app"]',
            ];
            for (const sel of candidates) {
                try {
                    const el = document.querySelector(sel);
                    if (el) { el.click(); return true; }
                } catch (_) {}
            }
            // Fallback for Gemini: navigate to /app which always opens fresh
            if (location.hostname.includes('gemini.google.com')) {
                location.href = 'https://gemini.google.com/app';
                return true;
            }
            return false;
        })()"#;
        match win.eval(js) {
            Ok(()) => respond_json(request, 200, serde_json::json!({"ok": true})),
            Err(e) => respond_json(
                request,
                500,
                serde_json::json!({
                    "ok": false,
                    "error": format!("eval failed: {}", e),
                }),
            ),
        }
    } else {
        respond_json(
            request,
            200,
            serde_json::json!({"ok": false, "error": "No browser window."}),
        );
    }
}

/// `POST /v1/provider` — set the preferred provider site and navigate/open.
pub(super) fn set_provider(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct ProviderRequest {
        site: String,
    }

    let parsed: ProviderRequest = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            respond_json(
                request,
                400,
                serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}),
            );
            return;
        }
    };

    let site = normalize_site(&parsed.site);
    if let Ok(mut guard) = KIM_PREFERRED_SITE
        .get_or_init(|| StdMutex::new(None))
        .lock()
    {
        *guard = Some(site.clone());
    }
    let url = default_site_url(&site);

    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
        if is_bridge_task_running() {
            respond_json(
                request,
                409,
                serde_json::json!({
                    "ok": false,
                    "error": "Cannot navigate the provider browser while Kim is running a task; this would lose LLM context.",
                }),
            );
            return;
        }

        // L-BRIDGE-13: a failed navigation eval must not report
        // {"ok": true} — the caller would proceed against the old page.
        match serde_json::to_string(url)
            .map_err(|e| e.to_string())
            .and_then(|js_url| {
                win.eval(format!("window.location.href = {};", js_url))
                    .map_err(|e| e.to_string())
            }) {
            Ok(()) => respond_json(request, 200, serde_json::json!({"ok": true, "site": site})),
            Err(e) => respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": format!("Navigation failed: {e}"), "site": site}),
            ),
        }
    } else {
        if is_bridge_task_running() {
            respond_json(
                request,
                409,
                serde_json::json!({
                    "ok": false,
                    "error": "Cannot open the provider browser while Kim is running a task; this would lose LLM context.",
                }),
            );
            return;
        }

        // Open the browser window with this provider
        match open_browser_signin_window_impl(url, Some(site.clone()), &app_handle) {
            Ok(_) => {
                show_browser_window_impl(&app_handle);
                respond_json(
                    request,
                    200,
                    serde_json::json!({"ok": true, "site": site.clone(), "opened": true}),
                );
            }
            Err(e) => {
                respond_json(
                    request,
                    500,
                    serde_json::json!({"ok": false, "error": format!("{}", e)}),
                );
            }
        }
    }
}
