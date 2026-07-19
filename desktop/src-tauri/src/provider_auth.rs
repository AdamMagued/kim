//! Provider auth: status probe, sign-in popup, sign-out.
//! Extracted from lib.rs (file-split restructure) — behavior unchanged.

use serde::{Deserialize, Serialize};

use std::collections::HashMap;
use std::time::Duration;
use tauri::{Emitter, Manager};

use crate::*;

// ---------------------------------------------------------------------------
// Provider auth: status probe, sign-in popup, sign-out
//
// These commands let the React side display a "Signed in as X" indicator
// below the chat composer and trigger an OAuth-style sign-in flow that uses
// the real provider page underneath. Auth state is sourced from the actual
// provider cookies inside the kim-browser-signin webview, so it survives app
// restarts and naturally reflects expiry / multi-account changes.
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ProviderAuthStatus {
    pub provider: String,
    pub signed_in: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub avatar: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

pub(crate) fn provider_origin(site: &str) -> Option<&'static str> {
    match site {
        "chatgpt" | "openai" => Some("https://chatgpt.com"),
        "gemini" | "google" => Some("https://gemini.google.com"),
        "deepseek" => Some("https://chat.deepseek.com"),
        _ => None,
    }
}

pub(crate) fn provider_login_url(site: &str) -> Option<String> {
    match site {
        "chatgpt" | "openai" => Some("https://chatgpt.com/auth/login".to_string()),
        "gemini" | "google" => Some(
            "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp"
                .to_string(),
        ),
        "deepseek" => Some("https://chat.deepseek.com/sign_in".to_string()),
        _ => None,
    }
}

pub(crate) fn build_auth_probe_js(site: &str, req_id: &str, base_url: &str, token: &str) -> String {
    let endpoint = match site {
        "chatgpt" | "openai" => "/api/auth/session",
        "gemini" | "google" => "__GEMINI_DOM__",
        _ => "__UNKNOWN__",
    };
    let req_id_js = serde_json::to_string(req_id).unwrap_or_else(|_| "\"\"".to_string());
    let base_js = serde_json::to_string(base_url).unwrap_or_else(|_| "\"\"".to_string());
    let token_js = serde_json::to_string(token).unwrap_or_else(|_| "\"\"".to_string());
    let site_js = serde_json::to_string(site).unwrap_or_else(|_| "\"\"".to_string());
    let endpoint_js = serde_json::to_string(endpoint).unwrap_or_else(|_| "\"\"".to_string());

    format!(
        r#"(async () => {{
    const reqId = {req_id_js};
    const baseUrl = {base_js};
    const token = {token_js};
    const site = {site_js};
    const endpoint = {endpoint_js};
    async function callback(payload) {{
        try {{
            await fetch(baseUrl + '/v1/callback', {{
                method: 'POST',
                headers: {{ 'X-Kim-Token': token, 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ req_id: reqId, payload: payload }}),
            }});
        }} catch (e) {{ console.error('[KimAuth] callback failed', e); }}
    }}
    try {{
        let info = {{ provider: site, signed_in: false }};
        if (endpoint === '__GEMINI_DOM__') {{
            const link = document.querySelector('a[aria-label*="Google Account"]')
                          || document.querySelector('a[aria-label*="compte Google"]')
                          || document.querySelector('a[aria-label*="Google-Konto"]');
            if (link) {{
                const label = link.getAttribute('aria-label') || '';
                const emailMatch = label.match(/[\w.+-]+@[\w.-]+/);
                const nameMatch = label.match(/(?:Account|compte|Konto)[^:]*:\s*([^()\n,]+?)(?:\s*\(|,|$)/i);
                info = {{
                    provider: 'gemini',
                    signed_in: true,
                    email: emailMatch ? emailMatch[0] : null,
                    name: nameMatch ? nameMatch[1].trim() : null,
                }};
            }}
        }} else if (endpoint !== '__UNKNOWN__') {{
            const resp = await fetch(endpoint, {{ credentials: 'include', cache: 'no-store' }});
            const text = await resp.text();
            if (resp.ok) {{
                try {{
                    const data = JSON.parse(text);
                    if (site === 'chatgpt' || site === 'openai') {{
                        if (data && data.user && data.user.email) {{
                            info = {{
                                provider: 'chatgpt',
                                signed_in: true,
                                email: data.user.email,
                                name: data.user.name || null,
                                avatar: data.user.image || data.user.picture || null,
                            }};
                        }}
                    }}
                }} catch (parseErr) {{ /* response was HTML (sign-in page) */ }}
            }}
        }}
        await callback({{ ok: true, response: JSON.stringify(info) }});
    }} catch (e) {{
        await callback({{ ok: false, error: String(e) }});
    }}
}})();"#
    )
}

