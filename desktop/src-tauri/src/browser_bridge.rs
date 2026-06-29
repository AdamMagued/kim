//! In-app webview bridge engine: the persistent bridge JS, the browser
//! sign-in window, payload collection, and one-shot bridge completion.
//! Extracted from lib.rs (file-split restructure) — behavior unchanged.

use std::collections::HashMap;
use std::time::{Duration, Instant};
use base64::Engine as _;
use tauri::{Emitter, Listener, Manager};

use crate::*;

pub(crate) const PERSISTENT_BRIDGE_JS: &str = include_str!("bridge.js");

pub(crate) fn open_browser_signin_window_impl(
    url: &str,
    provider_name: Option<String>,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_with_visibility(url, provider_name, true, app_handle)
}

/// Underlying implementation that lets callers create the window in a hidden
/// offscreen state. Used by `restore_browser_for_session` (we want the webview
/// alive so cookies/session URL hydrate, but the user should not see a popup
/// appear every time they click a chat in the sidebar) and by the auth-probe
/// path (we want to silently fetch /api/auth/session in the background).
pub(crate) fn open_browser_signin_window_with_visibility(
    url: &str,
    provider_name: Option<String>,
    initially_visible: bool,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("URL cannot be empty.".to_string());
    }

    let parsed = tauri::Url::parse(trimmed)
        .map_err(|e| format!("Invalid URL: {}", e))?;
    match parsed.scheme() {
        "https" | "http" => {}
        _ => return Err("Only http:// or https:// URLs are allowed.".to_string()),
    }

    let label = "kim-browser-signin";
    if let Some(existing) = app_handle.get_webview_window(label) {
        let task_running = is_bridge_task_running();

        if task_running {
            return Ok(
                "Reused existing Kim browser window without navigation because a task is active."
                    .to_string(),
            );
        }

        let js_url = serde_json::to_string(trimmed).map_err(|e| e.to_string())?;
        let _ = existing.eval(format!("window.location.href = {};", js_url));
        // Preserve current visibility: if the caller wants invisible and the
        // window is currently shown, hide it; if visible and currently hidden,
        // bring it back into view.
        if initially_visible && is_browser_window_offscreen(&existing) {
            show_browser_window_impl(app_handle);
        } else if !initially_visible && !is_browser_window_offscreen(&existing) {
            hide_browser_window_offscreen(&existing);
        }
        return Ok("Navigated existing Kim browser window".to_string());
    }

    if is_bridge_task_running() {
        return Err(
            "Cannot open the provider browser while Kim is running a task; this would lose LLM context."
                .to_string(),
        );
    }

    let title = provider_name
        .map(|name| format!("Kim Browser - {}", name))
        .unwrap_or_else(|| "Kim Browser".to_string());

    let window = tauri::WebviewWindowBuilder::new(
        app_handle,
        label,
        tauri::WebviewUrl::External(parsed),
    )
    .title(title)
    .inner_size(1280.0, 860.0)
    .resizable(true)
    .visible(initially_visible)
    .initialization_script(PERSISTENT_BRIDGE_JS)
    .build()
    .map_err(|e| format!("Failed to open Kim browser window: {}", e))?;

    // If we built it invisible, immediately move it offscreen so that later
    // calls to `is_browser_window_offscreen` recognise it as hidden and so the
    // page still has a real viewport for layout (required for the bridge JS).
    if !initially_visible {
        hide_browser_window_offscreen(&window);
    }

    let window_for_close = window.clone();
    let app_for_close = app_handle.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            // Keep the webview session alive for background/headless execution.
            // Hide the window instead of closing it so the DOM keeps rendering.
            api.prevent_close();
            hide_browser_window_offscreen(&window_for_close);

            // Refocus the main app window so the user isn't left staring at nothing.
            if let Some(main_win) = app_for_close.get_webview_window("main") {
                let _ = main_win.show();
                let _ = main_win.set_focus();
            }
            // Notify the frontend that the browser window was dismissed.
            let _ = app_for_close.emit("kim-browser-hidden", true);
        }
    });

    // Listen for IPC events from the persistent JS bridge only once per app instance.
    IPC_LISTENER_REGISTERED.get_or_init(|| {
        let app_for_listener = app_handle.clone();
        let app_for_event = app_for_listener.clone();
        app_for_listener.listen("kim-bridge-ipc", move |event| {
            let payload_str = event.payload();
            match serde_json::from_str::<BridgeIpcEvent>(payload_str) {
                Ok(ipc_event) => {
                    handle_bridge_ipc_event(ipc_event, &app_for_event);
                }
                Err(e) => {
                    eprintln!(
                        "[Kim] Failed to parse bridge IPC event: {} — payload: {}",
                        e,
                        payload_str.chars().take(200).collect::<String>()
                    );
                }
            }
        });
    });

    if let Some(existing) = app_handle.get_webview_window(label) {
        // Only steal focus if the window is actually visible on-screen.
        // In headless mode (offscreen at -10000,-10000) we must NOT focus
        // because it would pull the user away from their current page.
        if !is_browser_window_offscreen(&existing) {
            let _ = existing.set_focus();
        }
    }

    Ok("Opened in Kim browser window".to_string())
}

