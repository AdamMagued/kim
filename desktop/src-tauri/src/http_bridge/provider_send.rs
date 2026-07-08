//! Prompt-injection routes for the in-app provider webview:
//! `/v1/open`, `/v1/complete` (legacy single-shot) and `/v1/send`
//! (split send half of the send/receive pair; results are collected by
//! `results::handle_bridge_result_request`).
//!
//! Extracted verbatim from the monolithic `http_bridge.rs` route match —
//! behavior unchanged.

use std::collections::HashMap;
use std::sync::atomic::Ordering;
use std::sync::{Condvar, Mutex as StdMutex};
use std::time::{Duration, Instant};

use tauri::Manager;
use tiny_http::Request;

use crate::browser_bridge::{
    open_browser_signin_window_for_bridge_send, open_browser_signin_window_impl,
    run_bridge_completion_once,
};
use crate::http_util::{agent_debug_log, respond_json};
use crate::provider_url::{default_site_url, gemini_site_url, normalize_site};
use crate::{
    clear_provider_webview_chat, hide_browser_window_offscreen, is_browser_window_offscreen,
    prepare_gemini_webview, save_frontmost_app, schedule_frontmost_restore,
    should_keep_browser_visible, show_browser_window_impl, write_first_png_to_clipboard,
    write_text_prompt_to_clipboard, BridgeCompleteRequest, BridgeCompleteResponse,
    BridgeOpenRequest, WEBVIEW_BRIDGE_CFG, WEBVIEW_BRIDGE_LOCK, WEBVIEW_BRIDGE_NOTIFY,
    WEBVIEW_BRIDGE_REQ_COUNTER, WEBVIEW_BRIDGE_RESULTS, WEBVIEW_WAS_HIDDEN,
};

use super::read_body_capped;

