use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager};
use tiny_http::{Header, Request, Response, StatusCode};
use tokio::sync::Mutex;

mod google_oauth;
pub mod account;
pub mod codex_projects;
pub mod data_io;
pub mod feedback;
pub mod ollama;
pub mod relay;
pub mod run_history;
pub mod schedule_commands;
pub mod session_commands;
pub mod voice_config;
pub mod config;
pub(crate) mod http_bridge;
pub mod browser_bridge;
pub mod provider_auth;
pub(crate) use browser_bridge::*;

// Re-export commonly used types/helpers from submodules so remaining lib.rs
// code (session listing, run history, codex file-bridge) can use them unqualified.
use codex_projects::{mirror_latest_claw_session_to_codex, newest_codex_session};
use http_bridge::{capitalize, start_webview_bridge_server, start_bridge_file_watcher, show_screenshot_flash};
use ollama::ollama_tags;

// ---------------------------------------------------------------------------
// Shared state — currently running agent child (for cancellation)
// ---------------------------------------------------------------------------

#[derive(Default)]
pub struct RunningTask {
    /// PID of the running agent subprocess, if any.
    pid: Option<u32>,
    /// True while a task has reserved the runner slot but has not spawned yet.
    starting: bool,
}

pub type TaskState = Arc<Mutex<RunningTask>>;

#[derive(Clone, Debug)]
struct WebviewBridgeConfig {
    base_url: String,
    token: String,
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
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeCallbackRequest {
    req_id: String,
    payload: BridgeCompleteResponse,
}

/// IPC event payload sent from the persistent JS bridge via Tauri emit.
#[derive(Serialize, Deserialize, Clone, Debug)]
struct BridgeIpcEvent {
    event: String,     // "sent" | "done" | "error" | "progress"
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
}

static IPC_LISTENER_REGISTERED: OnceLock<()> = OnceLock::new();

static WEBVIEW_BRIDGE_CFG: OnceLock<WebviewBridgeConfig> = OnceLock::new();
static WEBVIEW_BRIDGE_LOCK: OnceLock<StdMutex<()>> = OnceLock::new();
static WEBVIEW_BRIDGE_REQ_COUNTER: AtomicU64 = AtomicU64::new(1);
static WEBVIEW_BRIDGE_RESULTS: OnceLock<StdMutex<HashMap<String, BridgeCompleteResponse>>> = OnceLock::new();
static WEBVIEW_BRIDGE_PROGRESS: OnceLock<StdMutex<HashMap<String, String>>> = OnceLock::new();
/// Condvar notified whenever a result is inserted into WEBVIEW_BRIDGE_RESULTS.
/// Collectors wait on this instead of polling every 150ms.
static WEBVIEW_BRIDGE_NOTIFY: OnceLock<(StdMutex<()>, Condvar)> = OnceLock::new();
/// Tracks whether the browser window was hidden before a specific /v1/send request, so /v1/result knows to hide it after.
static WEBVIEW_WAS_HIDDEN: OnceLock<StdMutex<std::collections::HashSet<String>>> = OnceLock::new();
/// Debug/testing mode: keep the provider webview visible while sending.
static WEBVIEW_KEEP_VISIBLE: OnceLock<StdMutex<bool>> = OnceLock::new();
/// PID of the currently-running agent subprocess, accessible from both the
/// sync bridge thread (/v1/task, /v1/cancel) and the async Tauri commands.
static BRIDGE_TASK_PID: OnceLock<StdMutex<Option<u32>>> = OnceLock::new();
/// Session ID of the currently-running agent task (set by /v1/task or send_task).
static BRIDGE_TASK_SESSION: OnceLock<StdMutex<Option<String>>> = OnceLock::new();
/// The site selected via /v1/provider, to be passed to the next agent spawn.
static KIM_PREFERRED_SITE: OnceLock<StdMutex<Option<String>>> = OnceLock::new();
/// Last Gemini authuser index intentionally loaded in the in-app browser.
static WEBVIEW_LAST_GEMINI_AUTHUSER: OnceLock<StdMutex<Option<u32>>> = OnceLock::new();

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

/// Ancestors of the current executable, used to locate an installed Kim
/// project root (`kim/` containing orchestrator/). This lets the packaged
/// desktop app find its sibling Python project without any hardcoded user
/// directories.
fn exe_ancestor_kim_root() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    for ancestor in exe.ancestors() {
        // Heuristic: an ancestor that contains `orchestrator/agent.py` is
        // a valid Kim root. Works for both `kim/desktop/…/desktop` dev and
        // packaged-app layouts where the binary lives beside the project.
        if ancestor.join("orchestrator").join("agent.py").exists() {
            return Some(ancestor.to_path_buf());
        }
    }
    None
}

pub(crate) fn default_project_root() -> PathBuf {
    // 0a. Compile-time baked path — the only reliable option when the app runs
    //     from inside a .app bundle where no ancestor contains orchestrator/.
    //     Set by build.rs from CARGO_MANIFEST_DIR at build time.
    if let Some(baked) = option_env!("KIM_COMPILE_TIME_ROOT") {
        let p = PathBuf::from(baked);
        if p.exists() && p.join("orchestrator").join("agent.py").exists() {
            return p;
        }
    }

    // 0b. ~/.kim_root — written by install.sh so even a moved/renamed project
    //     can be found at runtime without a rebuild.
    if let Some(home) = dirs::home_dir() {
        let root_file = home.join(".kim_root");
        if let Ok(contents) = std::fs::read_to_string(&root_file) {
            let p = PathBuf::from(contents.trim());
            if p.exists() && p.join("orchestrator").join("agent.py").exists() {
                return p;
            }
        }
    }

    // 1. Environment override wins (explicit user intent).
    if let Ok(env_root) = std::env::var("KIM_PROJECT_ROOT") {
        let p = PathBuf::from(env_root);
        if p.exists() {
            return p;
        }
    }
    // 2. Walk up from the executable.
    if let Some(root) = exe_ancestor_kim_root() {
        return root;
    }
    // 3. ~/.kim (standard per-user install).
    if let Some(home) = dirs::home_dir() {
        let user = home.join(".kim");
        if user.exists() {
            return user;
        }
        // Return the default location even if not yet created
        return user;
    }
    PathBuf::from(".")
}