#[tauri::command]
pub(crate) async fn add_custom_provider_capability(url: String, app_handle: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    use tauri::ipc::CapabilityBuilder;

    let parsed = tauri::Url::parse(&url).map_err(|e| e.to_string())?;
    let origin = parsed.origin().ascii_serialization();
    let capability_url = format!("{}/*", origin);

    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default() // avoids panic if system clock is before epoch (#21)
        .as_nanos();
    let cap_id = format!("kim-custom-bridge-{}", nanos);

    let cap = CapabilityBuilder::new(cap_id)
        .remote(capability_url)
        .window("kim-browser-signin")
        .permission("core:default")
        .permission("core:event:allow-emit");

    app_handle.add_capability(cap).map_err(|e| format!("Failed to add runtime capability: {}", e))?;
    Ok(())
}

/// Handle an IPC event from the persistent JS bridge.
/// Inserts results into the shared store and wakes up any waiting collector.
pub(crate) fn clean_bridge_progress_text(text: &str) -> Option<String> {
    let mut cleaned = text
        .replace('\u{0000}', "")
        .replace("\r\n", "\n")
        .trim()
        .to_string();

    while cleaned.contains("\n\n\n") {
        cleaned = cleaned.replace("\n\n\n", "\n\n");
    }
    cleaned = cleaned
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join(" ");

    let lower = cleaned.to_lowercase();
    if cleaned.len() < 8
        || lower.starts_with("error:")
        || lower.starts_with("need_help:")
        || lower.starts_with("{text")
        || lower.starts_with("{\"text")
        || lower.starts_with("{ \"text")
        || lower.starts_with('{')
        || lower.contains("\"tool_calls\"")
        || lower.contains("\"content\"")
        || lower.contains("<!doctype")
        || lower.contains("<html")
        || lower.contains("<style")
        || lower.contains("kim_")
    {
        return None;
    }

    if cleaned.chars().filter(|ch| *ch == '{' || *ch == '}' || *ch == '\\').count() > 8 {
        return None;
    }

    if cleaned.len() > 280 {
        let cut = (0..=277).rev().find(|&i| cleaned.is_char_boundary(i)).unwrap_or(0);
        cleaned.truncate(cut);
        cleaned.push_str("...");
    }

    Some(cleaned)
}

pub(crate) fn emit_bridge_progress(app_handle: &tauri::AppHandle, req_id: &str, text: &str) {
    let Some(cleaned) = clean_bridge_progress_text(text) else {
        return;
    };

    let progress_store = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new()));
    if let Ok(mut guard) = progress_store.lock() {
        if guard.get(req_id).map(|last| last == &cleaned).unwrap_or(false) {
            return;
        }
        guard.insert(req_id.to_string(), cleaned.clone());
    }

    let _ = app_handle.emit("kim-agent-output", format!("[PROGRESS] {}", cleaned));
}

