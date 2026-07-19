//! In-app webview bridge engine: sign-in window, payload collection, and
//! one-shot bridge completion. Extracted from lib.rs — behavior unchanged.

use base64::Engine as _;
use std::collections::HashMap;
use std::time::{Duration, Instant};
use tauri::{Emitter, Listener, Manager};

use crate::*;

pub(crate) fn open_browser_signin_window_impl(
    url: &str,
    provider_name: Option<String>,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_inner(url, provider_name, true, false, app_handle)
}

/// Create the provider webview on behalf of the active bridge send itself.
/// `/v1/task` reserves the task slot before Python calls `/v1/send`, so the
/// normal user-navigation guard would otherwise make the first CLI request
/// incapable of creating its own missing transport window.
pub(crate) fn open_browser_signin_window_for_bridge_send(
    url: &str,
    provider_name: Option<String>,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_inner(url, provider_name, true, true, app_handle)
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
    open_browser_signin_window_inner(url, provider_name, initially_visible, false, app_handle)
}

fn open_browser_signin_window_inner(
    url: &str,
    provider_name: Option<String>,
    initially_visible: bool,
    allow_create_during_task: bool,
    app_handle: &tauri::AppHandle,
) -> Result<String, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("URL cannot be empty.".to_string());
    }

    let parsed = tauri::Url::parse(trimmed).map_err(|e| format!("Invalid URL: {}", e))?;
    match parsed.scheme() {
        "https" | "http" => {}
        _ => return Err("Only http:// or https:// URLs are allowed.".to_string()),
    }

    // F-D-1 / F-C-4 (desktop half): reject navigation to link-local /
    // RFC-1918 / loopback / cloud-metadata targets BEFORE creating or
    // navigating the webview. Without this, a local process holding the bridge
    // token (F-D-4) could `POST /v1/open {"url":"http://169.254.169.254/…"}`
    // and the app's own webview would dial the internal address, then run the
    // persistent bridge JS + title-pull channel over the fetched content. This
    // is the top-level-navigation sibling of Team C's subresource SSRF guard;
    // we mirror the same classification (numeric-IP encodings included). All
    // legitimate callers navigate to PUBLIC provider / OAuth hosts, so this
    // never fires on the real flows (the Google OAuth loopback redirect is
    // caught by a local TcpListener, not by navigating this webview).
    let host = parsed.host_str().unwrap_or("");
    if crate::ssrf::host_is_ssrf_target(host) {
        return Err(format!(
            "Refusing to open an internal/loopback/link-local address: {}",
            if host.is_empty() { trimmed } else { host }
        ));
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
        // L-BRIDGE-13: a failed navigation eval must surface as an error, not
        // as "Navigated existing Kim browser window".
        existing
            .eval(format!("window.location.href = {};", js_url))
            .map_err(|e| format!("Failed to navigate Kim browser window: {e}"))?;
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

    if !allow_create_during_task && is_bridge_task_running() {
        return Err(
            "Cannot open the provider browser while Kim is running a task; this would lose LLM context."
                .to_string(),
        );
    }

    let title = provider_name
        .map(|name| format!("Kim Browser - {}", name))
        .unwrap_or_else(|| "Kim Browser".to_string());

    let window =
        tauri::WebviewWindowBuilder::new(app_handle, label, tauri::WebviewUrl::External(parsed))
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

    if cleaned
        .chars()
        .filter(|ch| *ch == '{' || *ch == '}' || *ch == '\\')
        .count()
        > 8
    {
        return None;
    }

    if cleaned.len() > 280 {
        let cut = (0..=277)
            .rev()
            .find(|&i| cleaned.is_char_boundary(i))
            .unwrap_or(0);
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
        if guard
            .get(req_id)
            .map(|last| last == &cleaned)
            .unwrap_or(false)
        {
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

    // AUDIT FIX #3: timestamp every req_id this bridge sees, so the periodic
    // GC sweep can evict it later if nothing ever polls /v1/result.
    mark_bridge_entry_seen(&ipc_event.req_id);

    match ipc_event.event.as_str() {
        "sent" => {
            // Prompt was injected and Enter pressed. Store a "sent" marker
            // so the /v1/send endpoint can return immediately.
            let store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
            if let Ok(mut guard) = store.lock() {
                let sent_key = format!("{}_sent", ipc_event.req_id);
                guard.insert(
                    sent_key,
                    BridgeCompleteResponse {
                        ok: true,
                        response: None,
                        error: None,
                        site: ipc_event.site,
                        attachments_uploaded: None,
                    },
                );
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
                guard.insert(
                    ipc_event.req_id.clone(),
                    BridgeCompleteResponse {
                        ok: true,
                        response: ipc_event.response,
                        error: None,
                        site: ipc_event.site,
                        // Forward the bridge's verified upload count so
                        // /v1/result can hand it to Python's honesty gating.
                        attachments_uploaded: ipc_event.attachments_uploaded,
                    },
                );
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
                guard.insert(
                    ipc_event.req_id.clone(),
                    BridgeCompleteResponse {
                        ok: false,
                        response: None,
                        error: ipc_event.error,
                        site: ipc_event.site,
                        attachments_uploaded: None,
                    },
                );
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
                    agent_debug_log(
                        "H1",
                        "native_paste fired Cmd+V",
                        serde_json::json!({
                            "reqId": req_id,
                            "ok": status,
                        }),
                    );
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
    let (_, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| (StdMutex::new(()), Condvar::new()));
    condvar.notify_all();
}

// ---------------------------------------------------------------------------
// AUDIT FIX #3: bounded lifetime for WEBVIEW_BRIDGE_RESULTS/PROGRESS/WAS_HIDDEN
// ---------------------------------------------------------------------------
//
// Every entry in those maps is normally cleaned up by `collect_bridge_payload`
// (on success or its own request timeout), which runs inside the HTTP thread
// handling `GET /v1/result/{req_id}`. If the caller that would have polled
// that endpoint dies first -- a task force-killed via subprocess.rs's cancel
// escalation, an HTTP client that crashed, a kimctl invocation that was
// Ctrl-C'd -- nothing ever calls `/v1/result` for that req_id, so its entries
// (which can hold full LLM response text) sit in these maps for the app's
// entire remaining lifetime.
//
// There is no existing req_id <-> task-pid mapping to hook eviction directly
// into task cancellation, so this uses a TTL sweep as the backstop the audit
// finding calls out as acceptable: every insertion is timestamped in
// WEBVIEW_BRIDGE_ENTRY_TIMES, and a periodic sweep (started by
// `start_bridge_gc_sweeper`) evicts entries older than the TTL from all three
// maps regardless of whether anyone ever polled for them.

/// Default time-to-live for an unclaimed bridge req_id before the periodic
/// sweep evicts it. Generous relative to `bridge_timeout_secs` (the
/// /v1/result collector's own timeout, which is always much shorter) so this
/// never races a legitimate slow-but-in-flight request.
pub(crate) const BRIDGE_ENTRY_TTL: Duration = Duration::from_secs(15 * 60);

/// How often the sweeper runs.
const BRIDGE_GC_INTERVAL: Duration = Duration::from_secs(5 * 60);

/// Record (or refresh, if not already tracked) the first-seen time for a
/// bridge req_id. Idempotent -- call this at every insertion site
/// (`/v1/send`, the persistent-bridge IPC handler, `/v1/callback`) without
/// worrying about resetting an existing entry's clock.
pub(crate) fn mark_bridge_entry_seen(req_id: &str) {
    if let Ok(mut guard) = WEBVIEW_BRIDGE_ENTRY_TIMES
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.entry(req_id.to_string()).or_insert_with(Instant::now);
    }
}

/// Remove a req_id's tracked timestamp. Called from the normal (non-stale)
/// cleanup paths in `collect_bridge_payload` so WEBVIEW_BRIDGE_ENTRY_TIMES
/// itself does not grow unbounded with timestamps for requests that were
/// already cleaned up the fast way.
fn forget_bridge_entry_time(req_id: &str) {
    if let Ok(mut guard) = WEBVIEW_BRIDGE_ENTRY_TIMES
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(req_id);
    }
}

/// Evict every bridge req_id whose first-seen timestamp is older than `ttl`
/// from WEBVIEW_BRIDGE_RESULTS (+ its `{id}_sent` companion key),
/// WEBVIEW_BRIDGE_PROGRESS, and WEBVIEW_WAS_HIDDEN. Returns the evicted ids
/// (empty if nothing was stale) so callers can log/assert on it.
pub(crate) fn sweep_stale_bridge_entries(ttl: Duration) -> Vec<String> {
    let now = Instant::now();
    let stale_ids: Vec<String> = {
        let times = WEBVIEW_BRIDGE_ENTRY_TIMES.get_or_init(|| StdMutex::new(HashMap::new()));
        match times.lock() {
            Ok(guard) => guard
                .iter()
                .filter(|(_, seen)| now.duration_since(**seen) >= ttl)
                .map(|(id, _)| id.clone())
                .collect(),
            Err(_) => return Vec::new(),
        }
    };

    if stale_ids.is_empty() {
        return stale_ids;
    }

    if let Ok(mut guard) = WEBVIEW_BRIDGE_ENTRY_TIMES
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        for id in &stale_ids {
            guard.remove(id);
        }
    }
    if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        for id in &stale_ids {
            guard.remove(id);
            guard.remove(&format!("{id}_sent"));
        }
    }
    if let Ok(mut guard) = WEBVIEW_BRIDGE_PROGRESS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        for id in &stale_ids {
            guard.remove(id);
        }
    }
    if let Ok(mut guard) = WEBVIEW_WAS_HIDDEN
        .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
        .lock()
    {
        for id in &stale_ids {
            guard.remove(id);
        }
    }

    agent_debug_log(
        "H1",
        "bridge GC sweep evicted stale entries",
        serde_json::json!({ "count": stale_ids.len(), "ttlSecs": ttl.as_secs() }),
    );

    stale_ids
}