/// `POST /v1/open` — open the provider sign-in window at a caller-supplied URL.
pub(super) fn open(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    let parsed: BridgeOpenRequest = match serde_json::from_str(&body) {
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

    match open_browser_signin_window_impl(&parsed.url, parsed.provider_name, &app_handle) {
        Ok(msg) => respond_json(
            request,
            200,
            serde_json::json!({"ok": true, "message": msg}),
        ),
        // L-BRIDGE-13: failures here are caused by the caller's input
        // (empty/invalid/non-http URL) or a busy task — client errors,
        // not server faults.
        Err(e) => respond_json(request, 400, serde_json::json!({"ok": false, "error": e})),
    }
}

/// `POST /v1/complete` — legacy single-shot prompt→response round trip.
pub(super) fn complete(mut request: Request, app_handle: tauri::AppHandle, token: String) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    let parsed: BridgeCompleteRequest = match serde_json::from_str(&body) {
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

    if parsed.prompt.trim().is_empty() {
        respond_json(
            request,
            400,
            serde_json::json!({"ok": false, "error": "Prompt cannot be empty."}),
        );
        return;
    }

    let site = normalize_site(parsed.site.as_deref().unwrap_or("claude"));
    let gemini_authuser = if site == "gemini" {
        parsed.authuser
    } else {
        None
    };
    let bridge_lock = WEBVIEW_BRIDGE_LOCK.get_or_init(|| StdMutex::new(()));
    let _guard = match bridge_lock.try_lock() {
        Ok(g) => g,
        Err(std::sync::TryLockError::WouldBlock) => {
            respond_json(
                request,
                429,
                serde_json::json!({
                    "ok": false,
                    "error": "In-app browser bridge is busy with another request. Retry in a moment.",
                }),
            );
            return;
        }
        Err(std::sync::TryLockError::Poisoned(_)) => {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": "Bridge lock poisoned."}),
            );
            return;
        }
    };

    let opened_window = false;
    let window = if let Some(w) = app_handle.get_webview_window("kim-browser-signin") {
        w
    } else {
        let open_url = if site == "gemini" {
            gemini_site_url(gemini_authuser)
        } else {
            default_site_url(&site).to_string()
        };
        let open_result =
            open_browser_signin_window_for_bridge_send(&open_url, Some(site.clone()), &app_handle);
        if let Err(e) = open_result {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": format!("Could not open in-app browser window: {}", e)}),
            );
            return;
        }
        show_browser_window_impl(&app_handle);
        respond_json(
            request,
            409,
            serde_json::json!({
                "ok": false,
                "error": "Provider browser window opened. Sign in to the provider, then resend your task.",
            }),
        );
        return;
    };

    if site == "gemini" {
        prepare_gemini_webview(&window, gemini_authuser, opened_window);
    }

    if parsed.clear_chat {
        agent_debug_log(
            "H1",
            "clear_chat requested for /v1/complete",
            serde_json::json!({
                "site": site.clone(),
            }),
        );
        if let Err(e) = clear_provider_webview_chat(&window, &site, gemini_authuser) {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": format!("Could not clear provider chat: {}", e)}),
            );
            return;
        }
    }

    let callback_url = WEBVIEW_BRIDGE_CFG
        .get()
        .map(|cfg| format!("{}/v1/callback", cfg.base_url))
        .unwrap_or_else(|| "http://127.0.0.1:18991/v1/callback".to_string());

    // M-BRIDGE-6: /v1/complete must stage the same environment as the
    // split /v1/send path. Without this, an image request fired the
    // native_paste Cmd+V against the user's STALE clipboard into
    // whatever app was frontmost, and long text prompts were never
    // staged for the paste-verify flow.
    let has_image_attachment = parsed
        .attachments
        .iter()
        .any(|a| a.mime_type == "image/png");
    if !parsed.attachments.is_empty() {
        let _clip_ok = write_first_png_to_clipboard(&parsed.attachments);
        agent_debug_log(
            "H1",
            "complete: clipboard write",
            serde_json::json!({ "ok": _clip_ok }),
        );
    } else {
        let _clip_ok = write_text_prompt_to_clipboard(&parsed.prompt);
        agent_debug_log(
            "H1",
            "complete: prompt clipboard write",
            serde_json::json!({ "ok": _clip_ok, "promptLen": parsed.prompt.len() }),
        );
    }

    // The window stays offscreen (1x1 at -10000,-10000) during
    // headless operation.  JS keeps running at 1x1 size, so we
    // do NOT need to show it to the user. Image sends need the
    // webview visible+key so the trusted Cmd+V lands in its editor.
    if has_image_attachment {
        show_browser_window_impl(&app_handle);
        let _ = window.set_focus();
    } else if should_keep_browser_visible() {
        show_browser_window_impl(&app_handle);
    } else if !is_browser_window_offscreen(&window) {
        // Legacy /v1/complete fallback must follow the same rule as
        // split /v1/send: normal provider sends should not take focus
        // or cover the user's target app.
        hide_browser_window_offscreen(&window);
    }

    // Snapshot + restore the user's frontmost app around the prompt
    // injection, exactly like /v1/send (skip for image sends, which
    // need the webview to stay key for the trusted paste).
    let saved_frontmost = if has_image_attachment {
        None
    } else {
        save_frontmost_app()
    };
    if let Some(app) = saved_frontmost {
        schedule_frontmost_restore(app);
    }

    let mut completion = run_bridge_completion_once(
        &window,
        &site,
        &parsed.prompt,
        &parsed.attachments,
        &callback_url,
        token.as_str(),
        parsed.completion_hash.as_deref(),
        parsed.model_tier.as_deref(),
    );

    let needs_nav_retry = match &completion {
        Ok(payload) => {
            let err = payload.error.clone().unwrap_or_default().to_lowercase();
            !payload.ok && err.contains("could not find input selector")
        }
        Err(err) => err.to_lowercase().contains("could not find input selector"),
    };

    if needs_nav_retry {
        completion = Ok(BridgeCompleteResponse {
            ok: false,
            response: None,
            error: Some(
                "Could not find the provider input box in the existing chat. \
                 Kim will not navigate to a new provider page because that would lose task context. \
                 Show the browser window, make sure the current chat is open, then resend."
                    .to_string(),
            ),
            site: Some(site.clone()),
            attachments_uploaded: None,
        });
    }

    // Window was never shown, no need to re-hide.

    match completion {
        Ok(payload) => {
            if payload.ok {
                respond_json(
                    request,
                    200,
                    serde_json::to_value(payload).unwrap_or_else(
                        |_| serde_json::json!({"ok": false, "error": "Serialization error"}),
                    ),
                );
            } else {
                respond_json(
                    request,
                    502,
                    serde_json::to_value(payload).unwrap_or_else(
                        |_| serde_json::json!({"ok": false, "error": "Serialization error"}),
                    ),
                );
            }
        }
        Err(e) => respond_json(
            request,
            504,
            serde_json::json!({"ok": false, "error": e, "site": site}),
        ),
    }
}