pub(crate) fn handle_bridge_ipc_event(ipc_event: BridgeIpcEvent, app_handle: &tauri::AppHandle) {
    agent_debug_log(
        "H1",
        "bridge IPC event received",
        serde_json::json!({
            "event": ipc_event.event,
            "reqId": ipc_event.req_id,
            "hasResponse": ipc_event.response.is_some(),
            "hasError": ipc_event.error.is_some(),
            "hasText": ipc_event.text.is_some(),
            // Surface the full error string (carries bridge.js diag={...} on the
            // 2nd-turn timeout) so failures are debuggable from bridge_debug.log.
            "error": ipc_event.error,
        }),
    );

    match ipc_event.event.as_str() {
        "sent" => {
            // Prompt was injected and Enter pressed. Store a "sent" marker
            // so the /v1/send endpoint can return immediately.
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                let sent_key = format!("{}_sent", ipc_event.req_id);
                guard.insert(sent_key, BridgeCompleteResponse {
                    ok: true,
                    response: None,
                    error: None,
                    site: ipc_event.site,
                });
            }
            // Wake up anyone waiting for the "sent" signal.
            notify_bridge_result();
        }
        "done" => {
            if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
                .get_or_init(|| StdMutex::new(HashMap::new()))
                .lock()
            {
                guard.remove(&ipc_event.req_id);
            }
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                guard.insert(ipc_event.req_id.clone(), BridgeCompleteResponse {
                    ok: true,
                    response: ipc_event.response,
                    error: None,
                    site: ipc_event.site,
                });
            }
            notify_bridge_result();
        }
        "progress" => {
            if let Some(text) = ipc_event.text.as_deref() {
                emit_bridge_progress(app_handle, &ipc_event.req_id, text);
            }
        }
        "error" => {
            if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
                .get_or_init(|| StdMutex::new(HashMap::new()))
                .lock()
            {
                guard.remove(&ipc_event.req_id);
            }
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                guard.insert(ipc_event.req_id.clone(), BridgeCompleteResponse {
                    ok: false,
                    response: None,
                    error: ipc_event.error,
                    site: ipc_event.site,
                });
            }
            notify_bridge_result();
        }
        "native_paste" => {
            // The bridge JS focused the chat editor and asked us to paste the image
            // with a REAL Cmd+V. WKWebView blocks programmatic paste (execCommand
            // returns false) and rejects synthetic ClipboardEvent (isTrusted:false),
            // but a genuine keystroke is trusted and accepted. The PNG is already on
            // the system clipboard (write_first_png_to_clipboard) and the webview is
            // visible + frontmost + key for image sends, so the keystroke lands in the
            // focused editor — the same trick the Playwright path uses via Cmd+V.
            #[cfg(target_os = "macos")]
            {
                let req_id = ipc_event.req_id.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(150));
                    let status = std::process::Command::new("osascript")
                        .arg("-e")
                        .arg(r#"tell application "System Events" to keystroke "v" using command down"#)
                        .status()
                        .map(|s| s.success())
                        .unwrap_or(false);
                    agent_debug_log("H1", "native_paste fired Cmd+V", serde_json::json!({
                        "reqId": req_id,
                        "ok": status,
                    }));
                });
            }
        }
        other => {
            eprintln!("[Kim] Unknown bridge IPC event type: {}", other);
        }
    }
}

/// Wake up any thread waiting on WEBVIEW_BRIDGE_NOTIFY.
pub(crate) fn notify_bridge_result() {
    let (_, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
        (StdMutex::new(()), Condvar::new())
    });
    condvar.notify_all();
}


// build_bridge_complete_script deleted (#22): the persistent bridge (bridge.js) is
// always loaded via initialization_script so the ~1000-line legacy inline-script
// fallback was dead code.