pub(crate) fn default_sessions_dir() -> PathBuf {
    // Environment override.
    if let Ok(env_dir) = std::env::var("KIM_SESSIONS_DIR") {
        let p = PathBuf::from(env_dir);
        if p.exists() {
            return p;
        }
    }
    // Project-root/kim_sessions if the project root was detected.
    let root = default_project_root();
    let root_sessions = root.join("kim_sessions");
    if root_sessions.exists() {
        return root_sessions;
    }
    // ~/.kim/sessions fallback.
    if let Some(home) = dirs::home_dir() {
        return home.join(".kim").join("sessions");
    }
    PathBuf::from("kim_sessions")
}

fn command_exists(cmd: &str) -> bool {
    std::process::Command::new(cmd)
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .is_ok()
}


/// Validate that a user-supplied `session_id` is a safe file-stem:
/// no path separators, no `..`, printable ASCII-ish. Prevents a caller
/// from escaping the per-date directory via `../../etc/passwd` etc.
pub(crate) fn validate_session_id(session_id: &str) -> Result<(), String> {
    if session_id.is_empty() {
        return Err("session_id is empty".to_string());
    }
    if session_id.len() > 128 {
        return Err("session_id is too long".to_string());
    }
    if session_id.contains('/')
        || session_id.contains('\\')
        || session_id.contains("..")
        || session_id.contains('\0')
    {
        return Err("session_id contains illegal characters".to_string());
    }
    // Only allow [A-Za-z0-9._-].
    if !session_id
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-')
    {
        return Err("session_id contains illegal characters".to_string());
    }
    Ok(())
}


pub(crate) fn browser_session_meta_filename(session_id: &str) -> String {
    format!("{}.browser.json", session_id)
}

fn read_browser_session_meta_from_dir(date_dir: &Path, session_id: &str) -> Option<BrowserSessionMeta> {
    let path = date_dir.join(browser_session_meta_filename(session_id));
    let raw = fs::read_to_string(path).ok()?;
    serde_json::from_str::<BrowserSessionMeta>(&raw).ok()
}

fn write_browser_session_meta_to_dir(
    date_dir: &Path,
    session_id: &str,
    meta: &BrowserSessionMeta,
) -> Result<(), String> {
    fs::create_dir_all(date_dir).map_err(|e| e.to_string())?;
    let path = date_dir.join(browser_session_meta_filename(session_id));
    let tmp_path = date_dir.join(format!("{}.browser.json.tmp", session_id));
    let text = serde_json::to_string_pretty(meta).map_err(|e| e.to_string())?;

    // Atomic write: write to a same-directory temp file, then rename over the
    // target. This prevents partially-written .browser.json files when the UI
    // and kimctl/bridge both commit URL metadata around the same time. The last
    // successful writer wins; callers always merge against the current file
    // before writing.
    fs::write(&tmp_path, text).map_err(|e| e.to_string())?;
    match fs::rename(&tmp_path, &path) {
        Ok(()) => Ok(()),
        Err(e) => {
            #[cfg(target_os = "windows")]
            {
                // Windows rename does not overwrite existing files. Fall back to
                // remove+rename; this is not perfectly atomic, but still avoids
                // exposing a partially-written JSON file.
                if path.exists() {
                    fs::remove_file(&path).map_err(|remove_err| remove_err.to_string())?;
                }
                fs::rename(&tmp_path, &path).map_err(|rename_err| rename_err.to_string())
            }
            #[cfg(not(target_os = "windows"))]
            {
                let _ = fs::remove_file(&tmp_path);
                Err(e.to_string())
            }
        }
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or(0)
}

fn session_base_dir(session_type: &str, kim_dir: Option<String>, codex_dir: Option<String>) -> PathBuf {
    if session_type == "codex" {
        codex_dir.map(PathBuf::from).unwrap_or_else(default_sessions_dir)
    } else {
        kim_dir.map(PathBuf::from).unwrap_or_else(default_sessions_dir)
    }
}

fn resolve_session_date_dir(
    base: &Path,
    session_id: &str,
    session_date: Option<&str>,
) -> Result<PathBuf, String> {
    validate_session_id(session_id)?;

    if let Some(date) = session_date.map(str::trim).filter(|d| !d.is_empty()) {
        let dir = base.join(date);
        if dir.join(format!("{}.jsonl", session_id)).exists()
            || dir.join(browser_session_meta_filename(session_id)).exists()
            || dir.exists()
        {
            return Ok(dir);
        }
    }

    if base.exists() {
        let mut date_dirs: Vec<PathBuf> = fs::read_dir(base)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        date_dirs.sort_by_key(|p| std::cmp::Reverse(p.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default()));
        for dir in date_dirs {
            if dir.join(format!("{}.jsonl", session_id)).exists()
                || dir.join(browser_session_meta_filename(session_id)).exists()
            {
                return Ok(dir);
            }
        }
    }

    // New chat fallback: create today's date bucket. This keeps metadata next to
    // the session file once the first run creates it.
    let today = chrono_like_today();
    Ok(base.join(today))
}

pub(crate) fn chrono_like_today() -> String {
    // Avoid adding a new dependency. Good enough for naming a fallback date dir;
    // most existing call sites pass the real session date.
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = secs / 86_400;
    // Civil date conversion from days since Unix epoch.
    let z = days as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if m <= 2 { 1 } else { 0 };
    format!("{:04}-{:02}-{:02}", year, m, d)
}

fn host_matches_site(host: &str, site: &str) -> bool {
    let host = host.trim().trim_start_matches("www.").to_ascii_lowercase();
    match normalize_site(site).as_str() {
        "claude" => host == "claude.ai" || host.ends_with(".claude.ai"),
        "chatgpt" => host == "chatgpt.com" || host == "chat.openai.com" || host.ends_with(".chatgpt.com"),
        "gemini" => host == "gemini.google.com",
        "deepseek" => host == "chat.deepseek.com" || host.ends_with(".deepseek.com"),
        "grok" => host == "grok.com" || host == "grok.x.com" || host == "x.com",
        _ => false,
    }
}

fn browser_url_site(url: &str) -> Option<String> {
    let parsed = tauri::Url::parse(url).ok()?;
    let host = parsed.host_str()?.to_ascii_lowercase();
    for site in ["claude", "chatgpt", "gemini", "deepseek", "grok"] {
        if host_matches_site(&host, site) {
            return Some(site.to_string());
        }
    }
    None
}

fn browser_url_is_bad_for_commit(url: &str, site: &str) -> bool {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return true;
    }
    let Ok(parsed) = tauri::Url::parse(trimmed) else { return true; };
    if !matches!(parsed.scheme(), "https" | "http") {
        return true;
    }
    let host = parsed.host_str().unwrap_or("").to_ascii_lowercase();
    if !host_matches_site(&host, site) {
        return true;
    }

    let lower = trimmed.to_ascii_lowercase();
    if lower.contains("accounts.google.com")
        || lower.contains("/login")
        || lower.contains("signin")
        || lower.contains("sign-in")
        || lower.contains("servicelogin")
        || lower.contains("signoutoptions")
        || lower.contains("/auth")
        || lower.contains("oauth")
    {
        return true;
    }

    let normalized = lower.trim_end_matches('/');
    let site_norm = normalize_site(site);
    match site_norm.as_str() {
        "claude" => normalized == "https://claude.ai" || normalized == "https://claude.ai/new",
        "chatgpt" => normalized == "https://chatgpt.com" || normalized == "https://chat.openai.com",
        "gemini" => normalized == "https://gemini.google.com" || normalized == "https://gemini.google.com/app",
        "deepseek" => normalized == "https://chat.deepseek.com",
        "grok" => normalized == "https://grok.com" || normalized == "https://grok.x.com",
        _ => true,
    }
}

