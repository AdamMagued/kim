use base64::Engine as _;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Condvar, Mutex as StdMutex, OnceLock};
use std::time::Duration;
use tauri::{Emitter, Manager};

pub mod account;
pub mod browser_bridge;
mod codex_bridge;
pub mod codex_projects;
pub mod config;
pub mod data_io;
mod env_path;
pub mod feedback;
mod google_oauth;
pub(crate) mod http_bridge;
pub mod ollama;
pub mod provider_auth;
pub mod run_history;
pub mod schedule_commands;
mod scheduler;
mod screenshot_flash;
pub mod secrets;
pub mod session_commands;
mod speed_access;
pub(crate) use browser_bridge::*;
mod paths;
pub(crate) use paths::*;
mod session_store;
pub(crate) use session_store::*;
mod provider_url;
pub(crate) use provider_url::*;
mod ssrf;
mod http_util;
pub(crate) use http_util::*;
pub(crate) mod codex_route;
pub(crate) use codex_route::{configure_codex_direct_provider, selected_ollama_codex_model};
pub(crate) mod spawn_supervisor;
pub(crate) mod task_runtime;
pub mod task_spec;

// Re-export commonly used types/helpers from submodules so remaining lib.rs
// code (session listing, run history, codex file-bridge) can use them unqualified.
use codex_bridge::start_bridge_file_watcher;
use codex_projects::newest_codex_session;
use http_bridge::{capitalize, start_webview_bridge_server};
use ollama::ollama_tags;
use screenshot_flash::show_screenshot_flash;

// H-PROC-1: the legacy `RunningTask`/`TaskState` managed state was removed.
// Nothing ever wrote its pid/starting fields — all real task state lives in
// `task_runtime::TaskRuntime` — so every guard reading it was dead code.