/// Condvar-based bridge payload collector.
///
/// Primary path: waits on WEBVIEW_BRIDGE_NOTIFY condvar which is notified
/// instantly when the JS bridge sends an IPC event.  No polling, no title
/// hacks.  Falls back to legacy title-polling if IPC hasn't delivered after
/// a few seconds (for backward compat with old JS scripts).
pub(crate) fn collect_bridge_payload(
    window: &tauri::WebviewWindow,
    req_id: &str,
    timeout: Duration,
) -> Result<BridgeCompleteResponse, String> {
    let started = Instant::now();
    let result_store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    let (lock, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
        (StdMutex::new(()), Condvar::new())
    });

    // For legacy fallback: if IPC isn't delivering, fall back to title polling
    let req_id_json = serde_json::to_string(req_id)
        .map_err(|e| format!("Failed to encode req_id for JS: {}", e))?;
    let mut ipc_wait_loops: u64 = 0;

    loop {
        // Check if result is already in the store (from IPC or HTTP callback).
        match result_store.lock() {
            Ok(mut guard) => {
                if let Some(payload) = guard.get(req_id).cloned() {
                    // Remove result and associated markers
                    guard.remove(req_id);
                    guard.remove(&format!("{}_sent", req_id));
                    agent_debug_log(
                        "H1",
                        "collect: result found via IPC",
                        serde_json::json!({ "reqId": req_id, "loops": ipc_wait_loops }),
                    );
                    // Also clean up progress and hidden-state entries
                    if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new())).lock() {
                        pg.remove(req_id);
                    }
                    if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                        hg.remove(req_id);
                    }
                    return Ok(payload);
                }
            }
            Err(_) => return Err("Bridge results lock poisoned.".to_string()),
        }

        // Check for a fatal title
        if let Ok(title) = window.title() {
            if let Some(msg) = title.strip_prefix("__KIMBRIDGE_FATAL__:") {
                return Err(format!("Bridge script fatal error: {}", msg.trim()));
            }
        }

        if started.elapsed() >= timeout {
            // Clean up leaked entries on timeout
            if let Ok(mut guard) = result_store.lock() {
                guard.remove(req_id);
                guard.remove(&format!("{}_sent", req_id));
            }
            if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS.get_or_init(|| StdMutex::new(HashMap::new())).lock() {
                pg.remove(req_id);
            }
            if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN.get_or_init(|| StdMutex::new(std::collections::HashSet::new())).lock() {
                hg.remove(req_id);
            }
            agent_debug_log(
                "H3",
                "collect timeout waiting payload",
                serde_json::json!({
                    "reqId": req_id,
                    "ipcWaitLoops": ipc_wait_loops,
                }),
            );
            return Err("Timed out waiting for in-app browser completion response.".to_string());
        }

        ipc_wait_loops += 1;

        // Wait briefly before the next title-pull attempt.
        // Since IPC doesn't work on external pages, the title-pull on each
        // iteration is our real collection mechanism (~500ms cadence).
        let wait_duration = Duration::from_millis(500);
        if let Ok(guard) = lock.lock() {
            let _ = condvar.wait_timeout(guard, wait_duration);
        }

        // Always try legacy title-pull on every iteration.
        // Tauri IPC (emit) does NOT work on external pages — __TAURI_INTERNALS__
        // is not injected even with remote IPC capabilities. The JS bridge stores
        // results in window.__kimBridgeStore which we poll via title-pull.
        match pull_payload_from_js_store_legacy(window, &req_id_json) {
            Ok(Some(payload)) => {
                agent_debug_log(
                    "H2",
                    "collect: result found via title-pull",
                    serde_json::json!({ "reqId": req_id, "loops": ipc_wait_loops }),
                );
                return Ok(payload);
            }
            Ok(None) => {}
            Err(e) => {
                agent_debug_log(
                    "H3",
                    "collect title-pull failed",
                    serde_json::json!({ "reqId": req_id, "error": e }),
                );
            }
        }
    }
}

/// Legacy title-polling fallback — only used when IPC isn't available.
/// Kept for backward compatibility with pages that don't have IPC access.
pub(crate) fn pull_payload_from_js_store_legacy(
    window: &tauri::WebviewWindow,
    req_id_json: &str,
) -> Result<Option<BridgeCompleteResponse>, String> {
    const NULL_MARKER: &str = "__KIMBRIDGE_NONE__";

    let write_js = format!(
        r#"(() => {{
            try {{
                const store = window.__kimBridgeStore || {{}};
                const entry = store[{req_id_json}];
                const data = (entry && typeof entry.data === 'string' && entry.data.length > 0)
                    ? entry.data : '{null_marker}';
                document.title = data;
            }} catch (_) {{
                document.title = '{null_marker}';
            }}
        }})()"#,
        req_id_json = req_id_json,
        null_marker = NULL_MARKER,
    );
    window.eval(write_js).map_err(|e| e.to_string())?;

    std::thread::sleep(Duration::from_millis(80));

    let title = window.title().map_err(|e| e.to_string())?;

    agent_debug_log(
        "H2",
        "title pull read title",
        serde_json::json!({ "reqId": req_id_json, "title": title }),
    );

    if title == NULL_MARKER || title.trim().is_empty() || title.contains(' ') {
        return Ok(None);
    }

    let decoded = match base64::engine::general_purpose::STANDARD.decode(&title) {
        Ok(b) => b,
        Err(_) => return Ok(None),
    };
    let decoded_str = match String::from_utf8(decoded) {
        Ok(s) => s,
        Err(_) => return Ok(None),
    };
    let payload: BridgeCompleteResponse = match serde_json::from_str(&decoded_str) {
        Ok(p) => p,
        Err(_) => return Ok(None),
    };

    let clear_js = format!(
        "try {{ delete (window.__kimBridgeStore || {{}})[{req_id_json}]; }} catch(_) {{}}",
        req_id_json = req_id_json,
    );
    let _ = window.eval(clear_js);

    Ok(Some(payload))
}