fn browser_url_allowed_for_restore(url: &str, site: &str) -> bool {
    // Restore is deliberately stricter than "same host": do not navigate to
    // arbitrary URLs, login/auth pages, or provider home/new-chat pages stored
    // by mistake. Fallback home navigation is controlled separately.
    !browser_url_is_bad_for_commit(url, site)
}

fn query_param(raw_url: &str, wanted: &str) -> Option<String> {
    let url = tauri::Url::parse(&format!("http://localhost{}", raw_url)).ok()?;
    for (key, value) in url.query_pairs() {
        if key == wanted {
            let owned = value.into_owned();
            if !owned.trim().is_empty() {
                return Some(owned);
            }
        }
    }
    None
}

fn browser_restore_status_for_session(
    session_dir: &Path,
    session_id: Option<&str>,
    provider_arg: &str,
) -> String {
    let Some(session_id) = session_id.map(str::trim).filter(|s| !s.is_empty()) else {
        return "new_or_unknown".to_string();
    };
    if validate_session_id(session_id).is_err() {
        return "new_or_unknown".to_string();
    }

    let site = if provider_arg.starts_with("browser:") {
        normalize_site(provider_arg.trim_start_matches("browser:"))
    } else if provider_arg == "browser" {
        // The UI stores browser_last_site in the sidecar before send. If the
        // provider is the generic "browser", read that hint below.
        "".to_string()
    } else {
        return "not_browser".to_string();
    };

    let date_dir = match resolve_session_date_dir(session_dir, session_id, None) {
        Ok(v) => v,
        Err(_) => return "new_or_unknown".to_string(),
    };
    let meta = read_browser_session_meta_from_dir(&date_dir, session_id).unwrap_or_default();
    let resolved_site = if site.is_empty() {
        meta.browser_last_site.clone().unwrap_or_else(|| "claude".to_string())
    } else {
        site
    };

    match meta.browser_threads.get(&resolved_site) {
        Some(url) if browser_url_allowed_for_restore(url, &resolved_site) => "stored_thread".to_string(),
        Some(_) => "stored_url_rejected".to_string(),
        None => "no_stored_url".to_string(),
    }
}

