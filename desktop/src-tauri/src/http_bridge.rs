use crate::*;
use tiny_http::{Method, Request, Server};
use tauri::Manager;
use std::time::Duration;

fn handle_webview_bridge_request(
    mut request: Request,
    app_handle: tauri::AppHandle,
    token: String,
) {
    let method = request.method().clone();
    let path = request
        .url()
        .split('?')
        .next()
        .unwrap_or("/")
        .to_string();

    if method == Method::Options {
        respond_json(request, 204, serde_json::json!({"ok": true}));
        return;
    }

    if !(method == Method::Get && (path == "/v1/health" || path == "/v1/status")) {
        let auth = header_value(&request, "X-Kim-Token");
        if auth.as_deref() != Some(token.as_str()) {
            respond_json(
                request,
                401,
                serde_json::json!({"ok": false, "error": "Unauthorized bridge token."}),
            );
            return;
        }
    }

    // Handle /v1/result/{reqId} before the match, since it has a dynamic path
    // and we can't use it in a match arm without consuming `method`.
    if method == Method::Get && path.starts_with("/v1/result/") {
        handle_bridge_result_request(request, &path, &token, app_handle.clone());
        return;
    }

    match (method, path.as_str()) {
        (Method::Get, "/v1/health") => {
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/hide") => {
            if let Some(win) = app_handle.get_webview_window("main") {
                let _ = win.hide();
            }
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/show") => {
            if let Some(win) = app_handle.get_webview_window("main") {
                let _ = win.show();
                let _ = win.set_focus();
            }
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/open") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(
                    request,
                    400,
                    serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}),
                );
                return;
            }

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
                Ok(msg) => respond_json(request, 200, serde_json::json!({"ok": true, "message": msg})),
                Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": e})),
            }
        }
        (Method::Post, "/v1/callback") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(
                    request,
                    400,
                    serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}),
                );
                return;
            }

            let parsed: BridgeCallbackRequest = match serde_json::from_str(&body) {
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

            agent_debug_log(
                "H2",
                "callback received",
                serde_json::json!({
                    "reqId": parsed.req_id,
                    "ok": parsed.payload.ok,
                    "hasResponse": parsed.payload.response.as_ref().map(|s| !s.is_empty()).unwrap_or(false),
                    "error": parsed.payload.error,
                }),
            );

            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            match store.lock() {
                Ok(mut guard) => {
                    guard.insert(parsed.req_id.clone(), parsed.payload);
                }
                Err(_) => {
                    respond_json(
                        request,
                        500,
                        serde_json::json!({"ok": false, "error": "Bridge results lock poisoned."}),
                    );
                    return;
                }
            }
            // Wake up any Condvar-waiting collector.
            notify_bridge_result();

            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Get, path) if path.starts_with("/v1/ping") => {
            let full_uri = request.url().to_string();
            let url = match tauri::Url::parse(&format!("http://localhost{}", full_uri)) {
                Ok(u) => u,
                Err(e) => {
                    respond_json(
                        request,
                        400,
                        serde_json::json!({"ok": false, "error": format!("Invalid ping URI: {}", e)}),
                    );
                    return;
                }
            };
            let mut req_id = String::new();
            let mut payload_str = String::new();
            for (key, value) in url.query_pairs() {
                if key == "req_id" {
                    req_id = value.into_owned();
                } else if key == "data" {
                    payload_str = value.into_owned();
                }
            }

            agent_debug_log(
                "H2",
                "ping received",
                serde_json::json!({
                    "reqId": req_id,
                    "payloadStrLen": payload_str.len(),
                }),
            );

            if !req_id.is_empty() && !payload_str.is_empty() {
                match base64::engine::general_purpose::STANDARD.decode(&payload_str) {
                    Ok(decoded) => {
                        match String::from_utf8(decoded) {
                            Ok(json_str) => {
                                if let Ok(ipc_event) = serde_json::from_str::<BridgeIpcEvent>(&json_str) {
                                    handle_bridge_ipc_event(ipc_event, &app_handle);
                                } else {
                                    match serde_json::from_str::<BridgeCompleteResponse>(&json_str) {
                                        Ok(payload) => {
                                            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
                                            if let Ok(mut guard) = store.lock() {
                                                guard.insert(req_id, payload);
                                            }
                                            notify_bridge_result();
                                        }
                                        Err(e) => {
                                            agent_debug_log("H2", "ping json parse failed", serde_json::json!({ "error": e.to_string(), "json_str": json_str }));
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                agent_debug_log("H2", "ping utf8 parse failed", serde_json::json!({ "error": e.to_string() }));
                            }
                        }
                    }
                    Err(e) => {
                        agent_debug_log("H2", "ping base64 parse failed", serde_json::json!({ "error": e.to_string(), "payload_str": payload_str }));
                    }
                }
            }
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/complete") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(
                    request,
                    400,
                    serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}),
                );
                return;
            }

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
            let gemini_authuser = if site == "gemini" { parsed.authuser } else { None };
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
                let open_result = open_browser_signin_window_impl(&open_url, Some(site.clone()), &app_handle);
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
                agent_debug_log("H1", "clear_chat requested for /v1/complete", serde_json::json!({
                    "site": site.clone(),
                }));
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

            // The window stays offscreen (1x1 at -10000,-10000) during
            // headless operation.  JS keeps running at 1x1 size, so we
            // do NOT need to show it to the user.
            if should_keep_browser_visible() {
                show_browser_window_impl(&app_handle);
            } else if !is_browser_window_offscreen(&window) {
                // Legacy /v1/complete fallback must follow the same rule as
                // split /v1/send: normal provider sends should not take focus
                // or cover the user's target app.
                hide_browser_window_offscreen(&window);
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
                });
            }

            // Window was never shown, no need to re-hide.

            match completion {
                Ok(payload) => {
                    if payload.ok {
                        respond_json(
                            request,
                            200,
                            serde_json::to_value(payload).unwrap_or_else(|_| serde_json::json!({"ok": false, "error": "Serialization error"})),
                        );
                    } else {
                        respond_json(
                            request,
                            502,
                            serde_json::to_value(payload).unwrap_or_else(|_| serde_json::json!({"ok": false, "error": "Serialization error"})),
                        );
                    }
                }
                Err(e) => respond_json(request, 504, serde_json::json!({"ok": false, "error": e, "site": site})),
            }
        }
        // ── Split send/receive: /v1/send ────────────────────────────────
        // Injects the prompt and returns immediately with a req_id.
        // Python can then long-poll /v1/result/{reqId} for the actual response.
        (Method::Post, "/v1/send") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(
                    request,
                    400,
                    serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}),
                );
                return;
            }

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
            let gemini_authuser = if site == "gemini" { parsed.authuser } else { None };

            // Acquire lock early to prevent overlapping sends from clobbering
            // clipboard, window state, or req_id stores (#33, #34)
            let _bridge_guard = match WEBVIEW_BRIDGE_LOCK.get_or_init(|| StdMutex::new(())).try_lock() {
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
                let open_result = open_browser_signin_window_impl(&open_url, Some(site.clone()), &app_handle);
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
                agent_debug_log("H1", "clear_chat requested for /v1/send", serde_json::json!({
                    "site": site.clone(),
                }));
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
            let attachments_json = serde_json::to_string(&attachments).unwrap_or_else(|_| "\"[]\"".to_string());
            let hash_json = serde_json::to_string(&parsed.completion_hash).unwrap_or_else(|_| "null".to_string());
            let tier_json = serde_json::to_string(&parsed.model_tier).unwrap_or_else(|_| "null".to_string());

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
                if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                    guard.insert(req_id.clone());
                }
            } else {
                // Keep the provider webview off-screen during normal sends so it
                // does not cover the user's target app before screenshot tools run.
                hide_browser_window_offscreen(&window);
                if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                    guard.insert(req_id.clone());
                }
            }

            agent_debug_log("H1", "send via persistent bridge", serde_json::json!({
                "reqId": req_id,
                "site": site.clone(),
                "promptLen": parsed.prompt.len(),
                "attachments": attachments.len(),
            }));

            // Best-effort: pre-populate the macOS system clipboard with the first PNG
            // attachment so the bridge JS can use document.execCommand('paste') to
            // generate a TRUSTED paste event that Gemini's editor will accept.
            // This runs BEFORE window.eval so the clipboard is ready when the async
            // JS bridge calls injectAttachments → execCommand('paste').
            if !attachments.is_empty() {
                let _clip_ok = write_first_png_to_clipboard(attachments);
                agent_debug_log("H1", "clipboard write", serde_json::json!({ "ok": _clip_ok }));
            } else {
                // For text-only sends, stage the full prompt through a temp file
                // and copy it to the system clipboard before the browser bridge
                // runs. The injected JS pastes once, verifies the full text, and
                // refuses to send if the provider editor only accepted part of it.
                let _clip_ok = write_text_prompt_to_clipboard(&parsed.prompt);
                agent_debug_log("H1", "prompt clipboard write", serde_json::json!({ "ok": _clip_ok, "promptLen": parsed.prompt.len() }));
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
            let saved_frontmost = if has_image_attachment { None } else { save_frontmost_app() };

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
            let (notify_lock, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
                (StdMutex::new(()), Condvar::new())
            });

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
        // ---------------------------------------------------------------
        // kimctl routes
        // ---------------------------------------------------------------
        (Method::Get, "/v1/status") => {
            let has_running_task = {
                let store = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None));
                if let Ok(guard) = store.lock() {
                    guard.map(process_exists).unwrap_or(false)
                } else {
                    false
                }
            };
            let active_session_id = {
                let store = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None));
                if let Ok(guard) = store.lock() {
                    guard.clone()
                } else {
                    None
                }
            };
            let browser_visible = app_handle
                .get_webview_window("kim-browser-signin")
                .map(|w| w.is_visible().unwrap_or(false))
                .unwrap_or(false);
            respond_json(request, 200, serde_json::json!({
                "ok": true,
                "has_running_task": has_running_task,
                "active_session_id": active_session_id,
                "browser_visible": browser_visible,
            }));
        }
        (Method::Get, "/v1/browser/current-url") => {
            let current_url = app_handle
                .get_webview_window("kim-browser-signin")
                .map(|w| webview_current_href(&w))
                .filter(|u| !u.trim().is_empty());
            respond_json(request, 200, serde_json::json!({
                "ok": true,
                "url": current_url,
            }));
        }
        (Method::Get, "/v1/browser/meta") => {
            let raw_url = request.url().to_string();
            let Some(session_id) = query_param(&raw_url, "session_id") else {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": "session_id is required."}));
                return;
            };
            if let Err(e) = validate_session_id(&session_id) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }
            let session_type = query_param(&raw_url, "session_type").unwrap_or_else(|| "kim".to_string());
            let session_date = query_param(&raw_url, "session_date");
            let kim_dir = query_param(&raw_url, "kim_dir");
            let codex_dir = query_param(&raw_url, "codex_dir");
            let base = session_base_dir(&session_type, kim_dir, codex_dir);
            match resolve_session_date_dir(&base, &session_id, session_date.as_deref()) {
                Ok(date_dir) => {
                    let meta = read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();
                    respond_json(request, 200, serde_json::json!({"ok": true, "meta": meta}));
                }
                Err(e) => respond_json(request, 400, serde_json::json!({"ok": false, "error": e})),
            }
        }
        (Method::Post, "/v1/browser/meta") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct BrowserMetaWriteRequest {
                session_id: String,
                #[serde(default)]
                session_date: Option<String>,
                #[serde(default)]
                session_type: Option<String>,
                #[serde(default)]
                site: Option<String>,
                #[serde(default)]
                url: Option<String>,
                #[serde(default)]
                browser_last_site: Option<String>,
                #[serde(default)]
                last_llm_provider: Option<String>,
                #[serde(default)]
                kim_dir: Option<String>,
                #[serde(default)]
                codex_dir: Option<String>,
            }

            let parsed: BrowserMetaWriteRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };
            if let Err(e) = validate_session_id(&parsed.session_id) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }

            let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
            let date_dir = match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                    return;
                }
            };
            let mut meta = read_browser_session_meta_from_dir(&date_dir, &parsed.session_id).unwrap_or_default();

            if let Err(e) = apply_browser_meta_writes(
                &mut meta,
                parsed.browser_last_site.clone(),
                parsed.site.clone(),
                parsed.url.clone(),
                parsed.last_llm_provider.clone(),
            ) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }

            match write_browser_session_meta_to_dir(&date_dir, &parsed.session_id, &meta) {
                Ok(()) => respond_json(request, 200, serde_json::json!({"ok": true, "meta": meta})),
                Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": e})),
            }
        }
        (Method::Post, "/v1/browser/commit-url") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct BrowserCommitRequest {
                session_id: String,
                #[serde(default)]
                session_date: Option<String>,
                #[serde(default)]
                session_type: Option<String>,
                #[serde(default)]
                preferred_site: Option<String>,
                #[serde(default)]
                kim_dir: Option<String>,
                #[serde(default)]
                codex_dir: Option<String>,
            }

            let parsed: BrowserCommitRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };
            if let Err(e) = validate_session_id(&parsed.session_id) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }

            let Some(win) = app_handle.get_webview_window("kim-browser-signin") else {
                respond_json(request, 200, serde_json::json!({"ok": true, "committed": false, "reason": "no_browser_window"}));
                return;
            };
            let current_url = webview_current_href(&win);
            let site = parsed.preferred_site
                .as_deref()
                .map(normalize_site)
                .filter(|s| !s.is_empty())
                .or_else(|| browser_url_site(&current_url))
                .unwrap_or_else(|| "claude".to_string());

            let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
            let date_dir = match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                    return;
                }
            };
            let mut meta = read_browser_session_meta_from_dir(&date_dir, &parsed.session_id).unwrap_or_default();

            if browser_url_is_bad_for_commit(&current_url, &site) {
                // Preserve any useful previous URL; only update the last-site hint.
                meta.browser_last_site = Some(site);
                meta.browser_threads_updated_at_ms = Some(now_ms());
                let _ = write_browser_session_meta_to_dir(&date_dir, &parsed.session_id, &meta);
                respond_json(request, 200, serde_json::json!({"ok": true, "committed": false, "reason": "ignored_bad_url", "meta": meta}));
                return;
            }

            meta.browser_threads.insert(site.clone(), current_url);
            meta.browser_last_site = Some(site);
            meta.browser_threads_updated_at_ms = Some(now_ms());
            match write_browser_session_meta_to_dir(&date_dir, &parsed.session_id, &meta) {
                Ok(()) => respond_json(request, 200, serde_json::json!({"ok": true, "committed": true, "meta": meta})),
                Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": e})),
            }
        }
        (Method::Post, "/v1/browser/restore") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct BrowserRestoreRequest {
                session_id: String,
                #[serde(default)]
                session_date: Option<String>,
                #[serde(default)]
                session_type: Option<String>,
                #[serde(default)]
                preferred_site: Option<String>,
                #[serde(default)]
                kim_dir: Option<String>,
                #[serde(default)]
                codex_dir: Option<String>,
            }

            let parsed: BrowserRestoreRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };
            if let Err(e) = validate_session_id(&parsed.session_id) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                return;
            }
            if is_bridge_task_running() {
                respond_json(request, 409, serde_json::json!({
                    "ok": false,
                    "error": "Cannot restore provider browser while Kim is running a task.",
                }));
                return;
            }

            let stype = parsed.session_type.unwrap_or_else(|| "kim".to_string());
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.codex_dir);
            let date_dir = match resolve_session_date_dir(&base, &parsed.session_id, parsed.session_date.as_deref()) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
                    return;
                }
            };
            let meta = read_browser_session_meta_from_dir(&date_dir, &parsed.session_id).unwrap_or_default();
            let site = parsed.preferred_site
                .as_deref()
                .map(normalize_site)
                .filter(|s| !s.is_empty())
                .or(meta.browser_last_site.clone())
                .unwrap_or_else(|| "claude".to_string());

            let mut restored = false;
            let reason;
            let target = if let Some(saved) = meta.browser_threads.get(&site) {
                if browser_url_allowed_for_restore(saved, &site) {
                    restored = true;
                    reason = "stored_thread".to_string();
                    saved.clone()
                } else {
                    reason = "stored_url_rejected".to_string();
                    fresh_site_url(&site, None)
                }
            } else {
                reason = "no_stored_url".to_string();
                fresh_site_url(&site, None)
            };

            let provider_name = Some(format!("{} (session)", capitalize(&site)));
            // Match the Tauri-command path: chat-select restores happen invisibly.
            match open_browser_signin_window_with_visibility(&target, provider_name, false, &app_handle) {
                Ok(_) => {
                    let message = if restored {
                        "Restored the saved browser conversation for this session."
                    } else if reason == "stored_url_rejected" {
                        "Saved browser URL was not safe/valid; opened a fresh provider page."
                    } else {
                        "No saved browser conversation for this provider; opened the provider start page."
                    };
                    respond_json(request, 200, serde_json::json!({
                        "ok": true,
                        "result": BrowserRestoreResult {
                            restored,
                            site,
                            url: target,
                            reason,
                            message: Some(message.to_string()),
                        },
                    }));
                }
                Err(e) => respond_json(request, 500, serde_json::json!({"ok": false, "error": format!("{}", e)})),
            }
        }
        (Method::Post, "/v1/task") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct TaskRequest {
                task: String,
                #[serde(default)]
                session_id: Option<String>,
                #[serde(default)]
                provider: Option<String>,
            }

            let parsed: TaskRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };

            if parsed.task.trim().is_empty() {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": "Task cannot be empty."}));
                return;
            }

            // Reject if a task is already running
            {
                let store = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None));
                if let Ok(guard) = store.lock() {
                    if let Some(pid) = *guard {
                        if process_exists(pid) {
                            respond_json(request, 409, serde_json::json!({
                                "ok": false,
                                "error": "A task is already running. Cancel it first.",
                            }));
                            return;
                        }
                    }
                }
            }

            let kim_root = default_project_root();
            let python = match find_python_interpreter(&kim_root) {
                Ok(p) => p,
                Err(e) => {
                    respond_json(request, 500, serde_json::json!({"ok": false, "error": e}));
                    return;
                }
            };

            let session_id = parsed.session_id
                .filter(|s| !s.trim().is_empty())
                .unwrap_or_else(|| {
                    let ts = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as u64;
                    let counter = WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed);
                    format!("{:04x}{:04x}", (ts & 0xFFFF) as u16, (counter & 0xFFFF) as u16)
                });
            let session_dir = kim_root.join("kim_sessions");
            let provider = parsed.provider
                .filter(|s| !s.trim().is_empty() && s != "desktop")
                .unwrap_or_else(|| "browser".to_string());

            let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();

            let mut cmd = std::process::Command::new(&python);
            // When the resolved interpreter is the bundled sidecar it's a standalone
            // executable, not a Python binary — invoke it directly (matches the canonical
            // spawn in subprocess.rs). Otherwise `<python> -m orchestrator.agent`.
            if !crate::subprocess::is_bundled_orchestrator(&python) {
                cmd.args(["-m", "orchestrator.agent"]);
            }
            cmd.arg("--task")
                .arg(&parsed.task)
                .arg("--session-dir")
                .arg(session_dir.to_string_lossy().to_string())
                .arg("--resume")
                .arg(&session_id)
                .arg("--provider")
                .arg(&provider)
                .current_dir(&kim_root)
                .env("PROJECT_ROOT", kim_root.to_str().unwrap_or(""))
                .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::inherit());
            // Own process group so a later /v1/cancel (kill -TERM -<pid>) reaps the whole
            // process tree (MCP server, browser/Playwright helpers), not just the parent
            // Python — mirrors subprocess.rs. Without this the children are orphaned.
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                cmd.process_group(0);
            }

            if let Ok(guard) = KIM_PREFERRED_SITE.get_or_init(|| StdMutex::new(None)).lock() {
                if let Some(site) = &*guard {
                    cmd.env("KIM_PREFERRED_SITE", site);
                }
            }

            if let Some(cfg) = &bridge_cfg {
                cmd.env("KIM_WEBVIEW_BRIDGE_URL", &cfg.base_url)
                    .env("KIM_WEBVIEW_BRIDGE_TOKEN", &cfg.token)
                    .env("KIM_WEBVIEW_WINDOW_LABEL", "kim-browser-signin");
            }

            if provider == "browser" || provider.starts_with("browser:") {
                let restore_status = browser_restore_status_for_session(
                    &session_dir,
                    Some(&session_id),
                    &provider,
                );
                cmd.env("KIM_BROWSER_RESTORE_STATUS", restore_status);
            }

            if provider.trim().eq_ignore_ascii_case("gemini") {
                let google_env = match tauri::async_runtime::block_on(google_oauth::google_oauth_env_for_agent()) {
                    Ok(value) => value,
                    Err(err) => {
                        respond_json(request, 400, serde_json::json!({
                            "ok": false,
                            "error": format!(
                                "Google for Kim is not connected. Open Settings → Account → Google for Kim (API), then Continue with Google. {}",
                                err
                            ),
                        }));
                        return;
                    }
                };
                for (key, value) in google_env.as_env_pairs() {
                    cmd.env(key, value);
                }
            }

            match cmd.spawn() {
                Ok(mut child) => {
                    let child_pid = child.id();
                    // Store PID
                    if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
                        *guard = Some(child_pid);
                    }
                    // Store session ID
                    if let Ok(mut guard) = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None)).lock() {
                        *guard = Some(session_id.clone());
                    }

                    // Emit event so the desktop UI knows a task started
                    let _ = app_handle.emit("kim-agent-started", serde_json::json!({
                        "session_id": session_id,
                        "source": "kimctl",
                    }));

                    // Background thread: read stdout to capture SCREENSHOT_FLASH and emit to UI
                    let reader_handle = if let Some(stdout) = child.stdout.take() {
                        let reader = std::io::BufReader::new(stdout);
                        use std::io::BufRead;
                        let app_handle_out = app_handle.clone();
                        Some(std::thread::spawn(move || {
                            for l in reader.lines().map_while(Result::ok) {
                                let _ = app_handle_out.emit("kim-agent-output", l);
                            }
                        }))
                    } else {
                        None
                    };

                    // Background thread: wait for child to exit, then clear PID
                    let app_for_wait = app_handle.clone();
                    std::thread::spawn(move || {
                    let mut child = child;
                        let status = child.wait();
                        let success = status.as_ref().map(|s| s.success()).unwrap_or(false);
                        if let Some(handle) = reader_handle {
                            let _ = handle.join();
                        }
                        if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
                            *guard = None;
                        }
                        if let Ok(mut guard) = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None)).lock() {
                            *guard = None;
                        }
                        if let Some(cancel_win) = app_for_wait.get_webview_window("cancel-widget") {
                            let _ = cancel_win.close();
                        }
                        if let Some(flash_win) = app_for_wait.get_webview_window("screenshot-flash") {
                            let _ = flash_win.close();
                        }
                        if let Some(main_win) = app_for_wait.get_webview_window("main") {
                            let _ = main_win.show();
                            let _ = main_win.set_focus();
                        }
                        let _ = app_for_wait.emit("kim-agent-done", success);
                    });

                    respond_json(request, 200, serde_json::json!({
                        "ok": true,
                        "session_id": session_id,
                        "sessions_dir": session_dir.to_string_lossy(),
                    }));
                }
                Err(e) => {
                    respond_json(request, 500, serde_json::json!({
                        "ok": false,
                        "error": format!("Failed to start agent: {}", e),
                    }));
                }
            }
        }
        (Method::Post, "/v1/cancel") => {
            let pid = {
                let store = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None));
                if let Ok(guard) = store.lock() {
                    *guard
                } else {
                    None
                }
            };

            match pid {
                Some(pid) if process_exists(pid) => {
                    match send_signal(pid, false) {
                        Ok(()) => {
                            if let Some(cancel_win) = app_handle.get_webview_window("cancel-widget") {
                                let _ = cancel_win.close();
                            }
                            if let Some(main_win) = app_handle.get_webview_window("main") {
                                let _ = main_win.show();
                                let _ = main_win.set_focus();
                            }
                            if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
                                let _ = flash_win.close();
                            }
                            // Background cleanup: wait 2s then force-kill if alive
                            std::thread::spawn(move || {
                                for _ in 0..20 {
                                    std::thread::sleep(Duration::from_millis(100));
                                    if !process_exists(pid) {
                                        break;
                                    }
                                }
                                if process_exists(pid) {
                                    let _ = send_signal(pid, true);
                                }
                                if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
                                    *guard = None;
                                }
                                if let Ok(mut guard) = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None)).lock() {
                                    *guard = None;
                                }
                            });
                            respond_json(request, 200, serde_json::json!({"ok": true, "message": "Cancelling task."}));
                        }
                        Err(e) => {
                            respond_json(request, 500, serde_json::json!({
                                "ok": false,
                                "error": format!("Failed to send stop signal: {}", e),
                            }));
                        }
                    }
                }
                _ => {
                    respond_json(request, 200, serde_json::json!({
                        "ok": true,
                        "message": "No task is currently running.",
                    }));
                }
            }
        }
        (Method::Post, "/v1/browser/show") => {
            if app_handle.get_webview_window("kim-browser-signin").is_some() {
                show_browser_window_impl(&app_handle);
                respond_json(request, 200, serde_json::json!({"ok": true}));
            } else {
                respond_json(request, 200, serde_json::json!({"ok": false, "message": "No browser window exists yet."}));
            }
        }
        (Method::Post, "/v1/browser/hide") => {
            if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
                hide_browser_window_offscreen(&win);
            }
            respond_json(request, 200, serde_json::json!({"ok": true}));
        }
        (Method::Post, "/v1/browser/click") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct ClickRequest {
                selector: String,
            }

            let parsed: ClickRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };

            if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
                let selector_json = serde_json::to_string(&parsed.selector).unwrap_or_else(|_| "\"\"".to_string());
                let js = format!(
                    "(() => {{ const el = document.querySelector({}); if (el) {{ el.click(); return true; }} return false; }})()",
                    selector_json
                );
                match win.eval(&js) {
                    Ok(()) => {
                        respond_json(request, 200, serde_json::json!({"ok": true, "clicked": true}));
                    }
                    Err(e) => {
                        respond_json(request, 500, serde_json::json!({
                            "ok": false,
                            "error": format!("eval failed: {}", e),
                        }));
                    }
                }
            } else {
                respond_json(request, 200, serde_json::json!({"ok": false, "clicked": false, "error": "No browser window."}));
            }
        }
        (Method::Post, "/v1/browser/new-chat") => {
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
                    Err(e) => respond_json(request, 500, serde_json::json!({
                        "ok": false,
                        "error": format!("eval failed: {}", e),
                    })),
                }
            } else {
                respond_json(request, 200, serde_json::json!({"ok": false, "error": "No browser window."}));
            }
        }
        (Method::Post, "/v1/provider") => {
            let mut body = String::new();
            if let Err(e) = request.as_reader().read_to_string(&mut body) {
                respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid body: {}", e)}));
                return;
            }

            #[derive(Deserialize)]
            struct ProviderRequest {
                site: String,
            }

            let parsed: ProviderRequest = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    respond_json(request, 400, serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}));
                    return;
                }
            };

            let site = normalize_site(&parsed.site);
            if let Ok(mut guard) = KIM_PREFERRED_SITE.get_or_init(|| StdMutex::new(None)).lock() {
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

                if let Ok(js_url) = serde_json::to_string(url) {
                    let _ = win.eval(format!("window.location.href = {};", js_url));
                }
                respond_json(request, 200, serde_json::json!({"ok": true, "site": site}));
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
                        respond_json(request, 200, serde_json::json!({"ok": true, "site": site.clone(), "opened": true}));
                    }
                    Err(e) => {
                        respond_json(request, 500, serde_json::json!({"ok": false, "error": format!("{}", e)}));
                    }
                }
            }
        }
        _ => {
            respond_json(
                request,
                404,
                serde_json::json!({"ok": false, "error": format!("Unknown bridge route: {}", path)}),
            );
        }
    }
}