/// Run a completion using the persistent bridge (preferred) or the legacy
/// full-script approach (fallback).
///
/// The persistent bridge is already loaded via initialization_script:
///   window.__kimBridge.send(prompt, reqId, site, attachments)
/// This is a ~100 byte eval call vs the previous ~30KB script injection.
#[allow(clippy::too_many_arguments)]
pub(crate) fn run_bridge_completion_once(
    window: &tauri::WebviewWindow,
    site: &str,
    prompt: &str,
    attachments: &[BridgeAttachment],
    _callback_url: &str,
    _callback_token: &str,
    completion_hash: Option<&str>,
    model_tier: Option<&str>,
) -> Result<BridgeCompleteResponse, String> {
    let app_config = window.state::<config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    let req_id = format!(
        "r-{}-{}",
        std::process::id(),
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    agent_debug_log(
        "H1",
        "run_bridge_completion_once start",
        serde_json::json!({
            "reqId": req_id,
            "site": site,
            "promptLen": prompt.len(),
            "attachments": attachments.len(),
            "collectorMode": "sentinel_v1",
            "collectorTimeoutS": timeout_secs,
        }),
    );

    // Clear any stale result for this req_id.
    if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
    }
    if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
    }

    // Try the persistent bridge first (tiny eval call).
    let prompt_json = serde_json::to_string(prompt).map_err(|e| e.to_string())?;
    let req_id_json = serde_json::to_string(&req_id).map_err(|e| e.to_string())?;
    let site_json = serde_json::to_string(site).map_err(|e| e.to_string())?;
    let attachments_json = serde_json::to_string(attachments).map_err(|e| e.to_string())?;
    let hash_json = serde_json::to_string(&completion_hash).unwrap_or_else(|_| "null".to_string());

    let tier_json = serde_json::to_string(&model_tier).unwrap_or_else(|_| "null".to_string());

    let bridge_call = format!(
        r#"(() => {{
            if (window.__kimBridge && window.__kimBridge._v >= 2) {{
                window.__kimBridge.send({prompt}, {req_id}, {site}, {attachments}, null, {hash}, {tier});
            }} else {{
                // Persistent bridge not installed — signal to fall back
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

    agent_debug_log(
        "H1",
        "bridge eval begin (persistent)",
        serde_json::json!({ "reqId": req_id, "scriptLen": bridge_call.len() }),
    );

    if let Err(e) = window.eval(&bridge_call) {
        agent_debug_log(
            "H3",
            "bridge eval failed",
            serde_json::json!({ "reqId": req_id, "error": e.to_string() }),
        );
        return Err(format!("Failed to evaluate in-app script: {}", e));
    }

    // The persistent bridge (bridge.js) is always loaded via initialization_script,
    // so the __KIMBRIDGE_NO_PERSISTENT__ marker should never appear in practice.
    // The legacy build_bridge_complete_script fallback has been removed (#22).

    agent_debug_log(
        "H1",
        "bridge collect begin",
        serde_json::json!({
            "reqId": req_id,
            "timeoutS": timeout_secs,
            "mode": "sentinel_v1",
        }),
    );

    let result = collect_bridge_payload(
        window,
        &req_id,
        Duration::from_secs(timeout_secs),
    );

    agent_debug_log(
        "H1",
        "bridge collect returned",
        serde_json::json!({ "reqId": req_id, "ok": result.is_ok() }),
    );
    match &result {
        Ok(payload) => agent_debug_log(
            "H2",
            "bridge completion collected payload",
            serde_json::json!({
                "reqId": req_id,
                "ok": payload.ok,
                "hasResponse": payload.response.as_ref().map(|s| !s.is_empty()).unwrap_or(false),
                "error": payload.error,
            }),
        ),
        Err(e) => agent_debug_log(
            "H3",
            "bridge completion collect failed",
            serde_json::json!({ "reqId": req_id, "error": e }),
        ),
    }
    result
}


// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------


#[tauri::command]
pub(crate) async fn open_browser_signin_window(
    url: String,
    provider_name: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_impl(&url, provider_name, &app_handle)
}

