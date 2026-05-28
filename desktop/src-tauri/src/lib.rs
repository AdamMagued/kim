use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{Emitter, Listener, Manager};
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
pub mod session_commands;
pub mod voice_config;
pub mod config;

// Re-export commonly used types/helpers from submodules so remaining lib.rs
// code (session listing, run history, codex file-bridge) can use them unqualified.
use codex_projects::{mirror_latest_claw_session_to_codex, newest_codex_session};
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
                match serde_json::from_str::<serde_json::Value>(trimmed)
                    .ok()
                    .and_then(codex_jsonl_line_to_kim_message)
                {
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

const PERSISTENT_BRIDGE_JS: &str = include_str!("bridge.js");

fn open_browser_signin_window_impl(
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
fn open_browser_signin_window_with_visibility(
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
                        &payload_str[..payload_str.len().min(200)]
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
async fn add_custom_provider_capability(url: String, app_handle: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    use tauri::ipc::CapabilityBuilder;

    let parsed = tauri::Url::parse(&url).map_err(|e| e.to_string())?;
    let origin = parsed.origin().ascii_serialization();
    let capability_url = format!("{}/*", origin);

    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
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
fn clean_bridge_progress_text(text: &str) -> Option<String> {
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
        cleaned.truncate(277);
        cleaned.push_str("...");
    }

    Some(cleaned)
}

fn emit_bridge_progress(app_handle: &tauri::AppHandle, req_id: &str, text: &str) {
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

    let _ = app_handle.emit("kim-agent-output", format!("[STATUS] {}", cleaned));
}

fn handle_bridge_ipc_event(ipc_event: BridgeIpcEvent, app_handle: &tauri::AppHandle) {
    agent_debug_log(
        "H1",
        "bridge IPC event received",
        serde_json::json!({
            "event": ipc_event.event,
            "reqId": ipc_event.req_id,
            "hasResponse": ipc_event.response.is_some(),
            "hasError": ipc_event.error.is_some(),
            "hasText": ipc_event.text.is_some(),
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
        other => {
            eprintln!("[Kim] Unknown bridge IPC event type: {}", other);
        }
    }
}

/// Wake up any thread waiting on WEBVIEW_BRIDGE_NOTIFY.
fn notify_bridge_result() {
    let (_, condvar) = WEBVIEW_BRIDGE_NOTIFY.get_or_init(|| {
        (StdMutex::new(()), Condvar::new())
    });
    condvar.notify_all();
}

#[allow(clippy::too_many_arguments)]
fn build_bridge_complete_script(
    site: &str,
    prompt: &str,
    req_id: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
    completion_hash: Option<&str>,
    model_tier: Option<&str>,
) -> Result<String, String> {
    let site_json = serde_json::to_string(site).map_err(|e| e.to_string())?;
    let prompt_json = serde_json::to_string(prompt).map_err(|e| e.to_string())?;
    let req_json = serde_json::to_string(req_id).map_err(|e| e.to_string())?;
    let attachments_json = serde_json::to_string(attachments).map_err(|e| e.to_string())?;
    let callback_url_json = serde_json::to_string(callback_url).map_err(|e| e.to_string())?;
    let callback_token_json = serde_json::to_string(callback_token).map_err(|e| e.to_string())?;
    let hash_json = serde_json::to_string(&completion_hash).unwrap_or_else(|_| "null".to_string());

    let header = format!(r#"(() => {{
    setTimeout(async () => {{
    try {{
  const __kimSite = {site};
  const __kimPrompt = {prompt};
  const __kimReqId = {req_id};
  const __kimAttachments = {attachments};
  const __kimCallbackUrl = {callback_url};
  const __kimCallbackToken = {callback_token};
  const __kimCompletionHash = {hash};
  window.__kimModelTier = {tier};
    let __kimFinished = false;
    let __kimWatchdog = null;
"#,
        site=site_json,
        prompt=prompt_json,
        req_id=req_json,
        attachments=attachments_json,
        callback_url=callback_url_json,
        callback_token=callback_token_json,
        hash=hash_json,
        tier=serde_json::to_string(&model_tier).unwrap_or_else(|_| "null".to_string())
    );

    let body = r#"
  const SITE_CONFIGS = {
    claude: {
      input_selectors: ["div[contenteditable='true'].ProseMirror", "div[contenteditable='true']"],
            send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[aria-label*='send']"],
    stop_selectors: ["button[aria-label*='Stop']"],
      response_selectors: ["[data-testid^='conversation-turn']", ".font-claude-message"],
            upload_button_selectors: ["button[aria-label*='Attach']", "button[aria-label*='Upload']"],
            file_input_selectors: ["input[type='file']"],
    },
    chatgpt: {
      input_selectors: ["div#prompt-textarea", "div[contenteditable='true']"],
      send_selectors: ["button[data-testid='send-button']", "button[aria-label*='Send']"],
      stop_selectors: ["button[data-testid='stop-button']", "button[aria-label*='Stop']"],
      response_selectors: ["div.markdown", "article div.prose"],
            upload_button_selectors: ["button[aria-label*='Attach']", "button[data-testid*='upload']"],
            file_input_selectors: ["input[type='file']"],
    },
    gemini: {
        input_selectors: ["rich-textarea div[contenteditable]", "rich-textarea [contenteditable='true']", "div[contenteditable='true']"],
        send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[data-testid*='send']", "button[mattooltip*='Send']"],
            stop_selectors: ["button[aria-label*='Stop']", "button[aria-label*='Stop generating']", "button[data-testid*='stop']"],
            response_selectors: ["model-response", "model-response message-content", "model-response .response-content", "message-content", "div.response-content", "div.markdown"],
            upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Add image']"],
            file_input_selectors: ["input[type='file']"],
    },
    deepseek: {
      input_selectors: ["textarea#chat-input", "textarea"],
      send_selectors: ["button[aria-label*='Send']", "button[type='submit']"],
      stop_selectors: ["button[aria-label*='Stop']", "div[role='button'][class*='stop']"],
      response_selectors: ["div.ds-markdown"],
            upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Attach']"],
            file_input_selectors: ["input[type='file']"],
    },
    grok: {
      input_selectors: ["textarea", "div[contenteditable='true']"],
      send_selectors: ["button[aria-label*='Send']", "button[type='submit']"],
      stop_selectors: ["button[aria-label*='Stop']"],
      response_selectors: ["article", "div.markdown", "[data-testid*='message']"],
            upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Attach']"],
            file_input_selectors: ["input[type='file']"],
    },
  };

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const RESPONSE_WAIT_MS = 45000;
    const STOP_APPEAR_WAIT_MS = 5000;
    const GENERATION_DONE_WAIT_MS = 60000;
    const READ_WAIT_MS = 10000;
    const HARD_SCRIPT_DEADLINE_MS = 90000;
    const hardDeadlineAt = Date.now() + HARD_SCRIPT_DEADLINE_MS;

    const ensureWithinDeadline = (stage) => {
        if (Date.now() > hardDeadlineAt) {
            throw new Error(`Hard timeout at ${stage} after ${HARD_SCRIPT_DEADLINE_MS}ms`);
        }
    };

  // No-op: previously fetched to localhost:7243 which CSP blocks on every provider page,
  // serializing the script on dozens of blocked network calls.  Silent no-op is safe
  // because Rust already logs bridge lifecycle via agent_debug_log.
  const __kimDbg = () => {};

  const emitPayload = async (payload) => {
        if (__kimFinished) return;
        __kimFinished = true;
        if (__kimWatchdog) {
            clearTimeout(__kimWatchdog);
            __kimWatchdog = null;
        }
    // #region agent log
    __kimDbg('H3', 'emitPayload called', { ok: !!payload?.ok, hasError: !!payload?.error, site: payload?.site || __kimSite || 'unknown' });
    // #endregion
    try {
      const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
            // Store payload in a per-request map so retries/cancellations cannot
            // leak stale data from a previous req_id.
            if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) {
                window.__kimBridgeStore = {};
            }
            window.__kimBridgeStore[__kimReqId] = {
                data: encoded,
                err: null,
                ts: Date.now(),
            };
    } catch (err) {
            if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) {
                window.__kimBridgeStore = {};
            }
            window.__kimBridgeStore[__kimReqId] = {
                data: null,
                err: String(err),
                ts: Date.now(),
            };
    }

    // Payload delivery: JS just stores the result in window.__kimBridgeStore[reqId].
    // Rust reads it via a tight eval+title roundtrip (see pull_payload_from_js_store).
    // No title pulsing, no Angular race, no CSP-blocked HTTP callback.
  };

    const __KIM_WATCHDOG_MS = 95000;
    __kimWatchdog = setTimeout(() => {
        if (__kimFinished) return;
        emitPayload({
            ok: false,
            error: `Bridge script watchdog timeout after ${__KIM_WATCHDOG_MS}ms`,
            site: __kimSite || 'unknown',
        });
    }, __KIM_WATCHDOG_MS);

  const findSelector = (selectors) => {
    for (const sel of selectors || []) {
      try {
        if (document.querySelector(sel)) return sel;
      } catch (_) {}
    }
    return null;
  };

    const isVisible = (el) => {
        if (!el) return false;
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) {
            return false;
        }
        if (el.offsetParent !== null) return true;
        return style.position === 'fixed';
    };

    const isEnabled = (el) => {
        if (!el) return false;
        if ('disabled' in el && el.disabled) return false;
        if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
        return true;
    };

    const findElement = (selectors, opts = { visible: false, enabled: false, last: false }) => {
        for (const sel of selectors || []) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            if (opts.last) {
                nodes = nodes.reverse();
            }
            for (const el of nodes) {
                if (opts.visible && !isVisible(el)) continue;
                if (opts.enabled && !isEnabled(el)) continue;
                return el;
            }
        }
        return null;
    };

    const findGeminiInput = () => {
        for (const host of Array.from(document.querySelectorAll('rich-textarea'))) {
            if (!isVisible(host)) continue;
            let inner = host.querySelector('[contenteditable]');
            if (inner && isVisible(inner)) return inner;
            if (host.shadowRoot) {
                inner = host.shadowRoot.querySelector('[contenteditable]');
                if (inner) return inner;
            }
        }
        for (const el of Array.from(document.querySelectorAll('[contenteditable]'))) {
            if (isVisible(el)) return el;
        }
        return null;
    };

    const dismissPopups = async () => {
        const labels = ['i agree', 'agree', 'got it', 'continue', 'accept', 'ok', 'dismiss', 'close', 'no thanks'];
        for (const btn of document.querySelectorAll('button')) {
            if (!isVisible(btn)) continue;
            const text = normalizeText(btn.textContent).toLowerCase();
            if (labels.includes(text)) {
                try { btn.click(); await sleep(200); } catch (_) {}
            }
        }
    };

    const inferExtension = (mime) => {
        const m = String(mime || '').toLowerCase();
        const extMap = {
            'image/png': 'png',
            'image/jpeg': 'jpg',
            'image/jpg': 'jpg',
            'image/webp': 'webp',
            'image/gif': 'gif',
            'image/svg+xml': 'svg',
            'application/pdf': 'pdf',
            'text/plain': 'txt',
            'text/markdown': 'md',
            'application/json': 'json',
            'application/zip': 'zip',
        };
        if (extMap[m]) return extMap[m];
        if (m.includes('/')) {
            const tail = m.split('/')[1].split('+')[0];
            if (tail) return tail;
        }
        return 'bin';
    };

    const decodeBase64 = (b64) => {
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes;
    };

    const makeFileFromAttachment = (att, idx) => {
        const mime = String(att?.mime_type || 'application/octet-stream');
        const dataB64 = String(att?.data_base64 || '');
        if (!dataB64) return null;
        const bytes = decodeBase64(dataB64);
        const blob = new Blob([bytes], { type: mime });
        const fallbackName = `attachment_${idx + 1}.${inferExtension(mime)}`;
        const name = String(att?.name || fallbackName).trim() || fallbackName;
        return new File([blob], name, { type: mime });
    };

    const injectAttachments = async (cfg, inputEl) => {
        const source = Array.isArray(__kimAttachments) ? __kimAttachments : [];
        if (!source.length) return 0;

        const files = [];
        for (let i = 0; i < source.length; i++) {
            try {
                const file = makeFileFromAttachment(source[i], i);
                if (file) files.push(file);
            } catch (_) {}
        }
        if (!files.length) return 0;

        const findFileInput = () => {
            for (const sel of cfg.file_input_selectors || []) {
                try {
                    const el = document.querySelector(sel);
                    if (el && el instanceof HTMLInputElement && el.type === 'file') {
                        return el;
                    }
                } catch (_) {}
            }
            return null;
        };

        let fileInput = findFileInput();
        if (!fileInput) {
            const uploadBtn = findElement(cfg.upload_button_selectors, { visible: true, enabled: true, last: true });
            if (uploadBtn) {
                try {
                    uploadBtn.click();
                    await sleep(280);
                    fileInput = findFileInput();
                } catch (_) {}
            }
        }

        if (fileInput) {
            const dt = new DataTransfer();
            for (const file of files) {
                dt.items.add(file);
            }

            try {
                fileInput.files = dt.files;
            } catch (_) {
                try {
                    Object.defineProperty(fileInput, 'files', {
                        value: dt.files,
                        configurable: true,
                    });
                } catch (_) {}
            }

            fileInput.dispatchEvent(new Event('input', { bubbles: true }));
            fileInput.dispatchEvent(new Event('change', { bubbles: true }));
            await sleep(700);
            return files.length;
        }

        // Fallback: image clipboard paste via ClipboardEvent('paste').
        // Synthetic KeyboardEvent('keydown', {key:'v'}) does NOT trigger the
        // browser's native paste handler — only a real ClipboardEvent with
        // clipboardData will be picked up by ProseMirror / React editors.
        const imageFile = files.find(f => String(f.type || '').startsWith('image/'));
        if (imageFile && inputEl) {
            try {
                inputEl.focus();
                await sleep(300);

                // Build a DataTransfer with the image file
                const dt2 = new DataTransfer();
                dt2.items.add(imageFile);

                // Dispatch a real ClipboardEvent('paste') — this is what editors listen for
                const pasteEvent = new ClipboardEvent('paste', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt2,
                });
                inputEl.dispatchEvent(pasteEvent);
                __kimDbg('H1', 'Dispatched ClipboardEvent paste with image', { name: imageFile.name, type: imageFile.type });

                // Give the UI time to process the pasted image (thumbnail render, upload)
                let waited = 0;
                while (waited < 5000) {
                    await sleep(200);
                    waited += 200;
                    if (document.querySelector('img[src^="blob:"], img[src^="data:"], file-attachment, thumbnail-view')) {
                        break;
                    }
                }

                // Dismiss any consent popups (like Gemini's "I agree" dialog)
                await dismissPopups();

                return 1;
            } catch (e) {
                __kimDbg('H1', 'ClipboardEvent paste fallback failed', { error: String(e) });
            }
        }

        return 0;
    };

    const normalizeText = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/[“”]/g, '"').replace(/[‘’]/g, "'").replace(/[—–]/g, '--').replace(/…/g, '...').trim();

    const selectorCounts = (selectors) => {
        const out = {};
        for (const sel of selectors || []) {
            try {
                out[sel] = document.querySelectorAll(sel).length;
            } catch (_) {
                out[sel] = -1;
            }
        }
        return out;
    };

    const isLikelyUserNode = (node) => {
        if (!node) return false;
        try {
            if (node.closest(
                'user-query, [data-message-author-role="user"], [data-role="user"], [data-author="user"], '
                + '.user-message, .from-user, .query-content, .prompt-bubble, [data-testid*="user-message"]'
            )) {
                return true;
            }
        } catch (_) {}
        try {
            const author = String(
                node.getAttribute?.('data-message-author-role')
                || node.getAttribute?.('data-role')
                || node.getAttribute?.('data-author')
                || ''
            ).toLowerCase();
            if (author === 'user') return true;
        } catch (_) {}
        try {
            const cls = String(node.className || '').toLowerCase();
            if (cls.includes('user') && !cls.includes('assistant')) return true;
        } catch (_) {}
        return false;
    };

    const readInputText = (inputEl) => {
        if (!inputEl) return '';
        if (inputEl instanceof HTMLTextAreaElement || inputEl instanceof HTMLInputElement) {
            return normalizeText(inputEl.value || '');
        }
        return normalizeText(inputEl.innerText || inputEl.textContent || '');
    };

    const promptMatchesInput = (inputEl, promptText) => {
        const expectedRaw = String(promptText || '');
        const actual = readInputText(inputEl);
        const expected = normalizeText(expectedRaw);
        if (!expected) return true;
        if (actual === expected) return true;
        if (expected.length < 500) return false;
        if (actual.length < Math.floor(expected.length * 0.98)) return false;
        
        const fuzzyMatch = (a, b) => a.replace(/\W+/g, '') === b.replace(/\W+/g, '');
        return fuzzyMatch(actual.slice(0, 220), expected.slice(0, 220))
            && fuzzyMatch(actual.slice(-220), expected.slice(-220));
    };

    const injectPromptText = async (inputEl, promptText) => {
        const target = String(promptText || '');
        try {
            inputEl.focus();
            document.execCommand('selectAll', false);
            if (document.execCommand('paste')) {
                await sleep(120);
                if (promptMatchesInput(inputEl, target)) return readInputText(inputEl).length;
            }
        } catch (_) {}
        if (inputEl instanceof HTMLTextAreaElement || inputEl instanceof HTMLInputElement) {
            const proto = Object.getPrototypeOf(inputEl);
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(inputEl, ''); else inputEl.value = '';
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
            if (setter) setter.call(inputEl, target); else inputEl.value = target;
            inputEl.dispatchEvent(new Event('input', { bubbles: true }));
            inputEl.dispatchEvent(new Event('change', { bubbles: true }));
            return readInputText(inputEl).length;
        }

        let inserted = false;
        try {
            inputEl.focus();
            document.execCommand('selectAll', false);
            inserted = document.execCommand('insertText', false, target);
        } catch (_) {}

        const currentText = readInputText(inputEl);
        if (!inserted || currentText.length < Math.min(8, target.length)) {
            inputEl.textContent = '';
            const lines = target.split('\n');
            for (const line of lines) {
                const div = document.createElement('div');
                div.textContent = line;
                inputEl.appendChild(div);
            }
        }

        // Gemini rich-textarea keeps its source-of-truth in a shadow-DOM textarea.
        // querySelector on the host element cannot reach shadow DOM; check shadowRoot too.
        try {
            const rich = inputEl.closest('rich-textarea');
            if (rich) {
                const mirror = rich.querySelector('textarea, input')
                    || (rich.shadowRoot && rich.shadowRoot.querySelector('textarea, input'));
                if (mirror && (mirror instanceof HTMLTextAreaElement || mirror instanceof HTMLInputElement)) {
                    const proto = Object.getPrototypeOf(mirror);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(mirror, target); else mirror.value = target;
                    mirror.dispatchEvent(new Event('input', { bubbles: true }));
                    mirror.dispatchEvent(new Event('change', { bubbles: true }));
                    mirror.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }
        } catch (_) {}

        try {
            inputEl.dispatchEvent(new InputEvent('beforeinput', {
                data: target,
                inputType: 'insertText',
                bubbles: true,
                cancelable: true,
            }));
        } catch (_) {}
        try {
            inputEl.dispatchEvent(new InputEvent('input', {
                data: target,
                inputType: 'insertText',
                bubbles: true,
                cancelable: true,
            }));
        } catch (_) {}
        inputEl.dispatchEvent(new Event('input', { bubbles: true }));
        inputEl.dispatchEvent(new Event('change', { bubbles: true }));
        await sleep(120);
        return readInputText(inputEl).length;
    };

    const gatherResponseCandidates = (cfg, siteKey) => {
        const candidates = [];
        const seenNodes = new Set();
        for (const sel of cfg.response_selectors || []) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            for (const node of nodes) {
                if (!node || seenNodes.has(node)) continue;
                seenNodes.add(node);
                if (!isVisible(node)) continue;
                if (isLikelyUserNode(node)) continue;

                // Gemini is especially noisy: only trust model-response subtree.
                if (siteKey === 'gemini') {
                    const isModelResponse = (
                        (node.matches && node.matches('model-response'))
                        || (node.closest && node.closest('model-response'))
                    );
                    if (!isModelResponse) continue;
                }

                const text = normalizeText(node.innerText || node.textContent || '');
                if (!text || text.length < 3) continue;

                candidates.push({
                    node,
                    selector: sel,
                    text,
                    key: `${sel}::${text.length}::${text.slice(0, 200)}::${text.slice(-200)}`,
                });
            }
        }

        candidates.sort((a, b) => {
            if (a.node === b.node) return 0;
            const pos = a.node.compareDocumentPosition(b.node);
            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
            if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
            return 0;
        });

        return candidates;
    };

    const getGeminiLatestResponseText = () => {
        const modelResponses = Array.from(document.querySelectorAll('model-response')).filter(node => {
            return !!node && isVisible(node) && !isLikelyUserNode(node);
        });

        let bestText = '';
        let bestModelNode = null;
        let bestModelIndex = -1;

        const chooseBest = (text, modelNode, modelIndex) => {
            if (!text) return;
            if (modelIndex > bestModelIndex) {
                bestText = text;
                bestModelNode = modelNode;
                bestModelIndex = modelIndex;
                return;
            }
            if (modelIndex === bestModelIndex && text.length > bestText.length) {
                bestText = text;
                bestModelNode = modelNode;
                bestModelIndex = modelIndex;
            }
        };

        for (const sel of [
            'model-response',
            'model-response message-content',
            'model-response .response-content',
            'message-content',
            'div.response-content',
        ]) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            for (const node of nodes) {
                if (!node || !isVisible(node) || isLikelyUserNode(node)) continue;

                const modelNode = (node.matches && node.matches('model-response'))
                    ? node
                    : (node.closest ? node.closest('model-response') : null);
                if (!modelNode || !isVisible(modelNode) || isLikelyUserNode(modelNode)) continue;

                const modelIndex = modelResponses.indexOf(modelNode);
                if (modelIndex < 0) continue;

                const text = normalizeText(node.innerText || node.textContent || '');
                chooseBest(text, modelNode, modelIndex);
            }
        }

        if (!bestText && modelResponses.length > 0) {
            const lastIndex = modelResponses.length - 1;
            const lastNode = modelResponses[lastIndex];
            bestText = normalizeText(lastNode.innerText || lastNode.textContent || '');
            bestModelNode = lastNode;
            bestModelIndex = lastIndex;
        }

        return {
            text: bestText,
            modelNode: bestModelNode,
            modelIndex: bestModelIndex,
        };
    };

    const captureResponseState = (cfg, siteKey) => {
        if (siteKey === 'gemini') {
            const snapshot = getGeminiLatestResponseText();
            const latestText = snapshot?.text || '';
            const latestNodeIndex = Number.isInteger(snapshot?.modelIndex) ? snapshot.modelIndex : -1;
            return {
                count: latestNodeIndex >= 0 ? latestNodeIndex + 1 : (latestText ? 1 : 0),
                keys: latestNodeIndex >= 0 ? ['gemini-node-index::' + latestNodeIndex] : [],
                latestText,
                latestNodeIndex,
            };
        }
        const candidates = gatherResponseCandidates(cfg, siteKey);
        return {
            count: candidates.length,
            keys: candidates.map(c => c.key),
            latestText: candidates.length > 0 ? candidates[candidates.length - 1].text : '',
        };
    };

    const hasNewResponseSince = (baselineState, currentState) => {
        // A valid current Gemini node index means we have a response node now.
        // Treat it as new if baseline had no valid node, or if the index advanced.
        if (
            Number.isInteger(currentState?.latestNodeIndex) &&
            currentState.latestNodeIndex >= 0 &&
            (
                !Number.isInteger(baselineState?.latestNodeIndex) ||
                baselineState.latestNodeIndex < 0 ||
                currentState.latestNodeIndex > baselineState.latestNodeIndex
            )
        ) return true;

        if ((currentState?.count || 0) > (baselineState?.count || 0)) return true;
        const baselineKeys = new Set((baselineState?.keys || []));
        if ((currentState?.keys || []).some(k => !baselineKeys.has(k))) return true;
        const baselineText = normalizeText(baselineState?.latestText || '');
        const currentText = normalizeText(currentState?.latestText || '');
        return !!currentText && currentText !== baselineText;
    };

    const extractLatestResponseText = (cfg, siteKey) => {
        return captureResponseState(cfg, siteKey).latestText;
    };

    const hasStopSemantics = (el) => {
        const label = String(
            (el && el.getAttribute && el.getAttribute('aria-label'))
            || (el && el.textContent)
            || ''
        ).toLowerCase().trim();
        if (!label) return false;
        return /(^|\b)stop(\b|$)/i.test(label) || label.includes('stop generating');
    };

    const isAnyStopVisible = (cfg) => {
        for (const sel of cfg.stop_selectors || []) {
            try {
                const nodes = Array.from(document.querySelectorAll(sel));
                for (const el of nodes) {
                    if (el && isVisible(el) && hasStopSemantics(el)) {
                        return true;
                    }
                }
            } catch (_) {}
        }
        return false;
    };

  try {
    const siteKey = SITE_CONFIGS[__kimSite] ? __kimSite : 'claude';
    const cfg = SITE_CONFIGS[siteKey];
        const selectorDiag = {
            input: selectorCounts(cfg.input_selectors),
            send: selectorCounts(cfg.send_selectors),
            stop: selectorCounts(cfg.stop_selectors),
            response: selectorCounts(cfg.response_selectors),
        };
        const selectorDiagText = JSON.stringify(selectorDiag);

        __kimDbg('H2', 'selector diagnostics', {
            siteKey,
            ...selectorDiag,
        });

        const baselineState = captureResponseState(cfg, siteKey);
        const initialResponseText = baselineState.latestText;
    // #region agent log
        __kimDbg('H2', 'bridge run start', {
            siteKey,
            baselineCount: baselineState.count,
            initialResponseTextLen: (initialResponseText || '').length,
        });
    // #endregion

                const inputEl = (siteKey === 'gemini' ? findGeminiInput() : null) || findElement(cfg.input_selectors, { visible: true, enabled: false });
        if (!inputEl) {
            throw new Error(`Could not find input selector for ${siteKey}. selectorDiag=${selectorDiagText}`);
    }
    inputEl.focus();

        if (siteKey === 'gemini') {
            const rawTier = (window.__kimModelTier == null ? '' : String(window.__kimModelTier)).trim().toLowerCase();
            const tier = rawTier === 'advanced' ? 'pro' : (rawTier === 'fast' ? 'flash' : rawTier);
            const hasExplicitTier = tier === 'flash' || tier === 'pro' || tier === 'thinking';
            if (!hasExplicitTier) {
                __kimDbg('H1', 'Gemini model auto-switch skipped (no explicit model tier)');
            } else {
            const selectGeminiPro = async () => {
                let modelBtn = null;
                const walker1 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                while (walker1.nextNode()) {
                    const node = walker1.currentNode;
                    const txt = normalizeText(node.textContent).toLowerCase().trim();
                    const words = txt.split(/\s+/);
                    
                    if (words.includes('gemini') || words.includes('flash') || words.includes('fast') || words.includes('pro') || words.includes('advanced') || words.includes('thinking')) {
                        if (!txt.includes('upgrade') && !txt.includes('try') && !txt.includes('learn') && !txt.includes('help') && !txt.includes('chat')) {
                            const el = node.parentElement;
                            if (el && isVisible(el)) {
                                const clickable = el.closest('button, [role="button"], [role="combobox"], [aria-haspopup], .mat-mdc-button, .mat-mdc-menu-trigger');
                                if (clickable) {
                                    modelBtn = clickable;
                                    break;
                                }
                            }
                        }
                    }
                }
                
                if (modelBtn) {
                    const currentTxt = normalizeText(modelBtn.textContent || '').toLowerCase();
                    const isTargetFlash = tier === 'flash' || tier === 'fast';
                    const isTargetPro = tier === 'pro' || tier === 'advanced';
                    const isTargetThinking = tier === 'thinking';
                    
                    let needsSwitch = false;
                    if (isTargetFlash && !currentTxt.includes('flash') && !currentTxt.includes('fast')) {
                        // If it's just 'gemini', it's likely flash.
                        if (currentTxt !== 'gemini') needsSwitch = true;
                    }
                    if (isTargetPro && !currentTxt.includes('advanced') && !currentTxt.includes('pro')) needsSwitch = true;
                    if (isTargetThinking && !currentTxt.includes('thinking')) needsSwitch = true;
                    
                    if (needsSwitch) {
                        __kimDbg('H1', 'Switching Gemini Model Tier', { current: currentTxt, target: tier });
                        modelBtn.click();
                        await sleep(700);
                        
                        let targetClicked = false;
                        // For Flash, if "fast" or "flash" is missing, clicking the basic "gemini" option is correct
                        const targetTerms = isTargetThinking ? ['thinking'] : (isTargetFlash ? ['flash', 'fast', 'gemini'] : ['advanced', 'pro']);
                        
                        // Search overlays first (dropdown menus usually attach to body end or cdk-overlay)
                        const overlays = document.querySelectorAll('.cdk-overlay-container, [role="menu"], [role="listbox"], [role="dialog"]');
                        const menuRoot = Array.from(overlays).find(o => isVisible(o)) || document.body;
                        
                        const walker2 = document.createTreeWalker(menuRoot, NodeFilter.SHOW_TEXT, null, false);
                        let targetNode = null;
                        while (walker2.nextNode()) {
                            const node = walker2.currentNode;
                            const txt = normalizeText(node.textContent).toLowerCase();
                            if (targetTerms.some(t => txt.includes(t)) && !txt.includes('upgrade') && !txt.includes('learn')) {
                                // For flash targeting "gemini", avoid matching "gemini advanced"
                                if (isTargetFlash && (txt.includes('advanced') || txt.includes('pro') || txt.includes('thinking'))) continue;
                                
                                const el = node.parentElement;
                                if (el && isVisible(el)) {
                                    targetNode = el;
                                    break;
                                }
                            }
                        }
                        
                        if (targetNode) {
                            const clickable = targetNode.closest('button, [role="button"], [role="menuitem"], [role="option"], a, li, mat-option') || targetNode;
                            clickable.click();
                            targetClicked = true;
                            await sleep(1200); // wait for page to reload/react
                        }

                        if (!targetClicked) {
                            __kimDbg('H1', 'Failed to find target Gemini model in menu', { target: tier });
                            document.body.click(); // Close menu
                            await sleep(200);
                        }
                    } else {
                        __kimDbg('H1', 'Gemini Model already correct', { current: currentTxt, target: tier });
                    }
                } else {
                    __kimDbg('H1', 'Gemini Model selector button not found');
                }
            };
            await selectGeminiPro();
            }
        }

        const uploadedCount = await injectAttachments(cfg, inputEl);

        const injectedLen = await injectPromptText(inputEl, __kimPrompt);
        if (injectedLen < Math.min(8, normalizeText(__kimPrompt).length) || !promptMatchesInput(inputEl, __kimPrompt)) {
            throw new Error('Prompt text was not fully accepted by the chat input. Refusing to send an incomplete prompt.');
        }

    await sleep(80);

        // Wait for the PREVIOUS request's completion hash before pressing
        // Enter.  This is the most reliable guard — the hash is embedded in
        // the response text itself and is provider-agnostic.
        {
            const prevHash = (window.__kimBridge && window.__kimBridge._lastHash) || null;
            if (prevHash) {
                const PRE_SEND_TIMEOUT = 120000;
                const preDeadline = Date.now() + PRE_SEND_TIMEOUT;
                while (Date.now() < preDeadline) {
                    const pageText = extractLatestResponseText(cfg, siteKey) || '';
                    if (pageText.includes(prevHash)) break;
                    await sleep(300);
                }
                // Short settle delay removed for latency
            }
        }

        // Record THIS request's hash so the NEXT request can wait for it
        if (__kimCompletionHash && window.__kimBridge) {
            window.__kimBridge._lastHash = __kimCompletionHash;
        }

        const stateBeforeSend = captureResponseState(cfg, siteKey);

        if (!promptMatchesInput(inputEl, __kimPrompt)) {
            throw new Error('Prompt changed after injection. Refusing to send a partial prompt.');
        }

        // Submit by clicking the provider's enabled Send button. Do not use
        // synthetic Enter events here: long prompts with newlines can be split
        // into partial provider messages if the editor has not fully committed.
        inputEl.focus();
        let sent = false;
        for (let attempt = 0; attempt < 20; attempt++) {
            const sendBtn = findElement(cfg.send_selectors, { visible: true, enabled: true });
            if (sendBtn) {
                try {
                    sendBtn.click();
                    sent = true;
                    break;
                } catch (_) {}
            }
            await sleep(200);
        }
        if (!sent) {
            throw new Error(`Could not find an enabled send button for ${siteKey}. Refusing to submit with Enter because it can split long prompts. selectorDiag=${selectorDiagText}`);
        }
        await sleep(400);
        // #region agent log
        __kimDbg('H1', 'send stage finished', { sent, hasForm: !!(inputEl && inputEl.closest && inputEl.closest('form')), inputTextLen: readInputText(inputEl).length });
        // #endregion

        const responseDeadline = Date.now() + RESPONSE_WAIT_MS;
                let brokeOnResponseSignal = false;
        while (Date.now() < responseDeadline) {
                ensureWithinDeadline('wait_response_start');
                        const currentState = captureResponseState(cfg, siteKey);
                        const hasNewText = hasNewResponseSince(baselineState, currentState);
                if (hasNewText) {
                brokeOnResponseSignal = true;
                break;
            }
            await sleep(450);
    }
        if (!brokeOnResponseSignal) {
            // Some providers update an existing node in-place without changing
            // the overall response count. Continue into fallback scraping.
            __kimDbg('H2', 'response signal timeout; continuing with fallback scrape', {
                baselineCount: baselineState.count,
                latestLen: (extractLatestResponseText(cfg, siteKey) || '').length,
            });
        }
    // #region agent log
        __kimDbg('H2', 'response wait finished', {
            brokeOnResponseSignal,
            latestLen: (extractLatestResponseText(cfg, siteKey) || '').length,
        });
    // #endregion

        let sawStop = isAnyStopVisible(cfg);
        const stopAppearDeadline = Date.now() + STOP_APPEAR_WAIT_MS;
        while (!sawStop && Date.now() < stopAppearDeadline) {
            ensureWithinDeadline('wait_stop_appear');
            const currentState = captureResponseState(cfg, siteKey);
            if ((currentState.latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) break;
            await sleep(250);
            sawStop = isAnyStopVisible(cfg);
        }

        if (sawStop || (captureResponseState(cfg, siteKey).latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) {
            const doneDeadline = Date.now() + GENERATION_DONE_WAIT_MS;
            while (Date.now() < doneDeadline) {
                ensureWithinDeadline('wait_generation_done');
                const currentState = captureResponseState(cfg, siteKey);
                if ((currentState.latestText || '').includes(__kimCompletionHash || '__KIMBRIDGE_DONE__')) break;
                if (!isAnyStopVisible(cfg)) break;
                await sleep(700);
            }
        }

    await sleep(400);

        let text = '';
        const readDeadline = Date.now() + READ_WAIT_MS;
        while (Date.now() < readDeadline) {
                        ensureWithinDeadline('read_response_text');
                        const currentState = captureResponseState(cfg, siteKey);
                        const candidate = currentState.latestText;
                                                const changed = candidate && (
                                                        hasNewResponseSince(baselineState, currentState)
                                                        || normalizeText(candidate) !== normalizeText(initialResponseText)
                                                );
                                                if (changed) {
                text = candidate;
                break;
            }
            await sleep(650);
    }

    if (!text) {
            const fallback = extractLatestResponseText(cfg, siteKey);
            if (fallback && normalizeText(fallback) !== normalizeText(initialResponseText)) {
                text = fallback;
            }
        }

        if (!text) {
      // #region agent log
            __kimDbg('H2', 'read failed empty text', { responseSelectors: cfg.response_selectors || [], initialResponseTextLen: (initialResponseText || '').length, finalCandidateLen: (extractLatestResponseText(cfg, siteKey) || '').length, sent });
      // #endregion
            throw new Error(`Could not read model response from page. selectorDiag=${selectorDiagText}`);
    }

    await emitPayload({ ok: true, response: text, site: siteKey, attachments_uploaded: uploadedCount || 0 });
  } catch (err) {
    const message = (err && err.message) ? err.message : String(err);
    // #region agent log
    __kimDbg('H3', 'bridge script catch', { message });
    // #endregion
    await emitPayload({ ok: false, error: message, site: __kimSite || 'unknown' });
  }
    } catch (fatalErr) {
        try {
            document.title = '__KIMBRIDGE_FATAL__:' + String((fatalErr && fatalErr.message) ? fatalErr.message : fatalErr);
        } catch (_) {}
    }
    }, 0);
})();
"#;
    let script = format!("{}{}", header, body);
    Ok(script)
}