// ── Split send/receive: /v1/send ────────────────────────────────
// Injects the prompt and returns immediately with a req_id.
// Python can then long-poll /v1/result/{reqId} for the actual response.
pub(super) fn send(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    let parsed: BridgeCompleteRequest = match serde_json::from_str(&body) {
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

    if parsed.prompt.trim().is_empty() {
        respond_json(
            request,
            400,
            serde_json::json!({"ok": false, "error": "Prompt cannot be empty."}),
        );
        return;
    }

    let site = normalize_site(parsed.site.as_deref().unwrap_or("claude"));
    let gemini_authuser = if site == "gemini" {
        parsed.authuser
    } else {
        None
    };

    // Acquire lock early to prevent overlapping sends from clobbering
    // clipboard, window state, or req_id stores (#33, #34)
    let _bridge_guard = match WEBVIEW_BRIDGE_LOCK
        .get_or_init(|| StdMutex::new(()))
        .try_lock()
    {
        Ok(g) => g,
        Err(std::sync::TryLockError::WouldBlock) => {
            respond_json(
                request,
                429,
                serde_json::json!({"ok": false, "error": "bridge busy"}),
            );
            return;
        }
        Err(std::sync::TryLockError::Poisoned(_)) => {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": "lock poisoned"}),
            );
            return;
        }
    };

    let opened_window = false;
    let window = if let Some(w) = app_handle.get_webview_window("kim-browser-signin") {
        w
    } else {
        let open_url = if site == "gemini" {
            gemini_site_url(gemini_authuser)
        } else {
            default_site_url(&site).to_string()
        };
        let open_result =
            open_browser_signin_window_for_bridge_send(&open_url, Some(site.clone()), &app_handle);
        if let Err(e) = open_result {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": format!("Could not open browser: {}", e)}),
            );
            return;
        }
        show_browser_window_impl(&app_handle);
        respond_json(
            request,
            409,
            serde_json::json!({
                "ok": false,
                "error": "Provider browser window opened. Sign in to the provider, then resend your task.",
            }),
        );
        return;
    };

    if site == "gemini" {
        prepare_gemini_webview(&window, gemini_authuser, opened_window);
    }

    if parsed.clear_chat {
        agent_debug_log(
            "H1",
            "clear_chat requested for /v1/send",
            serde_json::json!({
                "site": site.clone(),
            }),
        );
        if let Err(e) = clear_provider_webview_chat(&window, &site, gemini_authuser) {
            respond_json(
                request,
                500,
                serde_json::json!({"ok": false, "error": format!("Could not clear provider chat: {}", e)}),
            );
            return;
        }
    }

    if should_keep_browser_visible() {
        show_browser_window_impl(&app_handle);
    }

    // Generate req_id
    let req_id = format!(
        "r-{}-{}",
        std::process::id(),
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );

    // Clear old req_id and sentinel marker
    if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
        guard.remove(&format!("{}_sent", req_id));
    }

    let attachments = &parsed.attachments;
    let prompt_json = serde_json::to_string(&parsed.prompt).unwrap_or_else(|_| "\"\"".to_string());
    let req_id_json = serde_json::to_string(&req_id).unwrap_or_else(|_| "\"\"".to_string());
    let site_json = serde_json::to_string(&site).unwrap_or_else(|_| "\"\"".to_string());
    let attachments_json =
        serde_json::to_string(&attachments).unwrap_or_else(|_| "\"[]\"".to_string());
    let hash_json =
        serde_json::to_string(&parsed.completion_hash).unwrap_or_else(|_| "null".to_string());
    let tier_json =
        serde_json::to_string(&parsed.model_tier).unwrap_or_else(|_| "null".to_string());

    let bridge_call = format!(
        r#"(() => {{
            if (window.__kimBridge && window.__kimBridge._v >= 2) {{
                window.__kimBridge.send({prompt}, {req_id}, {site}, {attachments}, null, {hash}, {tier});
            }} else {{
                document.title = '__KIMBRIDGE_NO_PERSISTENT__';
            }}
        }})()"#,
        prompt = prompt_json,
        req_id = req_id_json,
        site = site_json,
        attachments = attachments_json,
        hash = hash_json,
        tier = tier_json,
    );

    // An image (screenshot) is pasted into the chat with a REAL Cmd+V
    // (native_paste) — a trusted paste the editor accepts. That keystroke
    // only lands if the provider webview is visible, frontmost and key, so
    // for image sends we show + focus it instead of hiding it offscreen. The
    // screenshot was already captured before this send, so showing the window
    // now does not interfere with screen-capture tools.
    // Only PNG drives the native-paste path: write_first_png_to_clipboard stages
    // only PNG, so a non-PNG image here would fire Cmd+V against a STALE clipboard
    // (pasting whatever the user had copied). Non-PNG images use the file-input /
    // synthetic-paste strategies instead. Screenshots are always PNG.
    let has_image_attachment = attachments.iter().any(|a| a.mime_type == "image/png");

    // The window stays offscreen (1x1 at -10000,-10000) during
    // headless operation.  JS keeps running at 1x1, no need to
    // show it to the user.
    if has_image_attachment {
        show_browser_window_impl(&app_handle);
        let _ = window.set_focus();
    } else if should_keep_browser_visible() {
        show_browser_window_impl(&app_handle);
    } else if is_browser_window_offscreen(&window) {
        if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN
            .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
            .lock()
        {
            guard.insert(req_id.clone());
        }
    } else {
        // Keep the provider webview off-screen during normal sends so it
        // does not cover the user's target app before screenshot tools run.
        hide_browser_window_offscreen(&window);
        if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN
            .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
            .lock()
        {
            guard.insert(req_id.clone());
        }
    }

    agent_debug_log(
        "H1",
        "send via persistent bridge",
        serde_json::json!({
            "reqId": req_id,
            "site": site.clone(),
            "promptLen": parsed.prompt.len(),
            "attachments": attachments.len(),
        }),
    );

    // Best-effort: pre-populate the macOS system clipboard with the first PNG
    // attachment so the bridge JS can use document.execCommand('paste') to
    // generate a TRUSTED paste event that Gemini's editor will accept.
    // This runs BEFORE window.eval so the clipboard is ready when the async
    // JS bridge calls injectAttachments → execCommand('paste').
    if !attachments.is_empty() {
        let _clip_ok = write_first_png_to_clipboard(attachments);
        agent_debug_log(
            "H1",
            "clipboard write",
            serde_json::json!({ "ok": _clip_ok }),
        );
    } else {
        // For text-only sends, stage the full prompt through a temp file
        // and copy it to the system clipboard before the browser bridge
        // runs. The injected JS pastes once, verifies the full text, and
        // refuses to send if the provider editor only accepted part of it.
        let _clip_ok = write_text_prompt_to_clipboard(&parsed.prompt);
        agent_debug_log(
            "H1",
            "prompt clipboard write",
            serde_json::json!({ "ok": _clip_ok, "promptLen": parsed.prompt.len() }),
        );
    }

    // Lock already acquired at the top of /v1/send handler (#33)

    // Snapshot the user's frontmost app BEFORE the JS eval so we can
    // restore it after the offscreen webview steals key-window status
    // during inputEl.focus() / send-button click. Without this, Stage
    // Manager swaps groups and Kim's window comes forward, breaking
    // observe_ui on the actual target app.
    // For image sends, keep the provider webview frontmost & key so the
    // native_paste Cmd+V lands in its focused editor — do NOT restore the
    // user's previous frontmost app until the paste is done.
    let saved_frontmost = if has_image_attachment {
        None
    } else {
        save_frontmost_app()
    };

    if let Err(e) = window.eval(&bridge_call) {
        respond_json(
            request,
            500,
            serde_json::json!({"ok": false, "error": format!("Eval failed: {}", e)}),
        );
        return;
    }

    if let Some(app) = saved_frontmost {
        schedule_frontmost_restore(app);
    }

    // Fallback for NO_PERSISTENT
    std::thread::sleep(Duration::from_millis(100));
    let title_check = window.title().unwrap_or_default();
    if title_check == "__KIMBRIDGE_NO_PERSISTENT__" {
        let _ = window.eval("document.title = '';");
        respond_json(
            request,
            500,
            serde_json::json!({"ok": false, "error": "Persistent bridge not installed. Ensure page is loaded."}),
        );
        return;
    }

    // Wait briefly for the "sent" confirmation from the persistent bridge
    let mut sent_confirmed = false;
    let start = Instant::now();
    let timeout = Duration::from_secs(5);

    let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    let (notify_lock, condvar) =
        WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| (StdMutex::new(()), Condvar::new()));

    if let Ok(mut guard) = notify_lock.lock() {
        while start.elapsed() < timeout {
            let mut found = false;
            if let Ok(store_guard) = store.lock() {
                if store_guard.contains_key(&format!("{}_sent", req_id)) {
                    found = true;
                }
            }
            if found {
                sent_confirmed = true;
                break;
            }
            let result = condvar.wait_timeout(guard, Duration::from_millis(100));
            if let Ok((new_guard, _)) = result {
                guard = new_guard;
            } else {
                break;
            }
        }
    }

    respond_json(
        request,
        200,
        serde_json::json!({
            "ok": true,
            "req_id": req_id,
            "sent_confirmed": sent_confirmed,
            "site": site.clone(),
        }),
    );
}