fn handle_bridge_result_request(
    request: Request,
    path: &str,
    _token: &str,
    app_handle: tauri::AppHandle,
) {
    let req_id = path.trim_start_matches("/v1/result/").to_string();
    if req_id.is_empty() {
        respond_json(request, 400, serde_json::json!({"ok": false, "error": "Missing req_id"}));
        return;
    }

    let window = if let Some(w) = app_handle.get_webview_window("kim-browser-signin") {
        w
    } else {
        respond_json(request, 500, serde_json::json!({"ok": false, "error": "Browser window closed."}));
        return;
    };

    agent_debug_log("H1", "result collector start (persistent bridge)", serde_json::json!({
        "reqId": req_id,
    }));

    let app_config = app_handle.state::<crate::config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    let result = collect_bridge_payload(
        &window,
        &req_id,
        Duration::from_secs(timeout_secs),
    );

    let mut should_hide = false;
    if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
        should_hide = guard.remove(&req_id);
    }
    if should_hide {
        hide_browser_window_offscreen(&window);
    }

    match result {
        Ok(payload) => {
            if payload.ok {
                respond_json(request, 200, serde_json::json!({
                    "ok": true,
                    "response": payload.response,
                    "site": payload.site.unwrap_or_else(|| "unknown".to_string()),
                    "req_id": req_id,
                }));
            } else {
                respond_json(request, 500, serde_json::json!({
                    "ok": false,
                    "error": payload.error.unwrap_or_else(|| "Unknown bridge error".to_string()),
                    "req_id": req_id,
                }));
            }
        }
        Err(e) => {
            respond_json(request, 504, serde_json::json!({
                "ok": false,
                "error": e,
                "req_id": req_id,
            }));
        }
    }
}