/// Condvar-based bridge payload collector.
///
/// Primary path: waits on WEBVIEW_BRIDGE_NOTIFY condvar which is notified
/// instantly when the JS bridge sends an IPC event.  No polling, no title
/// hacks.  Falls back to legacy title-polling if IPC hasn't delivered after
/// a few seconds (for backward compat with old JS scripts).
fn collect_bridge_payload(
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
fn pull_payload_from_js_store_legacy(
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
fn run_bridge_completion_once(
    window: &tauri::WebviewWindow,
    site: &str,
    prompt: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
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

    // Check if persistent bridge wasn't available (title marker)
    std::thread::sleep(Duration::from_millis(100));
    let title_check = window.title().unwrap_or_default();
    if title_check == "__KIMBRIDGE_NO_PERSISTENT__" {
        // Restore title and fall back to the legacy full-script approach.
        let _ = window.eval("document.title = '';");
        agent_debug_log(
            "H2",
            "persistent bridge not available, falling back to legacy script",
            serde_json::json!({ "reqId": req_id }),
        );

        // Clear old req_id and generate a new one for the legacy path
        if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
            .get_or_init(|| StdMutex::new(HashMap::new()))
            .lock()
        {
            guard.remove(&req_id);
        }

        let script = build_bridge_complete_script(
            site, prompt, &req_id, attachments, callback_url, callback_token, completion_hash, model_tier,
        ).map_err(|e| format!("Script build failed: {}", e))?;

        if let Err(e) = window.eval(&script) {
            return Err(format!("Failed to evaluate legacy script: {}", e));
        }
    }

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

pub(crate) mod http_bridge;
use http_bridge::{capitalize, start_webview_bridge_server, start_bridge_file_watcher, show_screenshot_flash};

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------


#[tauri::command]
async fn open_browser_signin_window(
    url: String,
    provider_name: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_impl(&url, provider_name, &app_handle)
}

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

fn provider_origin(site: &str) -> Option<&'static str> {
    match site {
        "chatgpt" | "openai" => Some("https://chatgpt.com"),
        "claude" | "anthropic" => Some("https://claude.ai"),
        "gemini" | "google" => Some("https://gemini.google.com"),
        "deepseek" => Some("https://chat.deepseek.com"),
        "grok" => Some("https://grok.com"),
        _ => None,
    }
}

fn provider_login_url(site: &str) -> Option<String> {
    match site {
        "chatgpt" | "openai" => Some("https://chatgpt.com/auth/login".to_string()),
        "claude" | "anthropic" => Some("https://claude.ai/login".to_string()),
        "gemini" | "google" => Some(
            "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fgemini.google.com%2Fapp"
                .to_string(),
        ),
        "deepseek" => Some("https://chat.deepseek.com/sign_in".to_string()),
        "grok" => Some("https://grok.com/sign-in".to_string()),
        _ => None,
    }
}

fn build_auth_probe_js(site: &str, req_id: &str, base_url: &str, token: &str) -> String {
    let endpoint = match site {
        "chatgpt" | "openai" => "/api/auth/session",
        "claude" | "anthropic" => "/api/organizations",
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
                    }} else if (site === 'claude' || site === 'anthropic') {{
                        if (Array.isArray(data) && data.length > 0) {{
                            const org = data[0] || {{}};
                            info = {{
                                provider: 'claude',
                                signed_in: true,
                                email: org.email_address || org.billing_email || null,
                                name: org.name || null,
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

fn parse_auth_response(site: &str, result: &BridgeCompleteResponse) -> ProviderAuthStatus {
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
            signed_in: v.get("signed_in").and_then(|x| x.as_bool()).unwrap_or(false),
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
async fn provider_check_auth(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<ProviderAuthStatus, String> {
    let site = normalize_site(&provider);
    let origin = provider_origin(&site)
        .ok_or_else(|| format!("Unsupported provider: {}", provider))?;

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
    let probe_js = build_auth_probe_js(&site, &req_id, &cfg.base_url, &cfg.token);
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
    Ok(ProviderAuthStatus {
        provider: site,
        signed_in: false,
        email: None,
        name: None,
        avatar: None,
        error: Some("auth probe timed out".to_string()),
    })
}

fn post_signin_url_patterns(site: &str) -> Vec<&'static str> {
    match site {
        "chatgpt" | "openai" => vec!["chatgpt.com/c/", "chatgpt.com/?", "chatgpt.com/g/"],
        "claude" | "anthropic" => vec!["claude.ai/chat", "claude.ai/new", "claude.ai/chats", "claude.ai/projects"],
        "gemini" | "google" => vec!["gemini.google.com/app"],
        "deepseek" => vec!["chat.deepseek.com/a", "chat.deepseek.com/?"],
        "grok" => vec!["grok.com/chat", "grok.com/?"],
        _ => vec![],
    }
}

fn spawn_post_signin_watcher(site: String, app_handle: tauri::AppHandle) {
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
async fn provider_signin(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    let site = normalize_site(&provider);
    let login_url = provider_login_url(&site)
        .ok_or_else(|| format!("No login URL configured for provider: {}", provider))?;

    open_browser_signin_window_with_visibility(
        &login_url,
        Some(site.clone()),
        true,
        &app_handle,
    )?;
    // Force-show even if the window already existed and was hidden.
    show_browser_window_impl(&app_handle);

    spawn_post_signin_watcher(site.clone(), app_handle.clone());
    Ok(format!("Opened sign-in for {}", site))
}

#[tauri::command]
async fn provider_signout(
    provider: String,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    let site = normalize_site(&provider);
    let origin = provider_origin(&site)
        .ok_or_else(|| format!("Unsupported provider: {}", provider))?;

    // Each provider has its own logout flow; navigating the webview to the
    // logout endpoint causes the page to clear its auth cookies in-place.
    let logout_url = match site.as_str() {
        "chatgpt" | "openai" => format!("{}/auth/logout", origin),
        "claude" | "anthropic" => format!("{}/login?logout=true", origin),
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
pub(crate) use subprocess::{find_python_interpreter, send_task, cancel_task, process_exists, send_signal};

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
    let config_path = config_yaml_path(None);
    let config = config::load_config(&config_path);

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
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
        .manage(config)
        .invoke_handler(tauri::generate_handler![
            session_commands::list_sessions,
            session_commands::delete_sessions,
            session_commands::load_session_messages,
            session_commands::summarize_session,
            run_history::save_run_history,
            run_history::load_run_history,
            session_commands::get_app_version,
            run_history::get_platform_info,
            run_history::run_update,
            add_custom_provider_capability,
            open_browser_signin_window,
            navigate_browser_window_if_open,
            get_browser_current_url,
            session_browser_meta_read,
            session_browser_meta_write,
            session_browser_url_commit,
            restore_browser_for_session,
            show_browser_window,
            hide_browser_window,
            set_browser_keep_visible,
            provider_check_auth,
            provider_signin,
            provider_signout,
            hide_main_window,
            show_main_window,
            set_task_active_mode,
            send_task,
            cancel_task,
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;

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