pub(crate) fn read_sessions_from_dir(base: &Path, session_type: &str) -> Result<Vec<SessionInfo>, String> {
    if !base.exists() {
        return Ok(vec![]);
    }

    let mut sessions = vec![];

    let mut date_dirs: Vec<_> = fs::read_dir(base)
        .map_err(|e| e.to_string())?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .collect();
    date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for date_entry in date_dirs {
        let date_dir = date_entry.path();
        let date_str = date_entry.file_name().to_string_lossy().to_string();

        let mut jsonl_files: Vec<_> = fs::read_dir(&date_dir)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .filter(|e| {
                let name = e.file_name();
                let s = name.to_string_lossy();
                s.ends_with(".jsonl") && !s.contains(".summary")
            })
            .collect();
        jsonl_files.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for file_entry in jsonl_files {
            let session_file = file_entry.path();
            let session_id = session_file
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();

            let summary_file = date_dir.join(format!("{}.summary.txt", session_id));
            let has_summary = summary_file.exists();
            let summary = if has_summary {
                fs::read_to_string(&summary_file)
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            };

            let message_count = count_lines(&session_file).unwrap_or(0);
            let title = infer_session_title(&session_file, summary.as_ref(), &session_id);

            let browser_meta = read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();
            let BrowserSessionMeta {
                browser_threads,
                browser_last_site,
                browser_threads_updated_at_ms,
                last_llm_provider,
            } = browser_meta;
            sessions.push(SessionInfo {
                session_key: format!("{}:{}:{}", session_type, date_str, session_id),
                session_id,
                title,
                date: date_str.clone(),
                message_count,
                has_summary,
                summary,
                session_type: session_type.to_string(),
                browser_threads: if browser_threads.is_empty() { None } else { Some(browser_threads) },
                browser_last_site,
                browser_threads_updated_at_ms,
                last_llm_provider,
            });
        }
    }

    Ok(sessions)
}

pub(crate) fn count_lines(path: &Path) -> std::io::Result<usize> {
    let file = fs::File::open(path)?;
    let reader = BufReader::new(file);
    Ok(reader
        .lines()
        .filter(|l| {
            l.as_ref()
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false)
        })
        .count())
}

pub(crate) fn parse_jsonl(path: &Path) -> Result<Vec<KimMessage>, String> {
    let file = fs::File::open(path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);
    let mut messages = vec![];

    for (i, line) in reader.lines().enumerate() {
        let line = line.map_err(|e| e.to_string())?;
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        match serde_json::from_str::<KimMessage>(trimmed) {
            Ok(msg) => messages.push(msg),
            Err(e) => {
                let parsed = serde_json::from_str::<serde_json::Value>(trimmed).ok();
                // Typed trace records (run_started, tool_call, llm_turn, run_checkpoint,
                // run_result) are valid JSON with a "type" key but no "role". Skip them
                // silently — they are not chat messages and are not malformed.
                let is_trace_record = parsed.as_ref().and_then(|v| v.get("type")).is_some()
                    && parsed.as_ref().and_then(|v| v.get("role")).is_none();
                if is_trace_record {
                    continue;
                }
                match parsed.and_then(codex_jsonl_line_to_kim_message) {
                    Some(msg) => messages.push(msg),
                    None => eprintln!("Skipping malformed JSONL line {}: {}", i + 1, e),
                }
            }
        }
    }

    Ok(messages)
}

fn codex_jsonl_line_to_kim_message(value: serde_json::Value) -> Option<KimMessage> {
    if value.get("type").and_then(|v| v.as_str()) != Some("message") {
        return None;
    }

    let message = value.get("message")?;
    let role = message.get("role")?.as_str()?.to_string();
    let content = message
        .get("blocks")
        .cloned()
        .map(normalize_codex_blocks)
        .or_else(|| message.get("content").cloned())
        .unwrap_or_else(|| serde_json::Value::String(String::new()));

    Some(KimMessage {
        role,
        content,
        tool_calls: None,
        tool_call_id: None,
        name: None,
    })
}

fn normalize_codex_blocks(blocks: serde_json::Value) -> serde_json::Value {
    let serde_json::Value::Array(items) = blocks else {
        return blocks;
    };

    serde_json::Value::Array(
        items
            .into_iter()
            .map(|mut block| {
                if block.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                    if let Some(raw_input) = block.get("input").and_then(|v| v.as_str()) {
                        let parsed = serde_json::from_str::<serde_json::Value>(raw_input)
                            .unwrap_or_else(|_| serde_json::json!({ "raw": raw_input }));
                        if let Some(obj) = block.as_object_mut() {
                            obj.insert("input".to_string(), parsed);
                        }
                    }
                }
                block
            })
            .collect(),
    )
}

fn normalize_title_text(raw: &str) -> Option<String> {
    let mut text = raw.replace('\n', " ");
    text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut cleaned = text.trim().to_string();

    for prefix in ["Task:", "task:", "TASK:"] {
        if cleaned.starts_with(prefix) {
            cleaned = cleaned[prefix.len()..].trim().to_string();
            break;
        }
    }

    if cleaned.is_empty() {
        return None;
    }

    let max_chars = 56usize;
    let char_count = cleaned.chars().count();
    if char_count > max_chars {
        let mut shortened: String = cleaned.chars().take(max_chars - 1).collect();
        shortened = shortened.trim_end().to_string();
        return Some(format!("{}…", shortened));
    }

    Some(cleaned)
}

fn extract_title_from_content(content: &serde_json::Value) -> Option<String> {
    match content {
        serde_json::Value::String(s) => normalize_title_text(s),
        serde_json::Value::Array(items) => {
            for item in items {
                let item_type = item.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if item_type == "text" {
                    if let Some(text) = item.get("text").and_then(|v| v.as_str()) {
                        if let Some(title) = normalize_title_text(text) {
                            return Some(title);
                        }
                    }
                }
            }
            None
        }
        _ => None,
    }
}

pub(crate) fn infer_session_title(session_file: &Path, summary: Option<&String>, session_id: &str) -> String {
    if let Ok(file) = fs::File::open(session_file) {
        let reader = BufReader::new(file);
        for line in reader.lines().map_while(Result::ok).take(80) {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let value: serde_json::Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(_) => continue,
            };

            let role = value.get("role").and_then(|v| v.as_str()).unwrap_or("");
            if role == "user" {
                if let Some(content) = value.get("content") {
                    if let Some(title) = extract_title_from_content(content) {
                        return title;
                    }
                }
            } else if value.get("type").and_then(|v| v.as_str()) == Some("message") {
                let message = value.get("message").unwrap_or(&serde_json::Value::Null);
                let role = message.get("role").and_then(|v| v.as_str()).unwrap_or("");
                if role == "user" {
                    if let Some(content) = message.get("blocks").or_else(|| message.get("content")) {
                        if let Some(title) = extract_title_from_content(content) {
                            return title;
                        }
                    }
                }
            }
        }
    }

    if let Some(s) = summary {
        if let Some(title) = normalize_title_text(s) {
            return title;
        }
    }

    let short_id: String = session_id.chars().take(8).collect();
    format!("Session {}", short_id)
}