// ---------------------------------------------------------------------------
// Codex file-bridge command watcher (Tasks 4/5/7 of the browser-parity work)
// ---------------------------------------------------------------------------

const CODEX_BRIDGE_DIR: &str = "/tmp/codex_bridge";

/// Spawn a background thread that:
///  - Polls `/tmp/codex_bridge/browser_cmd.json` every 500 ms and dispatches
///    show/hide/switch_site actions to the Kim webview window.
///  - Writes `/tmp/codex_bridge/bridge_status.json` every 5 s so Codex's
///    `/browser status` command can report the current bridge state.
pub(crate) fn start_bridge_file_watcher(app_handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        let bridge_dir = std::path::Path::new(CODEX_BRIDGE_DIR);
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
                let _ = fs::create_dir_all(bridge_dir);
                if let Ok(text) = serde_json::to_string_pretty(&status) {
                    let _ = fs::write(&status_path, text);
                }
            }
        }
    });
}

fn show_screenshot_flash_impl(app_handle: &tauri::AppHandle) {
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
    } else if url.contains("grok.x.com") || url.contains("x.com/i/grok") {
        "grok"
    } else if url.contains("claude.ai") {
        "claude"
    } else {
        "unknown"
    }
}

pub(crate) fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

/// D2: persist the active bridge token to `~/.kim/bridge_token` (0600 on unix)
/// so the `kim` CLI can pair without any manual configuration.
fn write_bridge_token_file(token: &str) {
    let Some(home) = dirs::home_dir() else {
        return;
    };
    let dir = home.join(".kim");
    if std::fs::create_dir_all(&dir).is_err() {
        return;
    }
    let path = dir.join("bridge_token");
    if std::fs::write(&path, token).is_err() {
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
}

pub(crate) fn start_webview_bridge_server(app_handle: tauri::AppHandle) -> Result<(), String> {
    if WEBVIEW_BRIDGE_CFG.get().is_some() {
        return Ok(());
    }

    let mut selected: Option<(Server, u16)> = None;
    for port in 18991u16..19011u16 {
        if let Ok(server) = Server::http(("127.0.0.1", port)) {
            selected = Some((server, port));
            break;
        }
    }

    let (server, port) = selected
        .ok_or_else(|| "Could not bind local in-app bridge port (18991-19010).".to_string())?;

    let mut token = std::env::var("KIM_API_KEY").unwrap_or_default();
    if token.is_empty() {
        let env_path = default_project_root().join(".env");
        if let Ok(content) = std::fs::read_to_string(env_path) {
            for line in content.lines() {
                if line.starts_with("KIM_API_KEY=") || line.starts_with("RELAY_API_KEY=") {
                    token = line.split_once('=').map(|x| x.1).unwrap_or("").trim().to_string();
                    if (token.starts_with('"') && token.ends_with('"') && token.len() >= 2) || (token.starts_with('\'') && token.ends_with('\'') && token.len() >= 2) {
                        token = token[1..token.len()-1].to_string();
                    }
                    if !token.is_empty() {
                        break;
                    }
                }
            }
        }
    }

    if token.is_empty() {
        token = format!(
            "kim-{}-{}",
            std::process::id(),
            WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
        );
        eprintln!("[Kim] WARNING: KIM_API_KEY not found in env or .env. Falling back to random bridge token.");
    }

    let base_url = format!("http://127.0.0.1:{}", port);

    let _ = WEBVIEW_BRIDGE_CFG.set(WebviewBridgeConfig {
        base_url: base_url.clone(),
        token: token.clone(),
    });

    // Write only base_url to kim_sessions/.bridge_url (no cleartext tokens here)
    let sessions_dir = default_project_root().join("kim_sessions");
    let _ = std::fs::create_dir_all(&sessions_dir);
    let _ = std::fs::write(sessions_dir.join(".bridge_url"), &base_url);
    // Best-effort remove legacy cleartext token
    let _ = std::fs::remove_file(sessions_dir.join(".bridge_token"));

    // D2: pair with the CLI by default. Whatever token we settled on (env, .env,
    // or the random fallback) is written to ~/.kim/bridge_token with 0600 perms,
    // overwritten on every bridge start. This is a *local-loopback* credential —
    // it only authorizes requests to the 127.0.0.1 bridge on this machine.
    write_bridge_token_file(&token);

    let app_config = app_handle.state::<crate::config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    std::thread::spawn(move || {
        eprintln!(
            "[Kim] In-app browser bridge listening at {} (mode=sentinel_v1, timeout={}s)",
            base_url,
            timeout_secs,
        );
        for request in server.incoming_requests() {
            let app = app_handle.clone();
            let tok = token.clone();
            std::thread::spawn(move || {
                handle_webview_bridge_request(request, app, tok);
            });
        }
    });

    Ok(())
}