/// Start the periodic background sweep. Idempotent to call once from app
/// setup alongside `scheduler::start_scheduler`; spawns a tokio task that
/// loops for the life of the app.
pub(crate) fn start_bridge_gc_sweeper(app_handle: tauri::AppHandle) {
    let _ = app_handle; // reserved for future emit-on-sweep telemetry
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(BRIDGE_GC_INTERVAL);
        interval.tick().await; // consume the immediate first tick
        loop {
            interval.tick().await;
            sweep_stale_bridge_entries(BRIDGE_ENTRY_TTL);
        }
    });
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
    let (lock, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| (StdMutex::new(()), Condvar::new()));

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
                    if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS
                        .get_or_init(|| StdMutex::new(HashMap::new()))
                        .lock()
                    {
                        pg.remove(req_id);
                    }
                    if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN
                        .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
                        .lock()
                    {
                        hg.remove(req_id);
                    }
                    forget_bridge_entry_time(req_id);
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
            if let Ok(mut pg) = WEBVIEW_BRIDGE_PROGRESS
                .get_or_init(|| StdMutex::new(HashMap::new()))
                .lock()
            {
                pg.remove(req_id);
            }
            if let Ok(mut hg) = WEBVIEW_WAS_HIDDEN
                .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
                .lock()
            {
                hg.remove(req_id);
            }
            forget_bridge_entry_time(req_id);
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
        match pull_payload_from_js_store_legacy(window, req_id) {
            Ok(Some(payload)) => {
                agent_debug_log(
                    "H2",
                    "collect: result found via title-pull",
                    serde_json::json!({ "reqId": req_id, "loops": ipc_wait_loops }),
                );
                forget_bridge_entry_time(req_id);
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

/// H-BRIDGE-2: `document.title` is a single shared slot on the ONE provider
/// window, but the split send/receive API lets several `GET /v1/result/{id}`
/// collectors title-pull concurrently (WEBVIEW_BRIDGE_LOCK is released once
/// `/v1/send` returns). This mutex serialises each eval→sleep→read→clear
/// cycle so collectors can't interleave their title writes.
static TITLE_PULL_LOCK: StdMutex<()> = StdMutex::new(());

pub(crate) const TITLE_PULL_NULL_MARKER: &str = "__KIMBRIDGE_NONE__";

/// Decode one title-pull read. The title must carry the `req_id|` stamp the
/// matching write placed there (H-BRIDGE-2): a title written for another
/// collector's req_id is discarded instead of being returned as OUR payload
/// (the old format was un-attributed base64, so a swap was undetectable and
/// request A's LLM response could be handed to request B).
pub(crate) fn decode_title_pull_payload(
    req_id: &str,
    title: &str,
) -> Option<BridgeCompleteResponse> {
    let payload_b64 = title.strip_prefix(&format!("{req_id}|"))?;
    if payload_b64 == TITLE_PULL_NULL_MARKER
        || payload_b64.trim().is_empty()
        || payload_b64.contains(' ')
    {
        return None;
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(payload_b64)
        .ok()?;
    let decoded_str = String::from_utf8(decoded).ok()?;
    serde_json::from_str::<BridgeCompleteResponse>(&decoded_str).ok()
}

/// Legacy title-polling fallback — only used when IPC isn't available.
/// Kept for backward compatibility with pages that don't have IPC access.
pub(crate) fn pull_payload_from_js_store_legacy(
    window: &tauri::WebviewWindow,
    req_id: &str,
) -> Result<Option<BridgeCompleteResponse>, String> {
    let req_id_json = serde_json::to_string(req_id)
        .map_err(|e| format!("Failed to encode req_id for JS: {}", e))?;

    // Serialise the whole eval→sleep→read→clear cycle across collectors
    // (H-BRIDGE-2). Held only in bridge HTTP threads, never on the async runtime.
    let _pull_guard = TITLE_PULL_LOCK
        .lock()
        .map_err(|_| "Title-pull lock poisoned.".to_string())?;

    // Stamp the req_id into the title so the read below can verify it is OUR
    // write (and not a concurrent collector's) before decoding.
    let write_js = format!(
        r#"(() => {{
            try {{
                const store = window.__kimBridgeStore || {{}};
                const entry = store[{req_id_json}];
                const data = (entry && typeof entry.data === 'string' && entry.data.length > 0)
                    ? entry.data : '{null_marker}';
                document.title = {req_id_json} + '|' + data;
            }} catch (_) {{
                document.title = {req_id_json} + '|' + '{null_marker}';
            }}
        }})()"#,
        req_id_json = req_id_json,
        null_marker = TITLE_PULL_NULL_MARKER,
    );
    window.eval(write_js).map_err(|e| e.to_string())?;

    std::thread::sleep(Duration::from_millis(80));

    let title = window.title().map_err(|e| e.to_string())?;

    agent_debug_log(
        "H2",
        "title pull read title",
        serde_json::json!({ "reqId": req_id, "title": title }),
    );

    let Some(payload) = decode_title_pull_payload(req_id, &title) else {
        return Ok(None);
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
    effort: Option<&str>,
) -> Result<BridgeCompleteResponse, String> {
    let app_config = window.state::<config::AppConfig>();
    let timeout_secs = app_config.bridge_timeout_secs;

    let req_id = format!(
        "r-{}-{}",
        std::process::id(),
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    // AUDIT FIX #3: see mark_bridge_entry_seen doc comment.
    mark_bridge_entry_seen(&req_id);
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
    let effort_json = serde_json::to_string(&effort).unwrap_or_else(|_| "null".to_string());

    let bridge_call = format!(
        r#"(() => {{
            if (window.__kimBridge && window.__kimBridge._v >= 2) {{
                window.__kimBridge.send({prompt}, {req_id}, {site}, {attachments}, null, {hash}, {tier}, {effort});
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
        effort = effort_json,
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

    let result = collect_bridge_payload(window, &req_id, Duration::from_secs(timeout_secs));

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

#[cfg(test)]
mod title_pull_tests {
    use super::*;

    fn encode(payload: &BridgeCompleteResponse) -> String {
        base64::engine::general_purpose::STANDARD.encode(serde_json::to_string(payload).unwrap())
    }

    fn sample(response: &str) -> BridgeCompleteResponse {
        BridgeCompleteResponse {
            ok: true,
            response: Some(response.to_string()),
            error: None,
            site: Some("chatgpt".to_string()),
            attachments_uploaded: None,
        }
    }

    // H-BRIDGE-2: a title stamped with OUR req_id decodes to our payload.
    #[test]
    fn decode_accepts_own_req_id() {
        let payload = sample("answer for A");
        let title = format!("r-1-7|{}", encode(&payload));
        let got = decode_title_pull_payload("r-1-7", &title).expect("must decode own payload");
        assert_eq!(got.response.as_deref(), Some("answer for A"));
    }

    // H-BRIDGE-2 core regression: another collector's payload must be
    // DISCARDED, not returned as ours (previously undetectable → response
    // swap between concurrent /v1/result collectors).
    #[test]
    fn decode_rejects_other_collectors_payload() {
        let payload = sample("answer for A");
        let title = format!("r-1-7|{}", encode(&payload));
        assert!(
            decode_title_pull_payload("r-1-8", &title).is_none(),
            "payload stamped for r-1-7 must never be returned to r-1-8"
        );
    }

    #[test]
    fn decode_rejects_null_marker_and_garbage() {
        assert!(decode_title_pull_payload("r-1-7", "r-1-7|__KIMBRIDGE_NONE__").is_none());
        assert!(decode_title_pull_payload("r-1-7", "r-1-7|not base64!!").is_none());
        // Normal page titles (no stamp at all).
        assert!(decode_title_pull_payload("r-1-7", "ChatGPT").is_none());
        assert!(decode_title_pull_payload("r-1-7", "Gemini - Google").is_none());
        assert!(decode_title_pull_payload("r-1-7", "").is_none());
    }

    // A req_id that is a prefix of another must not cross-match.
    #[test]
    fn decode_prefix_req_ids_do_not_cross_match() {
        let payload = sample("x");
        let title = format!("r-1-71|{}", encode(&payload));
        assert!(decode_title_pull_payload("r-1-7", &title).is_none());
    }
}

// AUDIT FIX #3: sweep_stale_bridge_entries / mark_bridge_entry_seen.
//
// Every test uses a req_id unique to that test (these statics are process-
// global and shared across the whole test binary, which runs tests in
// parallel by default) so tests cannot interfere with each other's entries.
#[cfg(test)]
mod gc_sweep_tests {
    use super::*;

    // These tests share process-global statics (WEBVIEW_BRIDGE_RESULTS etc.)
    // with EACH OTHER, and `sweep_stale_bridge_entries` operates on the
    // *entire* map, not just one test's ids -- a ttl=0 sweep in one test
    // would otherwise race and evict entries a concurrently-running sibling
    // test just inserted (cargo test runs tests in parallel by default).
    // Serialize this module's tests against each other with a local lock;
    // other test modules in this crate don't touch these statics (verified:
    // only browser_bridge.rs itself does), so this doesn't need to be
    // crate-wide.
    static TEST_LOCK: StdMutex<()> = StdMutex::new(());

    fn lock_guard() -> std::sync::MutexGuard<'static, ()> {
        TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn sample_response() -> BridgeCompleteResponse {
        BridgeCompleteResponse {
            ok: true,
            response: Some("hi".to_string()),
            error: None,
            site: Some("chatgpt".to_string()),
            attachments_uploaded: None,
        }
    }

    #[test]
    fn sweep_evicts_orphaned_entry_across_all_three_maps() {
        let _guard = lock_guard();
        let id = "gc-test-evict-all-maps";

        mark_bridge_entry_seen(id);
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .insert(id.to_string(), sample_response());
        WEBVIEW_BRIDGE_PROGRESS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .insert(id.to_string(), "working...".to_string());
        WEBVIEW_WAS_HIDDEN
            .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
            .lock()
            .unwrap()
            .insert(id.to_string());

        // ttl=0: anything with a recorded timestamp is immediately "stale"
        // (elapsed >= 0 is always true), so this deterministically evicts
        // without needing a real sleep.
        let evicted = sweep_stale_bridge_entries(Duration::ZERO);
        assert!(
            evicted.contains(&id.to_string()),
            "orphaned entry must be evicted by the sweep"
        );

        assert!(!WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .contains_key(id));
        assert!(!WEBVIEW_BRIDGE_PROGRESS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .contains_key(id));
        assert!(!WEBVIEW_WAS_HIDDEN
            .get_or_init(|| StdMutex::new(std::collections::HashSet::new()))
            .lock()
            .unwrap()
            .contains(id));
    }

    #[test]
    fn sweep_removes_sent_companion_key() {
        let _guard = lock_guard();
        let id = "gc-test-sent-companion";
        let sent_key = format!("{id}_sent");

        mark_bridge_entry_seen(id);
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .insert(sent_key.clone(), sample_response());

        sweep_stale_bridge_entries(Duration::ZERO);

        assert!(!WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .contains_key(&sent_key));
    }

    #[test]
    fn sweep_does_not_evict_entries_within_ttl() {
        let _guard = lock_guard();
        let id = "gc-test-fresh-entry";

        mark_bridge_entry_seen(id);
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .insert(id.to_string(), sample_response());

        // A generous ttl must not evict something marked moments ago.
        let evicted = sweep_stale_bridge_entries(Duration::from_secs(600));
        assert!(
            !evicted.contains(&id.to_string()),
            "fresh entry must not be evicted while within its TTL"
        );
        assert!(WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .contains_key(id));

        // Clean up so this entry doesn't linger for the rest of the test binary.
        forget_bridge_entry_time(id);
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .remove(id);
    }

    #[test]
    fn sweep_is_a_noop_for_untracked_ids() {
        let _guard = lock_guard();
        // An id with no WEBVIEW_BRIDGE_ENTRY_TIMES entry (never marked) must
        // never appear in the sweep's eviction list, and calling the sweep
        // must not panic even if the map entry is absent from RESULTS too.
        let evicted = sweep_stale_bridge_entries(Duration::ZERO);
        assert!(!evicted.contains(&"gc-test-never-marked".to_string()));
    }

    #[test]
    fn forget_bridge_entry_time_prevents_future_eviction() {
        let _guard = lock_guard();
        let id = "gc-test-forgotten";

        mark_bridge_entry_seen(id);
        forget_bridge_entry_time(id);
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .insert(id.to_string(), sample_response());

        // No timestamp is tracked anymore, so ttl=0 must not touch this key
        // (mirrors what happens on the normal collect_bridge_payload success
        // path: forget happens right before/alongside removal from RESULTS).
        let evicted = sweep_stale_bridge_entries(Duration::ZERO);
        assert!(!evicted.contains(&id.to_string()));

        // Cleanup.
        WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
            .unwrap()
            .remove(id);
    }
}