fn header_value(request: &Request, name: &str) -> Option<String> {
    request
        .headers()
        .iter()
    .find(|h| h.field.to_string().eq_ignore_ascii_case(name))
        .map(|h| h.value.as_str().to_string())
}

fn json_response(status: u16, body: serde_json::Value) -> Response<std::io::Cursor<Vec<u8>>> {
    let mut resp = Response::from_string(body.to_string()).with_status_code(StatusCode(status));
    if let Ok(h) = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..]) {
        resp.add_header(h);
    }
    if let Ok(h) = Header::from_bytes(&b"Access-Control-Allow-Origin"[..], &b"*"[..]) {
        resp.add_header(h);
    }
    if let Ok(h) = Header::from_bytes(
        &b"Access-Control-Allow-Headers"[..],
        &b"Content-Type, X-Kim-Token"[..],
    ) {
        resp.add_header(h);
    }
    if let Ok(h) = Header::from_bytes(&b"Access-Control-Allow-Methods"[..], &b"GET, POST, OPTIONS"[..]) {
        resp.add_header(h);
    }
    resp
}

fn respond_json(request: Request, status: u16, body: serde_json::Value) {
    let _ = request.respond(json_response(status, body));
}

fn agent_debug_log(hypothesis_id: &str, message: &str, data: serde_json::Value) {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let line = serde_json::json!({
        "sessionId": "16b33e",
        "hypothesisId": hypothesis_id,
        "location": "desktop/src-tauri/src/lib.rs",
        "message": message,
        "data": data,
        "timestamp": ts,
    });
    let log_path = default_sessions_dir().join("bridge_debug.log");
    let _ = std::fs::create_dir_all(default_sessions_dir());
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
    {
        use std::io::Write;
        let _ = writeln!(f, "{}", line);
    }
}

fn normalize_site(site: &str) -> String {
    match site.trim().to_lowercase().as_str() {
        "claude" | "claude.ai" => "claude".to_string(),
        "chatgpt" | "openai" | "gpt" => "chatgpt".to_string(),
        "gemini" | "google" => "gemini".to_string(),
        "deepseek" => "deepseek".to_string(),
        "grok" => "grok".to_string(),
        other if !other.is_empty() => other.to_string(),
        _ => "claude".to_string(),
    }
}

fn last_llm_provider_allowed(p: &str) -> bool {
    if p.is_empty() || p.len() > 64 {
        return false;
    }
    matches!(
        p,
        "browser"
            | "browser:claude"
            | "browser:chatgpt"
            | "browser:gemini"
            | "browser:grok"
            | "browser:deepseek"
            | "browser:custom"
            | "claude"
            | "openai"
            | "gemini"
            | "deepseek"
            | "ollama"
    )
}

fn apply_browser_meta_writes(
    meta: &mut BrowserSessionMeta,
    browser_last_site: Option<String>,
    site: Option<String>,
    url: Option<String>,
    last_llm_provider: Option<String>,
) -> Result<(), String> {
    if let Some(last) = browser_last_site
        .as_deref()
        .map(normalize_site)
        .filter(|s| !s.is_empty())
    {
        meta.browser_last_site = Some(last);
    }

    if let (Some(site_raw), Some(url_raw)) = (site.as_deref(), url.as_deref()) {
        let site_norm = normalize_site(site_raw);
        if browser_url_is_bad_for_commit(url_raw, &site_norm) {
            return Err(format!(
                "Refusing to store non-conversation/login URL for {}: {}",
                site_norm, url_raw
            ));
        }
        meta.browser_threads
            .insert(site_norm.clone(), url_raw.trim().to_string());
        meta.browser_last_site = Some(site_norm);
    }

    if let Some(p) = last_llm_provider {
        let t = p.trim();
        if last_llm_provider_allowed(t) {
            meta.last_llm_provider = Some(t.to_string());
        }
    }

    meta.browser_threads_updated_at_ms = Some(now_ms());
    Ok(())
}

fn default_site_url(site: &str) -> &'static str {
    match normalize_site(site).as_str() {
        "chatgpt" => "https://chatgpt.com",
        "gemini" => "https://gemini.google.com/app",
        "deepseek" => "https://chat.deepseek.com",
        "grok" => "https://grok.com",
        _ => "https://claude.ai/new",
    }
}

fn gemini_site_url(authuser: Option<u32>) -> String {
    match authuser {
        Some(index) => format!("https://gemini.google.com/app?authuser={index}"),
        None => "https://gemini.google.com/app".to_string(),
    }
}