#[derive(Clone, Debug)]
struct WebviewBridgeConfig {
    base_url: String,
    token: String,
    /// F-D-4: a SEPARATE, capability-scoped token that is the only token ever
    /// injected into a third-party provider webview (the auth-probe JS). Unlike
    /// `token` (which authorizes every bridge route, incl. `/v1/task` and
    /// `/v1/open`), this token authorizes ONLY `POST /v1/callback`, so a
    /// compromised/monkeypatched provider page that steals it cannot spawn a
    /// local agent run or drive the SSRF-capable open route.
    webview_token: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeAttachment {
    #[serde(default)]
    name: Option<String>,
    #[serde(default = "default_attachment_mime")]
    mime_type: String,
    data_base64: String,
}

fn default_attachment_mime() -> String {
    "application/octet-stream".to_string()
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeCompleteRequest {
    site: Option<String>,
    prompt: String,
    #[serde(default)]
    attachments: Vec<BridgeAttachment>,
    #[serde(default)]
    completion_hash: Option<String>,
    /// When true, navigate the provider webview to a fresh chat page before
    /// injecting the prompt. BrowserProvider uses this for Codex bridge relays
    /// because each relay prompt already contains the full Codex conversation;
    /// keeping the provider page history can make the scraper read stale bubbles.
    #[serde(default)]
    clear_chat: bool,
    /// Optional authuser index for Google multi-account routing.
    /// When set, the browser window navigates to gemini.google.com?authuser=N
    /// before injecting the prompt.
    #[serde(default)]
    authuser: Option<u32>,
    /// Optional model tier (e.g. "fast", "pro", "thinking") requested by the user.
    #[serde(default)]
    model_tier: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeOpenRequest {
    url: String,
    provider_name: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeCompleteResponse {
    ok: bool,
    response: Option<String>,
    error: Option<String>,
    site: Option<String>,
    /// How many attachments bridge.js verified as actually uploaded (chip
    /// appeared in the composer). Forwarded to Python via /v1/result so
    /// screenshot-honesty gating can tell "image delivered" from "image
    /// silently dropped". `None` on payloads from older bridge scripts.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    attachments_uploaded: Option<u64>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeCallbackRequest {
    req_id: String,
    payload: BridgeCompleteResponse,
}

/// IPC event payload sent from the persistent JS bridge via Tauri emit.
#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeIpcEvent {
    event: String, // "sent" | "done" | "error" | "progress"
    req_id: String,
    #[serde(default)]
    response: Option<String>,
    #[serde(default)]
    error: Option<String>,
    #[serde(default)]
    site: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    level: Option<String>,
    #[serde(default)]
    msg: Option<String>,
    /// Verified upload count from the bridge's `done` payload (see
    /// `BridgeCompleteResponse::attachments_uploaded`).
    #[serde(default)]
    attachments_uploaded: Option<u64>,
}

static IPC_LISTENER_REGISTERED: OnceLock<()> = OnceLock::new();

static WEBVIEW_BRIDGE_CFG: OnceLock<WebviewBridgeConfig> = OnceLock::new();
static WEBVIEW_BRIDGE_LOCK: OnceLock<StdMutex<()>> = OnceLock::new();
static WEBVIEW_BRIDGE_REQ_COUNTER: AtomicU64 = AtomicU64::new(1);
static WEBVIEW_BRIDGE_RESULTS: OnceLock<StdMutex<HashMap<String, BridgeCompleteResponse>>> =
    OnceLock::new();
static WEBVIEW_BRIDGE_PROGRESS: OnceLock<StdMutex<HashMap<String, String>>> = OnceLock::new();
/// Condvar notified whenever a result is inserted into WEBVIEW_BRIDGE_RESULTS.
/// Collectors wait on this instead of polling every 150ms.
static WEBVIEW_BRIDGE_NOTIFY: OnceLock<(StdMutex<()>, Condvar)> = OnceLock::new();
/// Condvar signalled by the /v1/callback handler once the bridge.js initialization
/// script has reported readiness (flag set to true).  The nav-wait in
/// `clear_provider_webview_chat` uses this condvar so it can wake early rather
/// than always sleeping for the full 3 500 ms (#10).
static WEBVIEW_NAV_READY: OnceLock<(StdMutex<bool>, Condvar)> = OnceLock::new();
/// Tracks whether the browser window was hidden before a specific /v1/send request, so /v1/result knows to hide it after.
static WEBVIEW_WAS_HIDDEN: OnceLock<StdMutex<std::collections::HashSet<String>>> = OnceLock::new();
/// AUDIT FIX #3: first-seen timestamp for every bridge req_id tracked in
/// WEBVIEW_BRIDGE_RESULTS/PROGRESS/WAS_HIDDEN. A caller that dies (task
/// force-killed, HTTP client crashed) before ever polling /v1/result leaves
/// its entries in those maps forever, since the only cleanup paths today
/// run inside `collect_bridge_payload` (on success or its own timeout) --
/// which never runs unless something actually calls /v1/result. This map
/// backs a periodic sweep (`browser_bridge::sweep_stale_bridge_entries`,
/// started by `browser_bridge::start_bridge_gc_sweeper`) that evicts
/// orphaned entries after a bound (default 15 min) regardless of whether
/// anyone ever polls for them.
static WEBVIEW_BRIDGE_ENTRY_TIMES: OnceLock<StdMutex<HashMap<String, std::time::Instant>>> =
    OnceLock::new();
/// Debug/testing mode: keep the provider webview visible while sending.
static WEBVIEW_KEEP_VISIBLE: OnceLock<StdMutex<bool>> = OnceLock::new();
/// The site selected via /v1/provider, to be passed to the next agent spawn.
static KIM_PREFERRED_SITE: OnceLock<StdMutex<Option<String>>> = OnceLock::new();
/// Last Gemini authuser index intentionally loaded in the in-app browser.
static WEBVIEW_LAST_GEMINI_AUTHUSER: OnceLock<StdMutex<Option<u32>>> = OnceLock::new();
/// Child handle for the CDP Chrome process spawned by launch_chrome_for_cdp (#8).
/// Stored so it can be killed on app shutdown rather than leaking as a zombie.
static CDP_CHROME_CHILD: OnceLock<StdMutex<Option<std::process::Child>>> = OnceLock::new();

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SessionInfo {
    pub session_id: String,
    pub session_key: String,
    pub title: String,
    pub date: String,
    pub message_count: usize,
    pub has_summary: bool,
    pub summary: Option<String>,
    pub session_type: String, // "kim" or "codex"
    /// K4: user pin (from the `.meta.json` sidecar). Pinned float to top.
    #[serde(default)]
    pub pinned: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_threads: Option<HashMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_last_site: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_threads_updated_at_ms: Option<u64>,
    /// Full composer provider for this session, e.g. `browser:gemini`, `claude`, `gemini`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_llm_provider: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct CompletedCodexSession {
    pub session_id: String,
    pub session_key: String,
    pub title: String,
    pub date: String,
    pub message_count: usize,
    pub has_summary: bool,
    pub summary: Option<String>,
    pub session_type: String,
    pub project_path: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct BrowserSessionMeta {
    #[serde(default)]
    pub browser_threads: HashMap<String, String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_last_site: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub browser_threads_updated_at_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_llm_provider: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct BrowserRestoreResult {
    pub restored: bool,
    pub site: String,
    pub url: String,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct KimMessage {
    pub role: String,
    pub content: serde_json::Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<serde_json::Value>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn command_exists(cmd: &str) -> bool {
    std::process::Command::new(cmd)
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .is_ok()
}

fn clear_provider_webview_chat(
    window: &tauri::WebviewWindow,
    site: &str,
    authuser: Option<u32>,
) -> Result<(), String> {
    let target_url = fresh_site_url(site, authuser);
    let js_url = serde_json::to_string(&target_url).map_err(|e| e.to_string())?;
    window
        .eval(format!("window.location.href = {};", js_url))
        .map_err(|e| e.to_string())?;

    // Wait for the provider SPA and the initialization_script-backed Kim bridge
    // to install before the next eval calls window.__kimBridge.send(...).
    // Uses a condvar so a future signal from the bridge readiness callback can
    // wake this thread early; falls back to 3 500 ms max timeout (#10).
    {
        let (lock, cvar) = WEBVIEW_NAV_READY.get_or_init(|| (StdMutex::new(false), Condvar::new()));
        if let Ok(mut ready) = lock.lock() {
            *ready = false; // reset for this navigation
            let _result = cvar.wait_timeout_while(ready, Duration::from_millis(3500), |r| !*r);
        }
    }

    if normalize_site(site) == "gemini" {
        if let Ok(mut guard) = WEBVIEW_LAST_GEMINI_AUTHUSER
            .get_or_init(|| StdMutex::new(None))
            .lock()
        {
            *guard = authuser;
        }
    }

    Ok(())
}

async fn is_bridge_task_running_async() -> bool {
    let mut rt = crate::task_runtime::task_runtime().lock().await;
    match rt.pid {
        Some(pid) if process_exists(pid) => true,
        Some(_) => {
            rt.clear();
            false
        }
        None => false,
    }
}

fn is_bridge_task_running() -> bool {
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::task::block_in_place(|| {
            tauri::async_runtime::block_on(is_bridge_task_running_async())
        })
    } else {
        tauri::async_runtime::block_on(is_bridge_task_running_async())
    }
}

fn should_keep_browser_visible() -> bool {
    WEBVIEW_KEEP_VISIBLE
        .get_or_init(|| StdMutex::new(false))
        .lock()
        .map(|guard| *guard)
        .unwrap_or(false)
}

fn webview_current_href(window: &tauri::WebviewWindow) -> String {
    if let Ok(url) = window.url() {
        let current = url.to_string();
        if !current.is_empty() {
            return current;
        }
    }

    let _ = window.eval("document.title = '__KIM_HREF__' + String(window.location.href);");
    std::thread::sleep(Duration::from_millis(100));
    window
        .title()
        .ok()
        .and_then(|title| title.strip_prefix("__KIM_HREF__").map(str::to_string))
        .unwrap_or_default()
}

fn gemini_url_has_conversation_path(raw_url: &str) -> bool {
    let Ok(url) = tauri::Url::parse(raw_url) else {
        return false;
    };
    let host = url.host_str().unwrap_or_default().to_ascii_lowercase();
    if host != "gemini.google.com" {
        return false;
    }
    let segments: Vec<&str> = url
        .path_segments()
        .map(|s| s.filter(|seg| !seg.is_empty()).collect())
        .unwrap_or_else(Vec::new);
    for i in 0..segments.len() {
        if segments[i] == "app" {
            return segments.get(i + 1).is_some_and(|next| !next.is_empty());
        }
    }
    false
}

fn prepare_gemini_webview(window: &tauri::WebviewWindow, authuser: Option<u32>, force: bool) {
    let current_url = webview_current_href(window);
    let task_running = is_bridge_task_running();

    if task_running {
        // Preserve the exact provider URL during active tasks.
        // Do not rewrite /app/<chat-id> to /app?authuser=N.
        return;
    }

    let selected_changed = WEBVIEW_LAST_GEMINI_AUTHUSER
        .get_or_init(|| StdMutex::new(None))
        .lock()
        .map(|guard| *guard != authuser)
        .unwrap_or(true);
    let has_thread_path = gemini_url_has_conversation_path(&current_url);
    let missing_selected_authuser = authuser.is_some_and(|index| {
        current_url.contains("gemini.google.com")
            && !current_url.contains(&format!("authuser={index}"))
            && !has_thread_path
    });
    let wrong_page = current_url.is_empty()
        || !current_url.contains("gemini.google.com")
        || current_url.contains("accounts.google.com")
        || current_url.contains("signin")
        || current_url.contains("ServiceLogin")
        || current_url.contains("SignOutOptions");

    if force || selected_changed || missing_selected_authuser || wrong_page {
        let target_url = gemini_site_url(authuser);
        if let Ok(js_url) = serde_json::to_string(&target_url) {
            let _ = window.eval(format!("window.location.href = {};", js_url));
            // Same condvar-based wait as clear_provider_webview_chat (#10):
            // wakes early on bridge readiness, falls back to 3 500 ms.
            let (lock, cvar) =
                WEBVIEW_NAV_READY.get_or_init(|| (StdMutex::new(false), Condvar::new()));
            if let Ok(mut ready) = lock.lock() {
                *ready = false;
                let _result = cvar.wait_timeout_while(ready, Duration::from_millis(3500), |r| !*r);
            }
        }
        if let Ok(mut guard) = WEBVIEW_LAST_GEMINI_AUTHUSER
            .get_or_init(|| StdMutex::new(None))
            .lock()
        {
            *guard = authuser;
        }
    }
}

/// Tracks the user's last known non-Kim frontmost app, so that even after
/// the first send has already stolen focus to Kim, subsequent sends can
/// still restore the user's actual target app.
#[cfg(target_os = "macos")]
static USER_FRONTMOST_APP: OnceLock<StdMutex<Option<String>>> = OnceLock::new();

/// Capture the current frontmost app. If it's Kim, fall back to the last
/// remembered user app. Always updates the cache when a non-Kim app is
/// observed so we keep tracking the user's actual target.
#[cfg(target_os = "macos")]
fn save_frontmost_app() -> Option<String> {
    let cache = USER_FRONTMOST_APP.get_or_init(|| StdMutex::new(None));

    let output = std::process::Command::new("osascript")
        .arg("-e")
        .arg(r#"tell application "System Events" to return bundle identifier of first application process whose frontmost is true"#)
        .output();

    if let Ok(out) = output {
        if out.status.success() {
            let bundle_id = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !bundle_id.is_empty() && bundle_id != "com.kim.desktop" {
                if let Ok(mut guard) = cache.lock() {
                    *guard = Some(bundle_id.clone());
                }
                return Some(bundle_id);
            }
        }
    }

    // Frontmost is Kim (or query failed). Use the last user-frontmost we saw.
    cache.lock().ok().and_then(|g| g.clone())
}

/// Reactivate the saved frontmost app, scheduled in a background thread to run
/// after the bridge JS has had a chance to inject focus into the offscreen
/// webview. This is what undoes Stage Manager swapping groups when the
/// hidden Tauri webview steals key-window status during prompt injection.
#[cfg(target_os = "macos")]
fn schedule_frontmost_restore(bundle_id: String) {
    std::thread::spawn(move || {
        // Two restores: one early (catches inputEl.focus()), one late (catches
        // the send-button click and any post-paste focus events).
        for delay_ms in [400u64, 1500u64] {
            std::thread::sleep(Duration::from_millis(delay_ms));
            let script = format!(
                r#"tell application id "{}" to activate"#,
                bundle_id.replace('"', "")
            );
            let _ = std::process::Command::new("osascript")
                .arg("-e")
                .arg(&script)
                .status();
        }
    });
}

#[cfg(not(target_os = "macos"))]
fn save_frontmost_app() -> Option<String> {
    None
}

#[cfg(not(target_os = "macos"))]
fn schedule_frontmost_restore(_bundle_id: String) {}

/// Write the first PNG attachment to the macOS system clipboard via osascript.
///
/// This allows the bridge JS to call `document.execCommand('paste')` inside
/// the WKWebView, generating a *trusted* paste event (isTrusted: true) that
/// Gemini's editor accepts — unlike synthetic ClipboardEvent which is always
/// rejected (isTrusted: false).
///
/// Non-blocking best-effort: errors are logged but never propagate.
#[cfg(target_os = "macos")]
fn write_first_png_to_clipboard(attachments: &[BridgeAttachment]) -> bool {
    let att = match attachments.iter().find(|a| a.mime_type == "image/png") {
        Some(a) => a,
        None => return false,
    };

    let bytes = match base64::engine::general_purpose::STANDARD.decode(&att.data_base64) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[Kim] clipboard: base64 decode failed: {}", e);
            return false;
        }
    };

    // Use a random hex suffix for the temp file name rather than a guessable PID
    // so a malicious process can't race and swap in a symlink (#23/#4).
    // We use std::fs::OpenOptions with O_CREAT|O_EXCL mode 0600 (via permissions)
    // instead of the `tempfile` dev-dependency which isn't available in production code.
    let rand_hex: String = {
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut h = DefaultHasher::new();
        (std::time::SystemTime::now(), std::process::id()).hash(&mut h);
        format!("{:016x}", h.finish())
    };
    let temp_path = format!(
        "{}/kim_clip_{}.png",
        std::env::temp_dir().display(),
        rand_hex
    );
    // Set permissions to 0600 immediately after creation.
    {
        use std::io::Write;
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create_new(true); // O_CREAT | O_EXCL
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        match opts.open(&temp_path) {
            Ok(mut f) => {
                if let Err(e) = f.write_all(&bytes) {
                    eprintln!("[Kim] clipboard: temp write failed: {}", e);
                    let _ = std::fs::remove_file(&temp_path);
                    return false;
                }
            }
            Err(e) => {
                eprintln!("[Kim] clipboard: temp open failed: {}", e);
                return false;
            }
        }
    }

    // class PNGf is the four-char AppleScript type for PNG data.
    let script = format!(
        "set the clipboard to (read (POSIX file \"{}\") as \u{00AB}class PNGf\u{00BB})",
        temp_path
    );
    let ok = std::process::Command::new("osascript")
        .arg("-e")
        .arg(&script)
        .status()
        .map(|s| s.success())
        .unwrap_or(false);

    let _ = std::fs::remove_file(&temp_path);

    if !ok {
        eprintln!("[Kim] clipboard: osascript failed (non-fatal)");
    }
    ok
}

#[cfg(not(target_os = "macos"))]
fn write_first_png_to_clipboard(_attachments: &[BridgeAttachment]) -> bool {
    false
}

/// Read the current macOS clipboard as text (empty string if it holds no text).
#[cfg(target_os = "macos")]
fn read_clipboard_text() -> Option<String> {
    std::process::Command::new("pbpaste")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
}

/// Copy `text` to the macOS clipboard via pbcopy. Returns true on success.
#[cfg(target_os = "macos")]
fn pbcopy_text(text: &str) -> bool {
    let mut child = match std::process::Command::new("pbcopy")
        .stdin(std::process::Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => {
            eprintln!("[Kim] clipboard: pbcopy spawn failed: {}", e);
            return false;
        }
    };
    let wrote = child
        .stdin
        .take()
        .map(|mut stdin| {
            use std::io::Write;
            stdin.write_all(text.as_bytes()).is_ok()
        })
        .unwrap_or(false);
    wrote && child.wait().map(|s| s.success()).unwrap_or(false)
}

/// Stage a text prompt on the macOS clipboard so the browser bridge can paste it
/// into the provider editor.
///
/// #44: the prompt (which may contain secrets/PII) used to be left on the system
/// clipboard indefinitely. We now snapshot the prior clipboard, and after the
/// paste has landed restore it — but only if the clipboard still holds *our*
/// injected prompt, so we never clobber something the user copied in the
/// meantime.
#[cfg(target_os = "macos")]
fn write_text_prompt_to_clipboard(prompt: &str) -> bool {
    let prior = read_clipboard_text();
    let ok = pbcopy_text(prompt);
    if !ok {
        eprintln!("[Kim] clipboard: pbcopy failed (non-fatal)");
        return false;
    }

    let prompt_owned = prompt.to_string();
    std::thread::spawn(move || {
        // Well after the bridge's paste (~100–500ms). Restore-if-unchanged.
        std::thread::sleep(Duration::from_secs(8));
        if read_clipboard_text().as_deref() == Some(prompt_owned.as_str()) {
            let _ = pbcopy_text(prior.as_deref().unwrap_or(""));
        }
    });
    ok
}

#[cfg(not(target_os = "macos"))]
fn write_text_prompt_to_clipboard(_prompt: &str) -> bool {
    false
}

// ---------------------------------------------------------------------------
// Persistent JS bridge — injected ONCE via initialization_script.
//
// This replaces the previous approach of re-evaluating a ~30KB JS script on
// every single request.  The bridge is injected at page load time and persists
// across SPA navigations.  It re-injects automatically on full page loads.
//
// Communication:
//   JS  →  Rust:  window.__TAURI_INTERNALS__.invoke() / emit()
//   Rust → JS:    window.__kimBridge.send(prompt, reqId, site, attachments)
//
// The bridge emits three event types via Tauri IPC:
//   "sent"  — prompt was injected and Enter pressed (~150ms)
//   "done"  — generation complete, full response text attached
//   "error" — something went wrong
// ---------------------------------------------------------------------------

/// Hide the Kim browser window without triggering WKWebView's background-tab
/// JavaScript suspension on macOS. Raw `win.hide()` causes WKWebView to throttle
/// or pause `setTimeout`/`requestAnimationFrame` and synthetic DOM events, which
/// breaks the bridge JS mid-poll on multi-turn conversations.
///
/// Instead we strip decorations, shrink to 0x0, and move off-screen. The window
/// remains technically "visible" so JS keeps running at full speed, but is
/// invisible to the user. `show_browser_window_impl` restores it.
fn hide_browser_window_offscreen(win: &tauri::WebviewWindow) {
    let _ = win.set_decorations(false);
    // Use 1024x768 — the page MUST have a real viewport for layout to work.
    // - 0x0 makes WKWebView throttle/suspend JS (setTimeout, RAF, DOM events).
    // - 1x1 keeps JS alive but collapses CSS layout: elements like Gemini's
    //   <rich-textarea> get offsetParent=null, so isVisible() returns false,
    //   findGeminiInput()/findElement({visible:true}) return null, and the
    //   bridge throws "Could not find input selector" → headless prompts
    //   silently fail.
    // - 1024x768 gives the page enough room to compute a valid layout while
    //   staying entirely off-screen at (-10000, -10000). Invisible to the
    //   user, fully functional for the bridge.
    let _ = win.set_size(tauri::PhysicalSize::new(1024, 768));
    let _ = win.set_position(tauri::PhysicalPosition::new(-10000, -10000));
}

/// Detect whether the browser window has been moved off-screen by
/// `hide_browser_window_offscreen`.  `is_visible()` always returns true
/// because we never call `win.hide()`, so we check position/size instead.
fn is_browser_window_offscreen(win: &tauri::WebviewWindow) -> bool {
    let pos = win
        .outer_position()
        .unwrap_or(tauri::PhysicalPosition::new(0, 0));
    let size = win
        .outer_size()
        .unwrap_or(tauri::PhysicalSize::new(100, 100));
    pos.x <= -9000 || pos.y <= -9000 || (size.width <= 1 && size.height <= 1)
}

fn show_browser_window_impl(app_handle: &tauri::AppHandle) {
    let label = "kim-browser-signin";
    if let Some(win) = app_handle.get_webview_window(label) {
        let _ = win.set_decorations(true);
        if let Some(monitor) = win.current_monitor().unwrap_or(None) {
            let monitor_size = monitor.size();
            let width = 1280;
            let height = 860;
            let pos = monitor.position();
            let x = pos.x + (monitor_size.width as i32 - width) / 2;
            let y = pos.y + (monitor_size.height as i32 - height) / 2;
            let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
            let _ = win.set_size(tauri::PhysicalSize::new(width as u32, height as u32));
        } else {
            // Fallback if no monitor found
            let _ = win.set_position(tauri::PhysicalPosition::new(100, 100));
            let _ = win.set_size(tauri::PhysicalSize::new(1280, 860));
        }
        let _ = win.show();
        let _ = win.set_focus();
    }
}

#[tauri::command]
async fn show_browser_window(app_handle: tauri::AppHandle) -> Result<(), String> {
    if app_handle
        .get_webview_window("kim-browser-signin")
        .is_some()
    {
        show_browser_window_impl(&app_handle);
        Ok(())
    } else {
        Err("No Kim browser window is open yet. Open a browser provider first.".to_string())
    }
}

pub(crate) mod window_manager;
pub(crate) use window_manager::{set_task_active_mode, show_main_window};
pub(crate) mod updater;

async fn session_browser_meta_read(
    session_id: String,
    session_date: Option<String>,
    session_type: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, codex_dir);
    let date_dir = resolve_session_date_dir(&base, &session_id, session_date.as_deref())?;
    Ok(read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default())
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn session_browser_meta_write(
    session_id: String,
    session_date: Option<String>,
    session_type: Option<String>,
    site: Option<String>,
    url: Option<String>,
    browser_last_site: Option<String>,
    last_llm_provider: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, codex_dir);
    let date_dir = resolve_session_date_dir(&base, &session_id, session_date.as_deref())?;
    // M-STORE-1: read-modify-write under the process-wide meta lock so a
    // concurrent bridge commit can't discard this write's fields.
    update_browser_session_meta(&date_dir, &session_id, |meta| {
        apply_browser_meta_writes(meta, browser_last_site, site, url, last_llm_provider)
    })
}

#[tauri::command]
async fn session_browser_url_commit(
    session_id: String,
    session_date: Option<String>,
    session_type: Option<String>,
    preferred_site: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let Some(win) = app_handle.get_webview_window("kim-browser-signin") else {
        return session_browser_meta_read(
            session_id,
            session_date,
            session_type,
            kim_dir,
            codex_dir,
        )
        .await;
    };
    let current_url = webview_current_href(&win);
    let site = preferred_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
        .or_else(|| browser_url_site(&current_url))
        .unwrap_or_else(|| "claude".to_string());

    if browser_url_is_bad_for_commit(&current_url, &site) {
        // Preserve good previous metadata. Generic homes, login pages, and
        // provider auth redirects must never overwrite the last useful thread.
        let stype = session_type.clone().unwrap_or_else(|| "kim".to_string());
        let base = session_base_dir(&stype, kim_dir, codex_dir);
        let date_dir = resolve_session_date_dir(&base, &session_id, session_date.as_deref())?;
        // M-STORE-1: read-modify-write under the process-wide meta lock.
        let meta = update_browser_session_meta(&date_dir, &session_id, |meta| {
            if meta.browser_last_site.as_deref() != Some(site.as_str()) {
                meta.browser_last_site = Some(site.clone());
                meta.browser_threads_updated_at_ms = Some(now_ms());
            }
            Ok(())
        })?;
        return Ok(meta);
    }

    session_browser_meta_write(
        session_id,
        session_date,
        session_type,
        Some(site),
        Some(current_url),
        None,
        None,
        kim_dir,
        codex_dir,
    )
    .await
}

#[tauri::command]
async fn restore_browser_for_session(
    session_id: String,
    session_date: Option<String>,
    session_type: Option<String>,
    preferred_site: Option<String>,
    kim_dir: Option<String>,
    codex_dir: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<BrowserRestoreResult, String> {
    validate_session_id(&session_id)?;
    if is_bridge_task_running_async().await {
        return Err("Cannot restore provider browser while Kim is running a task.".to_string());
    }

    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, codex_dir);
    let date_dir = resolve_session_date_dir(&base, &session_id, session_date.as_deref())?;
    let meta = read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();

    let site = preferred_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
        .or(meta.browser_last_site.clone())
        .unwrap_or_else(|| "claude".to_string());

    let mut restored = false;
    let mut reason = "fallback_home".to_string();
    let target = if let Some(saved) = meta.browser_threads.get(&site) {
        if browser_url_allowed_for_restore(saved, &site) {
            restored = true;
            reason = "stored_thread".to_string();
            saved.clone()
        } else {
            fresh_site_url(&site, None)
        }
    } else {
        fresh_site_url(&site, None)
    };

    let provider_name = Some(format!("{} (session)", capitalize(&site)));
    // Restore the webview offscreen — selecting a chat in the sidebar should
    // never pop up a Chrome window. The webview is still created (or its URL
    // updated) so cookies hydrate and the bridge is ready when the user sends
    // their first message, but it stays invisible until the user explicitly
    // clicks the "Sign in" / "Show provider" affordance.
    open_browser_signin_window_with_visibility(&target, provider_name, false, &app_handle)?;

    let message = if restored {
        Some("Restored the saved browser conversation for this session.".to_string())
    } else if meta.browser_threads.contains_key(&site) {
        Some(
            "Saved browser URL was no longer safe/valid, so Kim opened a fresh provider page."
                .to_string(),
        )
    } else {
        Some(
            "No saved browser conversation for this provider; opened the provider start page."
                .to_string(),
        )
    };

    Ok(BrowserRestoreResult {
        restored,
        site,
        url: target,
        reason,
        message,
    })
}

/// Try to start Chrome/Chromium with remote debugging enabled on port 9222.
/// Uses the same user-data dir as Python's BrowserProvider: `<project>/sessions/chrome_data`.
/// If port 9222 is already open, does not spawn (avoids a new Chrome window each task).
/// Probes common install locations on each platform.
///
/// Returns `Ok(true)` if Chrome was freshly spawned (caller should wait ~2 s for the debug
/// port to open), `Ok(false)` if it was already running, or `Err` if not found.
///
/// Static Chrome flags for Kim's CDP launch.
///
/// F-I-4: `--remote-debugging-address=127.0.0.1` pins the (unauthenticated)
/// DevTools port to the loopback interface so it can never bind `0.0.0.0` and
/// expose the logged-in provider profile to the LAN. The port itself is still
/// 9222 by default; moving it to a random port and coordinating the Python
/// connect side is Team B's provider half (handoff below).
fn cdp_static_flags() -> [&'static str; 3] {
    [
        // Loopback-only bind for the CDP endpoint (F-I-4).
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        // --disable-popup-blocking intentionally NOT set: it weakens browser
        // security (#3).
    ]
}

/// NOTE: this function must only be called from a blocking context (e.g. inside
/// `tokio::task::spawn_blocking`) because `TcpStream::connect` and `fs` calls are
/// synchronous.  Do NOT call it directly from an async Tokio task.
fn launch_chrome_for_cdp(project_root: &Path) -> Result<bool, String> {
    use std::net::TcpStream;
    use std::process::Command as StdCommand;

    // Honour KIM_REAL_BROWSER_CDP_PORT so the Rust launcher matches the Python
    // side (web.py already reads this variable).  Default: 9222.
    let cdp_port: u16 = std::env::var("KIM_REAL_BROWSER_CDP_PORT")
        .ok()
        .and_then(|v| v.trim().parse::<u16>().ok())
        .filter(|&p| p != 0)
        .unwrap_or(9222);

    let port_open = TcpStream::connect(format!("127.0.0.1:{cdp_port}")).is_ok();
    if port_open {
        return Ok(false); // already running, no wait needed
    }

    let user_data_dir = project_root.join("sessions").join("chrome_data");
    let _ = fs::create_dir_all(&user_data_dir);
    let user_data_str = user_data_dir.to_string_lossy().into_owned();

    #[cfg(target_os = "macos")]
    let candidates: &[&str] = &[
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    ];
    #[cfg(target_os = "linux")]
    let candidates: &[&str] = &[
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
    ];
    #[cfg(target_os = "windows")]
    let candidates: &[&str] = &[
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ];
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    let candidates: &[&str] = &[];

    for chrome in candidates {
        let user_data_arg = format!("--user-data-dir={}", user_data_str);
        let cdp_port_arg = format!("--remote-debugging-port={cdp_port}");
        let result = StdCommand::new(chrome)
            .arg(user_data_arg.as_str())
            .arg(cdp_port_arg.as_str())
            .args(cdp_static_flags())
            .spawn();
        if let Ok(child) = result {
            // Store the child handle so it can be killed on app exit (#8).
            // Without this, Chrome processes accumulate as zombies.
            if let Ok(mut guard) = CDP_CHROME_CHILD.get_or_init(|| StdMutex::new(None)).lock() {
                // M-PROC-4: reap any previous child before overwriting the
                // slot. Dropping a Child without kill()+wait() leaves either
                // an orphaned live Chrome or a zombie for the app's lifetime
                // (e.g. when the first spawn never opened the debug port).
                if let Some(mut old) = guard.take() {
                    match old.try_wait() {
                        Ok(Some(_)) => {} // already exited and now reaped
                        _ => {
                            let _ = old.kill();
                            let _ = old.wait();
                        }
                    }
                }
                *guard = Some(child);
            }
            // Caller is responsible for the post-launch wait so it can use
            // tokio::time::sleep instead of std::thread::sleep.
            return Ok(true); // freshly spawned — caller must wait for port
        }
    }
    Err("Chrome/Chromium not found. Install Google Chrome to use the browser provider.".to_string())
}

/// Kill the CDP Chrome child on app shutdown (#8).
pub(crate) fn kill_cdp_chrome() {
    if let Some(guard) = CDP_CHROME_CHILD.get() {
        if let Ok(mut child_opt) = guard.lock() {
            if let Some(mut child) = child_opt.take() {
                let _ = child.kill();
                // M-PROC-4: reap after kill so the process doesn't linger as
                // a zombie until our own exit.
                let _ = child.wait();
            }
        }
    }
}

/// Read a single value from `<kim_root>/.env` (best-effort, no dependency).
/// Real env vars take priority over file values; `codex` and `python` already
/// inherit the parent process env, so this only acts as a fallback.
fn read_env_file_var(kim_root: &Path, key: &str) -> Option<String> {
    if let Ok(v) = std::env::var(key) {
        if !v.trim().is_empty() {
            return Some(v);
        }
    }
    let path = kim_root.join(".env");
    let content = std::fs::read_to_string(&path).ok()?;
    let prefix = format!("{}=", key);
    for raw in content.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        if let Some(rest) = line.strip_prefix(&prefix) {
            let mut v = rest.trim().to_string();
            if (v.starts_with('"') && v.ends_with('"') && v.len() >= 2)
                || (v.starts_with('\'') && v.ends_with('\'') && v.len() >= 2)
            {
                v = v[1..v.len() - 1].to_string();
            }
            if !v.is_empty() {
                return Some(v);
            }
        }
    }
    None
}

fn read_first_env_file_var(kim_root: &Path, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| read_env_file_var(kim_root, key))
}

fn ollama_openai_base_url(base_url: Option<&str>) -> String {
    let base = base_url
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("http://localhost:11434")
        .trim_end_matches('/');
    if base.ends_with("/v1") {
        base.to_string()
    } else {
        format!("{}/v1", base)
    }
}

pub(crate) mod hitl;
pub(crate) use hitl::hitl_respond_approval;
pub(crate) use hitl::respond_approval_decision;
pub(crate) use hitl::respond_user_input;
pub(crate) mod subprocess;
pub(crate) use subprocess::{
    cancel_task, find_python_interpreter, process_exists, send_task, steer_task,
};

// ---------------------------------------------------------------------------
// Account — ~/.config/kim/account.json (platform-native config dir)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Ollama — local daemon status, sign-in launcher, model management
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Codex (Code) projects — grouped by project directory + git branch
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // #23: repair the minimal launchd PATH before anything shells out —
    // packaged GUI launches otherwise can't find ollama/git/etc.
    env_path::fix_gui_path();
    let schedule_timer_state = schedule_commands::new_schedule_timer_state();
    let config_path = config_yaml_path(None);
    let config = config::load_config(&config_path);

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        // K2: global-shortcut plugin — Alt+Space toggles the quick-ask window.
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    use tauri_plugin_global_shortcut::ShortcutState;
                    if event.state() == ShortcutState::Pressed {
                        speed_access::toggle_quick_ask(app);
                    }
                })
                .build(),
        )
        .setup(|app| {
            // K2/K7: register the quick-ask shortcut and build the system tray.
            speed_access::register_quick_ask_shortcut(app.handle());
            if let Err(e) = speed_access::build_tray(app.handle()) {
                eprintln!("[Kim] tray init failed: {e}");
            }
            #[cfg(target_os = "macos")]
            {
                use tauri::window::{Effect, EffectState, EffectsBuilder};

                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.set_effects(
                        EffectsBuilder::new()
                            .effect(Effect::WindowBackground)
                            .state(EffectState::Active)
                            .build(),
                    );
                }
            }

            if let Err(e) = start_webview_bridge_server(app.handle().clone()) {
                eprintln!("[Kim] Failed to start in-app browser bridge: {}", e);
            }
            start_bridge_file_watcher(app.handle().clone());
            // D6: start the 60s in-app scheduler tick loop.
            scheduler::start_scheduler(app.handle().clone());
            // AUDIT FIX #3: periodic sweep evicting orphaned bridge
            // result/progress entries left behind when a caller dies before
            // polling /v1/result.
            browser_bridge::start_bridge_gc_sweeper(app.handle().clone());
            Ok(())
        })
        .manage(schedule_timer_state)
        .manage(config)
        .invoke_handler(tauri::generate_handler![
            session_commands::list_sessions,
            session_commands::load_session_messages,
            session_commands::summarize_session,
            run_history::save_run_history,
            run_history::load_run_history,
            session_commands::get_app_version,
            session_commands::reveal_logs,
            session_commands::set_privacy_pause,
            session_commands::get_privacy_pause,
            run_history::get_platform_info,
            run_history::run_update,
            browser_bridge::open_browser_signin_window,
            session_browser_meta_write,
            session_browser_url_commit,
            restore_browser_for_session,
            show_browser_window,
            provider_auth::provider_check_auth,
            provider_auth::provider_signin,
            provider_auth::provider_signout,
            show_main_window,
            set_task_active_mode,
            send_task,
            cancel_task,
            hitl_respond_approval,
            respond_approval_decision,
            respond_user_input,
            steer_task,
            google_oauth::google_oauth_status,
            google_oauth::google_oauth_start,
            google_oauth::google_oauth_disconnect,
            google_oauth::google_oauth_test,
            account::load_account,
            account::save_account,
            account::reset_onboarding,
            account::delete_all_sessions,
            secrets::store_github_token,
            secrets::delete_github_token,
            ollama::ollama_get_status,
            ollama::ollama_test_model,
            ollama::ollama_signin,
            ollama::ollama_pull_model,
            data_io::verify_github_pat,
            data_io::export_data,
            data_io::import_data,
            data_io::backup_to_gist,
            data_io::restore_from_gist,
            codex_projects::list_codex_projects,
            codex_projects::open_in_finder,
            feedback::send_feedback,
            show_screenshot_flash,
            feedback::save_attachment,
            schedule_commands::list_scheduled_tasks,
            schedule_commands::add_scheduled_task,
            schedule_commands::update_scheduled_task,
            schedule_commands::delete_scheduled_task,
            schedule_commands::run_due_scheduled_task,
            schedule_commands::start_schedule_timer,
            schedule_commands::stop_schedule_timer,
            schedule_commands::get_schedule_timer_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|_app_handle, event| {
            // Kill the CDP Chrome child process on every exit path (#8) so we don't
            // leave a zombie browser consuming memory and holding the debug port.
            if let tauri::RunEvent::Exit = event {
                kill_cdp_chrome();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cdp_launch_flags_pin_loopback_bind() {
        // F-I-4: the CDP DevTools port is unauthenticated — the launch must
        // pin it to the loopback interface so it cannot bind 0.0.0.0.
        let flags = cdp_static_flags();
        assert!(
            flags.contains(&"--remote-debugging-address=127.0.0.1"),
            "CDP launch must bind the debug port to loopback only"
        );
        // The popup-blocking weakener stays OFF (#3).
        assert!(!flags.iter().any(|f| f.contains("disable-popup-blocking")));
    }

    // Helper: write lines to a temp file using O_EXCL-safe NamedTempFile (#23).
    // subsec_nanos() was guessable and racy — tempfile guarantees uniqueness + 0600 mode.
    fn write_temp_jsonl(lines: &[&str]) -> std::path::PathBuf {
        use std::io::Write as _;
        let mut f = tempfile::Builder::new()
            .prefix("kim_test_")
            .suffix(".jsonl")
            .tempfile()
            .expect("tempfile creation failed");
        f.write_all(lines.join("\n").as_bytes()).unwrap();
        // Persist so the test can open it by path; caller is responsible for cleanup.
        let (_, path) = f.keep().expect("tempfile persist failed");
        path
    }

    #[test]
    fn test_parse_jsonl_skips_trace_records_silently() {
        let path = write_temp_jsonl(&[
            r#"{"role":"user","content":"hello"}"#,
            r#"{"type":"run_started","task":"do x","ts":1234}"#,
            r#"{"type":"tool_call","tool":"shell","ts":1234}"#,
            r#"{"type":"llm_turn","provider":"claude","ts":1234}"#,
            r#"{"type":"run_checkpoint","iteration":1}"#,
            r#"{"type":"run_result","success":true}"#,
            r#"{"role":"assistant","content":"world"}"#,
        ]);
        let msgs = parse_jsonl(&path).unwrap();
        // Only the two role-bearing chat lines should be parsed.
        assert_eq!(
            msgs.len(),
            2,
            "trace records must be skipped, not treated as errors"
        );
        assert_eq!(msgs[0].role, "user");
        assert_eq!(msgs[1].role, "assistant");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn test_parse_jsonl_still_warns_truly_malformed_lines() {
        // A truly malformed line (not valid JSON) should NOT be included in results.
        // We just verify parse_jsonl succeeds and omits the bad line.
        let path = write_temp_jsonl(&[
            r#"{"role":"user","content":"ok"}"#,
            r#"not json at all {"broken"#,
        ]);
        let msgs = parse_jsonl(&path).unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].role, "user");
        let _ = std::fs::remove_file(path);
    }

    // #22: build_bridge_complete_script was deleted; the persistent bridge.js
    // initialization_script handles all injection now. The old test that called this
    // deleted function has been removed to keep `cargo test` green.

    // -----------------------------------------------------------------------
    // normalize_site regression guards
    // -----------------------------------------------------------------------

    #[test]
    fn normalize_site_deepseek_browser() {
        // Code-tab promotion: "deepseek-browser" must map to "deepseek".
        assert_eq!(normalize_site("deepseek-browser"), "deepseek");
    }

    #[test]
    fn normalize_site_default() {
        // Empty string falls back to "claude".
        assert_eq!(normalize_site(""), "claude");
        // Aliases: openai and gpt both map to chatgpt.
        assert_eq!(normalize_site("openai"), "chatgpt");
        assert_eq!(normalize_site("gpt"), "chatgpt");
        // Alias: google maps to gemini.
        assert_eq!(normalize_site("google"), "gemini");
        // Canonical names pass through unchanged.
        assert_eq!(normalize_site("claude"), "claude");
        assert_eq!(normalize_site("chatgpt"), "chatgpt");
        assert_eq!(normalize_site("gemini"), "gemini");
        assert_eq!(normalize_site("deepseek"), "deepseek");
        assert_eq!(normalize_site("grok"), "grok");
    }

    // -----------------------------------------------------------------------
    // host_matches_site regression guards
    // -----------------------------------------------------------------------

    #[test]
    fn host_matches_site_grok_excludes_xcom() {
        // x.com is Twitter's root domain — must NOT match grok (#9).
        assert!(
            !host_matches_site("x.com", "grok"),
            "x.com must not match grok"
        );
        // www. prefix should be stripped, still not match.
        assert!(
            !host_matches_site("www.x.com", "grok"),
            "www.x.com must not match grok"
        );
        // Canonical Grok hosts must match.
        assert!(
            host_matches_site("grok.com", "grok"),
            "grok.com must match grok"
        );
        assert!(
            host_matches_site("grok.x.com", "grok"),
            "grok.x.com must match grok"
        );
    }

    #[test]
    fn host_matches_site_others() {
        // ChatGPT canonical hosts.
        assert!(host_matches_site("chatgpt.com", "chatgpt"));
        assert!(host_matches_site("chat.openai.com", "chatgpt"));
        // Gemini canonical host.
        assert!(host_matches_site("gemini.google.com", "gemini"));
        // DeepSeek canonical host.
        assert!(host_matches_site("chat.deepseek.com", "deepseek"));
        // Claude canonical host.
        assert!(host_matches_site("claude.ai", "claude"));
        // Aliases resolve correctly through host_matches_site (openai alias).
        assert!(host_matches_site("chatgpt.com", "openai"));
        // www. prefix stripping works for claude.
        assert!(host_matches_site("www.claude.ai", "claude"));
    }
}