pub(crate) fn parse_auth_response(
    site: &str,
    result: &BridgeCompleteResponse,
) -> ProviderAuthStatus {
    if !result.ok {
        return ProviderAuthStatus {
            provider: site.to_string(),
            signed_in: false,
            email: None,
            name: None,
            avatar: None,
            error: result.error.clone(),
        };
    }
    let response_str = result.response.as_deref().unwrap_or("{}");
    match serde_json::from_str::<serde_json::Value>(response_str) {
        Ok(v) => ProviderAuthStatus {
            provider: v
                .get("provider")
                .and_then(|x| x.as_str())
                .unwrap_or(site)
                .to_string(),
            signed_in: v
                .get("signed_in")
                .and_then(|x| x.as_bool())
                .unwrap_or(false),
            email: v.get("email").and_then(|x| x.as_str()).map(String::from),
            name: v.get("name").and_then(|x| x.as_str()).map(String::from),
            avatar: v.get("avatar").and_then(|x| x.as_str()).map(String::from),
            error: None,
        },
        Err(e) => ProviderAuthStatus {
            provider: site.to_string(),
            signed_in: false,
            email: None,
            name: None,
            avatar: None,
            error: Some(format!("parse: {}", e)),
        },
    }
}

#[tauri::command]
pub(crate) async fn provider_check_auth(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<ProviderAuthStatus, String> {
    let site = normalize_site(&provider);
    let origin =
        provider_origin(&site).ok_or_else(|| format!("Unsupported provider: {}", provider))?;

    let cfg = WEBVIEW_BRIDGE_CFG
        .get()
        .ok_or_else(|| "Bridge server not initialised yet.".to_string())?
        .clone();

    // Ensure the kim-browser-signin webview exists and is on the provider's
    // origin (so document.cookie is the right jar for the fetch). Stay hidden.
    let webview = match app_handle.get_webview_window("kim-browser-signin") {
        Some(w) => {
            let current = w.url().map(|u| u.to_string()).unwrap_or_default();
            if !current.starts_with(origin) {
                let js_url = serde_json::to_string(origin).map_err(|e| e.to_string())?;
                let _ = w.eval(format!("window.location.href = {};", js_url));
                tokio::time::sleep(Duration::from_millis(1800)).await;
            }
            w
        }
        None => {
            open_browser_signin_window_with_visibility(
                origin,
                Some(site.clone()),
                false,
                &app_handle,
            )?;
            // Cold-creating a webview + first authenticated fetch takes
            // longer than navigating an existing one. 2.5s is conservative;
            // the probe still polls for up to 12s.
            tokio::time::sleep(Duration::from_millis(2500)).await;
            app_handle
                .get_webview_window("kim-browser-signin")
                .ok_or_else(|| "Failed to create provider webview".to_string())?
        }
    };

    let req_id = format!(
        "auth-{}-{}",
        site,
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    // F-D-4: inject the capability-scoped webview token (callback-only), NEVER
    // the full-capability `cfg.token`. A monkeypatched `fetch` on the provider
    // page can still capture whatever we inject, but the scoped token cannot
    // reach /v1/task or /v1/open.
    let probe_js = build_auth_probe_js(&site, &req_id, &cfg.base_url, &cfg.webview_token);
    webview
        .eval(&probe_js)
        .map_err(|e| format!("eval failed: {}", e))?;

    let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    for _ in 0..60 {
        tokio::time::sleep(Duration::from_millis(200)).await;
        if let Ok(mut guard) = store.lock() {
            if let Some(result) = guard.remove(&req_id) {
                drop(guard);
                return Ok(parse_auth_response(&site, &result));
            }
        }
    }
    // Timed out: report not-signed-in with an error so the UI shows the
    // re-sign-in affordance rather than blocking forever.
    // L-AUTH-1: drop our req_id from the shared results map — if the JS
    // callback lands after this timeout, the entry would otherwise sit in
    // WEBVIEW_BRIDGE_RESULTS forever (nobody collects auth-… ids twice).
    if let Ok(mut guard) = store.lock() {
        guard.remove(&req_id);
    }
    Ok(ProviderAuthStatus {
        provider: site,
        signed_in: false,
        email: None,
        name: None,
        avatar: None,
        error: Some("auth probe timed out".to_string()),
    })
}

pub(crate) fn post_signin_url_patterns(site: &str) -> Vec<&'static str> {
    match site {
        "chatgpt" | "openai" => vec!["chatgpt.com/c/", "chatgpt.com/?", "chatgpt.com/g/"],
        "gemini" | "google" => vec!["gemini.google.com/app"],
        "deepseek" => vec!["chat.deepseek.com/a", "chat.deepseek.com/?"],
        _ => vec![],
    }
}

pub(crate) fn spawn_post_signin_watcher(site: String, app_handle: tauri::AppHandle) {
    let label = "kim-browser-signin";
    let Some(window) = app_handle.get_webview_window(label) else {
        return;
    };
    let patterns: Vec<String> = post_signin_url_patterns(&site)
        .iter()
        .map(|s| s.to_string())
        .collect();
    if patterns.is_empty() {
        return;
    }

    let app = app_handle.clone();
    let window_clone = window.clone();
    let site_owned = site.clone();

    // Tauri 2 doesn't expose a stable per-window navigation listener on every
    // backend (WKWebView vs WebView2 differ), so polling window.url() every
    // 600ms is the portable approximation. The moment we see a post-login URL,
    // hide the window, refocus main, and emit `kim-auth-changed` so the React
    // side re-probes auth status.
    std::thread::spawn(move || {
        let start = std::time::Instant::now();
        let timeout = std::time::Duration::from_secs(300);
        loop {
            std::thread::sleep(std::time::Duration::from_millis(600));
            if start.elapsed() > timeout {
                return;
            }
            let url_str = window_clone
                .url()
                .map(|u| u.to_string())
                .unwrap_or_default();
            if patterns.iter().any(|p| url_str.contains(p)) {
                hide_browser_window_offscreen(&window_clone);
                if let Some(main_win) = app.get_webview_window("main") {
                    let _ = main_win.show();
                    let _ = main_win.set_focus();
                }
                let _ = app.emit("kim-auth-changed", site_owned.clone());
                return;
            }
        }
    });
}

#[tauri::command]
pub(crate) async fn provider_signin(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    let site = normalize_site(&provider);
    let login_url = provider_login_url(&site)
        .ok_or_else(|| format!("No login URL configured for provider: {}", provider))?;

    open_browser_signin_window_with_visibility(&login_url, Some(site.clone()), true, &app_handle)?;
    // Force-show even if the window already existed and was hidden.
    show_browser_window_impl(&app_handle);

    spawn_post_signin_watcher(site.clone(), app_handle.clone());
    Ok(format!("Opened sign-in for {}", site))
}

#[tauri::command]
pub(crate) async fn provider_signout(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    let site = normalize_site(&provider);
    let origin =
        provider_origin(&site).ok_or_else(|| format!("Unsupported provider: {}", provider))?;

    // Each provider has its own logout flow; navigating the webview to the
    // logout endpoint causes the page to clear its auth cookies in-place.
    //
    // #41: the Gemini logout hits Google's global `/Logout`, which clears ALL
    // Google sessions — but only inside the `kim-browser-signin` WKWebView's own
    // cookie jar (isolated from the user's Safari/Chrome). Gemini is currently
    // Kim's only Google-backed browser provider, so there is no other Google
    // provider to collaterally sign out. If a second Google provider is ever
    // added, scope this to a per-account sign-out instead of the global URL.
    let logout_url = match site.as_str() {
        "chatgpt" | "openai" => format!("{}/auth/logout", origin),
        "gemini" | "google" => "https://accounts.google.com/Logout".to_string(),
        _ => format!("{}/logout", origin),
    };

    if let Some(window) = app_handle.get_webview_window("kim-browser-signin") {
        let js = format!(
            "window.location.href = {};",
            serde_json::to_string(&logout_url).unwrap_or_else(|_| "\"about:blank\"".to_string())
        );
        let _ = window.eval(&js);
    }

    let _ = app_handle.emit("kim-auth-changed", site.clone());
    Ok(format!("Signed out of {}", site))
}

// ---------------------------------------------------------------------------
// Chrome auto-launch for browser provider CDP
// ---------------------------------------------------------------------------