fn fresh_site_url(site: &str, authuser: Option<u32>) -> String {
    if normalize_site(site) == "gemini" {
        gemini_site_url(authuser)
    } else {
        default_site_url(site).to_string()
    }
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

    // Give the provider SPA and the initialization_script-backed Kim bridge time
    // to install before the next eval calls window.__kimBridge.send(...).
    std::thread::sleep(Duration::from_millis(3500));

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

fn is_bridge_task_running() -> bool {
    let store = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None));
    let Ok(mut guard) = store.lock() else { return false };
    match *guard {
        Some(pid) if process_exists(pid) => true,
        Some(_) => {
            // Process exited but PID was never cleared — clean it up
            *guard = None;
            false
        }
        None => false,
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
            std::thread::sleep(Duration::from_millis(3500));
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
fn save_frontmost_app() -> Option<String> { None }

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

    let temp_path = format!("/tmp/kim_clip_{}.png", std::process::id());
    if let Err(e) = std::fs::write(&temp_path, &bytes) {
        eprintln!("[Kim] clipboard: write temp failed: {}", e);
        return false;
    }

    // «class PNGf» is the four-char AppleScript type for PNG data.
    let script = format!(
        "set the clipboard to (read (POSIX file \"{}\") as «class PNGf»)",
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

/// Stage a text prompt through a temporary file and copy it to the macOS
/// clipboard. The temporary file is removed immediately after pbcopy reads it.
#[cfg(target_os = "macos")]
fn write_text_prompt_to_clipboard(prompt: &str) -> bool {
    let stamp = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let temp_path = format!("/tmp/kim_prompt_{}_{}.txt", std::process::id(), stamp);
    if let Err(e) = std::fs::write(&temp_path, prompt.as_bytes()) {
        eprintln!("[Kim] clipboard: prompt temp write failed: {}", e);
        return false;
    }

    let bytes = match std::fs::read(&temp_path) {
        Ok(bytes) => bytes,
        Err(e) => {
            let _ = std::fs::remove_file(&temp_path);
            eprintln!("[Kim] clipboard: prompt temp read failed: {}", e);
            return false;
        }
    };

    let mut child = match std::process::Command::new("pbcopy")
        .stdin(std::process::Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => {
            let _ = std::fs::remove_file(&temp_path);
            eprintln!("[Kim] clipboard: pbcopy spawn failed: {}", e);
            return false;
        }
    };

    let wrote = child
        .stdin
        .take()
        .map(|mut stdin| {
            use std::io::Write;
            stdin.write_all(&bytes).is_ok()
        })
        .unwrap_or(false);
    let ok = wrote && child.wait().map(|s| s.success()).unwrap_or(false);
    let _ = std::fs::remove_file(&temp_path);

    if !ok {
        eprintln!("[Kim] clipboard: pbcopy failed (non-fatal)");
    }
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
    let pos = win.outer_position().unwrap_or(tauri::PhysicalPosition::new(0, 0));
    let size = win.outer_size().unwrap_or(tauri::PhysicalSize::new(100, 100));
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
    if app_handle.get_webview_window("kim-browser-signin").is_some() {
        show_browser_window_impl(&app_handle);
        Ok(())
    } else {
        Err("No Kim browser window is open yet. Open a browser provider first.".to_string())

    }
}

pub(crate) mod window_manager;
pub(crate) use window_manager::{hide_main_window, show_main_window, set_task_active_mode};
pub(crate) mod updater;

#[tauri::command]
async fn hide_browser_window(app_handle: tauri::AppHandle) -> Result<(), String> {
    let label = "kim-browser-signin";
    if let Some(win) = app_handle.get_webview_window(label) {
        hide_browser_window_offscreen(&win);

        // Refocus the main app window.
        if let Some(main_win) = app_handle.get_webview_window("main") {
            let _ = main_win.show();
            let _ = main_win.set_focus();
        }
        let _ = app_handle.emit("kim-browser-hidden", true);
        Ok(())
    } else {
        Ok(())
    }
}

#[tauri::command]
async fn set_browser_keep_visible(keep_visible: bool, app_handle: tauri::AppHandle) -> Result<(), String> {
    if let Ok(mut guard) = WEBVIEW_KEEP_VISIBLE.get_or_init(|| StdMutex::new(false)).lock() {
        *guard = keep_visible;
    }

    if keep_visible && app_handle.get_webview_window("kim-browser-signin").is_some() {
        show_browser_window_impl(&app_handle);
    }

    Ok(())
}

#[tauri::command]
async fn navigate_browser_window_if_open(url: String, app_handle: tauri::AppHandle) -> Result<bool, String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return Err("URL cannot be empty.".to_string());
    }
    if browser_url_site(trimmed).is_none() {
        return Err("Refusing to navigate the provider browser to a non-provider URL.".to_string());
    }

    if let Some(existing) = app_handle.get_webview_window("kim-browser-signin") {
        let task_running = is_bridge_task_running();

        if task_running {
            return Err(
                "Cannot navigate the provider browser while Kim is running a task; this would lose LLM context."
                    .to_string(),
            );
        }

        let js_url = serde_json::to_string(trimmed).map_err(|e| e.to_string())?;
        let _ = existing.eval(format!("window.location.href = {};", js_url));
        Ok(true)
    } else {
        Ok(false)
    }
}


#[tauri::command]
async fn get_browser_current_url(app_handle: tauri::AppHandle) -> Result<Option<String>, String> {
    if let Some(win) = app_handle.get_webview_window("kim-browser-signin") {
        let url = webview_current_href(&win);
        if url.trim().is_empty() {
            Ok(None)
        } else {
            Ok(Some(url))
        }
    } else {
        Ok(None)
    }
}

#[tauri::command]
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
    let mut meta = read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();

    apply_browser_meta_writes(
        &mut meta,
        browser_last_site,
        site,
        url,
        last_llm_provider,
    )?;
    write_browser_session_meta_to_dir(&date_dir, &session_id, &meta)?;
    Ok(meta)
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
        return session_browser_meta_read(session_id, session_date, session_type, kim_dir, codex_dir).await;
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
        let mut meta = read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default();
        if meta.browser_last_site.as_deref() != Some(site.as_str()) {
            meta.browser_last_site = Some(site);
            meta.browser_threads_updated_at_ms = Some(now_ms());
            let _ = write_browser_session_meta_to_dir(&date_dir, &session_id, &meta);
        }
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
    ).await
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
    if is_bridge_task_running() {
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
        Some("Saved browser URL was no longer safe/valid, so Kim opened a fresh provider page.".to_string())
    } else {
        Some("No saved browser conversation for this provider; opened the provider start page.".to_string())
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
/// NOTE: this function must only be called from a blocking context (e.g. inside
/// `tokio::task::spawn_blocking`) because `TcpStream::connect` and `fs` calls are
/// synchronous.  Do NOT call it directly from an async Tokio task.
fn launch_chrome_for_cdp(project_root: &Path) -> Result<bool, String> {
    use std::net::TcpStream;
    use std::process::Command as StdCommand;

    let port_open = TcpStream::connect("127.0.0.1:9222").is_ok();
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
        "google-chrome", "google-chrome-stable", "chromium-browser", "chromium",
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
        let result = StdCommand::new(chrome)
            .args([
                user_data_arg.as_str(),
                "--remote-debugging-port=9222",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-popup-blocking",
            ])
            .spawn();
        if result.is_ok() {
            // Caller is responsible for the post-launch wait so it can use
            // tokio::time::sleep instead of std::thread::sleep.
            return Ok(true); // freshly spawned — caller must wait for port
        }
    }
    Err("Chrome/Chromium not found. Install Google Chrome to use the browser provider.".to_string())
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
        if line.is_empty() || line.starts_with('#') { continue; }
        if let Some(rest) = line.strip_prefix(&prefix) {
            let mut v = rest.trim().to_string();
            if (v.starts_with('"') && v.ends_with('"') && v.len() >= 2)
                || (v.starts_with('\'') && v.ends_with('\'') && v.len() >= 2)
            {
                v = v[1..v.len() - 1].to_string();
            }
            if !v.is_empty() { return Some(v); }
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

pub(crate) async fn selected_ollama_codex_model(
    mode: Option<&str>,
    base_url: Option<&str>,
    local_model: Option<&str>,
    cloud_model: Option<&str>,
    config: &config::AppConfig,
) -> Result<String, String> {
    let mode = mode.unwrap_or("cloud").trim().to_ascii_lowercase();
    if mode == "local" {
        let model = local_model.unwrap_or("").trim();
        if !model.is_empty() {
            return Ok(model.to_string());
        }
        let base = base_url
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("http://localhost:11434");
        if let Ok(models) = ollama_tags(base).await {
            if let Some(first) = models.first().map(|m| m.name.trim()).filter(|m| !m.is_empty()) {
                return Ok(first.to_string());
            }
        }
        return Err("Pick or pull an Ollama local model before running Codex with Ollama Local.".to_string());
    }
    let fallback = config.default_model.get("ollama").map(|s| s.as_str()).unwrap_or("gpt-oss:120b-cloud");
    Ok(cloud_model
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback)
        .to_string())
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn configure_codex_direct_provider(
    cmd: &mut tokio::process::Command,
    provider_arg: &str,
    kim_root: &Path,
    ollama_base_url: Option<&str>,
    ollama_mode: Option<&str>,
    ollama_local_model: Option<&str>,
    ollama_cloud_model: Option<&str>,
    config: &config::AppConfig,
) -> Result<String, String> {
    let provider = provider_arg.trim().to_ascii_lowercase();
    match provider.as_str() {
        "ollama" => {
            let model = selected_ollama_codex_model(
                ollama_mode,
                ollama_base_url,
                ollama_local_model,
                ollama_cloud_model,
                config,
            ).await?;
            cmd.arg("--model").arg(&model)
                .env("OPENAI_BASE_URL", ollama_openai_base_url(ollama_base_url))
                // Required by OpenAI-compatible clients; ignored by Ollama.
                .env("OPENAI_API_KEY", "ollama");
            Ok(format!("Ollama via local daemon ({model})"))
        }
        "openai" => {
            let key = read_env_file_var(kim_root, "OPENAI_API_KEY")
                .ok_or_else(|| "Codex with OpenAI needs OPENAI_API_KEY in the environment or Kim's .env.".to_string())?;
            let fallback = config.default_model.get("openai").map(|s| s.as_str()).unwrap_or("openai/gpt-4o");
            let model = read_first_env_file_var(kim_root, &["CODEX_OPENAI_MODEL", "OPENAI_MODEL"])
                .unwrap_or_else(|| fallback.to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key);
            if let Some(base) = read_env_file_var(kim_root, "OPENAI_BASE_URL") {
                cmd.env("OPENAI_BASE_URL", base);
            }
            Ok(format!("OpenAI-compatible API ({model})"))
        }
        "deepseek" => {
            let key = read_env_file_var(kim_root, "DEEPSEEK_API_KEY")
                .ok_or_else(|| "Codex with DeepSeek needs DEEPSEEK_API_KEY in the environment or Kim's .env.".to_string())?;
            let fallback = config.default_model.get("deepseek").map(|s| s.as_str()).unwrap_or("deepseek-chat");
            let model = read_first_env_file_var(kim_root, &["CODEX_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"])
                .unwrap_or_else(|| fallback.to_string());
            let base = read_env_file_var(kim_root, "DEEPSEEK_BASE_URL")
                .unwrap_or_else(|| "https://api.deepseek.com/v1".to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key)
                .env("OPENAI_BASE_URL", base);
            Ok(format!("DeepSeek API ({model})"))
        }
        "gemini" => {
            let key = read_env_file_var(kim_root, "GOOGLE_API_KEY")
                .ok_or_else(|| "Codex with Gemini direct API needs GOOGLE_API_KEY in the environment or Kim's .env. Kim's Google OAuth token is only wired into the Chat provider path.".to_string())?;
            let fallback = config.default_model.get("gemini").map(|s| s.as_str()).unwrap_or("gemini-2.0-flash");
            let model = read_first_env_file_var(kim_root, &["CODEX_GEMINI_MODEL", "GEMINI_MODEL"])
                .unwrap_or_else(|| fallback.to_string());
            let base = read_env_file_var(kim_root, "GEMINI_OPENAI_BASE_URL")
                .unwrap_or_else(|| "https://generativelanguage.googleapis.com/v1beta/openai".to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key)
                .env("OPENAI_BASE_URL", base);
            Ok(format!("Gemini OpenAI-compatible API ({model})"))
        }
        _ => {
            let key = read_first_env_file_var(kim_root, &["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
                .ok_or_else(|| "Codex needs an Anthropic API key for Claude direct mode. Add ANTHROPIC_API_KEY to Kim's .env, or switch the provider dropdown to Ollama/Browser.".to_string())?;
            cmd.env("ANTHROPIC_API_KEY", key);
            for key in ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CODEX_MODEL", "CLAUDE_MODEL", "ANTHROPIC_MODEL"] {
                if let Some(value) = read_env_file_var(kim_root, key) {
                    cmd.env(key, value);
                }
            }
            Ok("Claude direct API".to_string())
        }
    }
}

pub(crate) mod subprocess;
pub(crate) use subprocess::{find_python_interpreter, send_task, cancel_task, hitl_respond_approval, process_exists, send_signal};

// ---------------------------------------------------------------------------
// Voice config (config.yaml — voice:/enabled, voice:/engine, voice:/voice_id)
// ---------------------------------------------------------------------------

pub(crate) fn config_yaml_path(project_root: Option<String>) -> PathBuf {
    project_root
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root)
        .join("config.yaml")
}


// ---------------------------------------------------------------------------
// Phone relay (config.yaml `relay:` block + RELAY_PC_API_KEY env var)
// ---------------------------------------------------------------------------
//
// The PC reads `relay.url` from config.yaml so the user can point Kim at a
// different relay (self-hosted, staging, etc.) without rebuilding. The PC
// authenticates to the relay with `RELAY_PC_API_KEY` — which we keep in the
// .env file rather than the YAML so it doesn't end up in screenshots.

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
    let task_state: TaskState = Arc::new(Mutex::new(RunningTask::default()));
    let schedule_timer_state = schedule_commands::new_schedule_timer_state();
    let config_path = config_yaml_path(None);
    let config = config::load_config(&config_path);

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
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
            Ok(())
        })
        .manage(task_state)
        .manage(schedule_timer_state)
        .manage(config)
        .invoke_handler(tauri::generate_handler![
            session_commands::list_sessions,
            session_commands::delete_sessions,
            session_commands::prune_sessions,
            session_commands::load_session_messages,
            session_commands::summarize_session,
            run_history::save_run_history,
            run_history::load_run_history,
            session_commands::get_app_version,
            session_commands::reveal_logs,
            run_history::get_platform_info,
            run_history::run_update,
            browser_bridge::add_custom_provider_capability,
            browser_bridge::open_browser_signin_window,
            navigate_browser_window_if_open,
            get_browser_current_url,
            session_browser_meta_read,
            session_browser_meta_write,
            session_browser_url_commit,
            restore_browser_for_session,
            show_browser_window,
            hide_browser_window,
            set_browser_keep_visible,
            provider_auth::provider_check_auth,
            provider_auth::provider_signin,
            provider_auth::provider_signout,
            hide_main_window,
            show_main_window,
            set_task_active_mode,
            send_task,
            cancel_task,
            hitl_respond_approval,
            voice_config::read_voice_config,
            voice_config::write_voice_config,
            relay::read_relay_config,
            relay::write_relay_url,
            relay::relay_pair_init,
            relay::relay_pair_status,
            google_oauth::google_oauth_status,
            google_oauth::google_oauth_start,
            google_oauth::google_oauth_disconnect,
            google_oauth::google_oauth_test,
            google_oauth::google_oauth_setup_free_tier_project,
            account::load_account,
            account::save_account,
            account::clear_account,
            account::reset_onboarding,
            account::delete_all_sessions,
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
            codex_projects::add_code_project,
            codex_projects::remove_code_project,
            codex_projects::open_in_finder,
            feedback::send_feedback,
            show_screenshot_flash,
            feedback::save_attachment,
            schedule_commands::list_scheduled_tasks,
            schedule_commands::add_scheduled_task,
            schedule_commands::update_scheduled_task,
            schedule_commands::delete_scheduled_task,
            schedule_commands::list_due_scheduled_tasks,
            schedule_commands::run_due_scheduled_task,
            schedule_commands::start_schedule_timer,
            schedule_commands::stop_schedule_timer,
            schedule_commands::get_schedule_timer_status,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

    // Helper: write lines to a temp file, return path.
    fn write_temp_jsonl(lines: &[&str]) -> std::path::PathBuf {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("kim_test_{}.jsonl", std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap_or_default().subsec_nanos()));
        std::fs::write(&path, lines.join("\n")).unwrap();
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
        assert_eq!(msgs.len(), 2, "trace records must be skipped, not treated as errors");
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

    #[test]
    fn test_build_bridge_complete_script_no_poisoning() {
        let script = build_bridge_complete_script(
            "gemini",
            "hello __KIM_SITE__",
            "req_123",
            &[],
            "http://local",
            "token__KIM_REQID__",
            None,
            None
        ).unwrap();

        assert!(script.contains("const __kimSite = \"gemini\";"));
        assert!(script.contains("const __kimPrompt = \"hello __KIM_SITE__\";"));
        assert!(script.contains("const __kimReqId = \"req_123\";"));
        assert!(script.contains("const __kimCallbackToken = \"token__KIM_REQID__\";"));
    }
}
