use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{Emitter, Listener, Manager, State};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use tokio::sync::Mutex;

mod google_oauth;

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
    /// injecting the prompt. BrowserProvider uses this for Claw bridge relays
    /// because each relay prompt already contains the full Claw conversation;
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
const BRIDGE_COMPLETION_TIMEOUT_S: u64 = 720;
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
    pub session_type: String, // "kim" or "claw"
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
pub struct CompletedClawSession {
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

fn default_project_root() -> PathBuf {
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

fn default_sessions_dir() -> PathBuf {
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

fn find_python_interpreter(project_root: &Path) -> Result<String, String> {
    let candidates = [
        project_root.join("venv").join("bin").join("python"),
        project_root.join(".venv").join("bin").join("python"),
        project_root.join("venv").join("Scripts").join("python.exe"),
        project_root.join(".venv").join("Scripts").join("python.exe"),
    ];

    for candidate in candidates {
        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
    }

    #[cfg(target_os = "windows")]
    let cmd_candidates = ["py", "python", "python3"];
    #[cfg(not(target_os = "windows"))]
    let cmd_candidates = ["python3", "python"];

    for cmd in cmd_candidates {
        if command_exists(cmd) {
            return Ok(cmd.to_string());
        }
    }

    Err(
        "No Python interpreter found. Install Python 3 or create a project venv (venv/.venv)."
            .to_string(),
    )
}

/// Validate that a user-supplied `session_id` is a safe file-stem:
/// no path separators, no `..`, printable ASCII-ish. Prevents a caller
/// from escaping the per-date directory via `../../etc/passwd` etc.
fn validate_session_id(session_id: &str) -> Result<(), String> {
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


fn browser_session_meta_filename(session_id: &str) -> String {
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

fn session_base_dir(session_type: &str, kim_dir: Option<String>, claw_dir: Option<String>) -> PathBuf {
    if session_type == "claw" {
        claw_dir.map(PathBuf::from).unwrap_or_else(default_sessions_dir)
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

fn chrono_like_today() -> String {
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

fn read_sessions_from_dir(base: &Path, session_type: &str) -> Result<Vec<SessionInfo>, String> {
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

fn count_lines(path: &Path) -> std::io::Result<usize> {
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

fn parse_jsonl(path: &Path) -> Result<Vec<KimMessage>, String> {
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
                    .and_then(claw_jsonl_line_to_kim_message)
                {
                    Some(msg) => messages.push(msg),
                    None => eprintln!("Skipping malformed JSONL line {}: {}", i + 1, e),
                }
            }
        }
    }

    Ok(messages)
}

fn claw_jsonl_line_to_kim_message(value: serde_json::Value) -> Option<KimMessage> {
    if value.get("type").and_then(|v| v.as_str()) != Some("message") {
        return None;
    }

    let message = value.get("message")?;
    let role = message.get("role")?.as_str()?.to_string();
    let content = message
        .get("blocks")
        .cloned()
        .map(normalize_claw_blocks)
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

fn normalize_claw_blocks(blocks: serde_json::Value) -> serde_json::Value {
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

fn infer_session_title(session_file: &Path, summary: Option<&String>, session_id: &str) -> String {
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
        .map(|mut stdin| stdin.write_all(&bytes).is_ok())
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

const PERSISTENT_BRIDGE_JS: &str = r#"
(() => {
  // Guard: only install once per page load.
  if (window.__kimBridge && window.__kimBridge._v >= 10) return;

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
      send_selectors: ["button[aria-label*='Send message']", "button[aria-label*='Send']", "button[data-testid*='send']", "button[mattooltip*='Send']", "button[aria-label*='Submit prompt']", "button[aria-label*='Submit']"],
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

  // ── Helpers ──────────────────────────────────────────────────────────
  const normalizeText = (v) => String(v || '').replace(/\u00a0/g, ' ').replace(/[“”]/g, '"').replace(/[‘’]/g, "'").replace(/[—–]/g, '--').replace(/…/g, '...').trim();

  const isVisible = (el) => {
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
    if (el.offsetParent !== null) return true;
    return style.position === 'fixed';
  };

  const isEnabled = (el) => {
    if (!el) return false;
    if ('disabled' in el && el.disabled) return false;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return false;
    return true;
  };

  const findElement = (selectors, opts = {}) => {
    for (const sel of selectors || []) {
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(sel)); } catch (_) { continue; }
      for (const el of nodes) {
        if (opts.visible && !isVisible(el)) continue;
        if (opts.enabled && !isEnabled(el)) continue;
        return el;
      }
    }
    return null;
  };

  // Shadow-DOM-aware input finder for Gemini's rich-textarea component.
  // document.querySelectorAll cannot pierce shadow roots, so we check both
  // the light DOM children and the shadow root of each rich-textarea host.
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
    // Broad fallback: any visible contenteditable on the page
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
        try { btn.click(); await new Promise(r => setTimeout(r, 200)); } catch (_) {}
      }
    }
  };

  const hasStopSemantics = (el) => {
    const label = String((el && el.getAttribute && el.getAttribute('aria-label')) || (el && el.textContent) || '').toLowerCase().trim();
    if (!label) return false;
    return /(^|\b)stop(\b|$)/i.test(label) || label.includes('stop generating');
  };

  const isAnyStopVisible = (cfg) => {
    for (const sel of cfg.stop_selectors || []) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        for (const el of nodes) {
          if (el && isVisible(el) && hasStopSemantics(el)) return true;
        }
      } catch (_) {}
    }
    return false;
  };

  const isLikelyUserNode = (node) => {
    if (!node) return false;
    try {
      if (node.closest(
        'user-query, [data-message-author-role="user"], [data-role="user"], [data-author="user"], '
        + '.user-message, .from-user, .query-content, .prompt-bubble, [data-testid*="user-message"]'
      )) return true;
    } catch (_) {}
    try {
      const author = String(node.getAttribute?.('data-message-author-role') || node.getAttribute?.('data-role') || node.getAttribute?.('data-author') || '').toLowerCase();
      if (author === 'user') return true;
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

  // ── IPC emit (Tauri native) ──────────────────────────────────────────
  const ipcEmit = (payload) => {
    try {
      if (window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke) {
        window.__TAURI_INTERNALS__.invoke('plugin:event|emit', {
          event: 'kim-bridge-ipc',
          payload: payload,
        });
        return true;
      }
    } catch (_) {}

    try {
      // Direct WebKit IPC fallback for external domains where __TAURI_INTERNALS__ is not injected
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.ipc) {
        window.webkit.messageHandlers.ipc.postMessage({
          cmd: "plugin:event|emit",
          callback: 0,
          error: 0,
          payload: {
            event: "kim-bridge-ipc",
            payload: payload
          }
        });
        return true;
      }
    } catch (e) {
      console.warn('[KimBridge] WebKit IPC emit failed:', e);
    }

    return false;
  };

  // ── Response extraction ──────────────────────────────────────────────
  const getLatestResponseText = (cfg, siteKey) => {
    if (siteKey === 'gemini') {
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
        }

        return bestText;
    }

    // Generic: find last visible non-user response node
    for (const sel of cfg.response_selectors || []) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        // Iterate backwards to get the most recent message bubble
        for (let i = nodes.length - 1; i >= 0; i--) {
          const node = nodes[i];
          if (!node || !isVisible(node) || isLikelyUserNode(node)) continue;
          const text = normalizeText(node.innerText || node.textContent || '');
          if (text) return text;
        }
      } catch (_) {}
    }
    return '';
  };

  const countResponseNodes = (cfg, siteKey) => {
    if (siteKey === 'gemini') {
      return Array.from(document.querySelectorAll('model-response')).filter(n => !!n && isVisible(n) && !isLikelyUserNode(n)).length;
    }
    let count = 0;
    const seen = new Set();
    for (const sel of cfg.response_selectors || []) {
      try {
        const nodes = Array.from(document.querySelectorAll(sel));
        for (const n of nodes) {
          if (!n || seen.has(n) || !isVisible(n) || isLikelyUserNode(n)) continue;
          seen.add(n);
          count++;
        }
      } catch (_) {}
    }
    return count;
  };

  // ── Prompt injection ─────────────────────────────────────────────────
  const injectPromptText = async (inputEl, promptText) => {
    const target = String(promptText || '');
    try {
      inputEl.focus();
      document.execCommand('selectAll', false);
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
      // Strategy 3: Synthetic ClipboardEvent for ProseMirror (Claude)
      try {
        const dt = new DataTransfer();
        dt.setData('text/plain', target);
        const pasteEvent = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: dt });
        inputEl.dispatchEvent(pasteEvent);
      } catch (e) {
        console.warn('[KimBridge] Synthetic paste failed:', e);
      }
    }
    // Gemini rich-textarea mirror sync
    try {
      const rich = inputEl.closest('rich-textarea');
      if (rich) {
        const mirror = rich.querySelector('textarea, input') || (rich.shadowRoot && rich.shadowRoot.querySelector('textarea, input'));
        if (mirror && (mirror instanceof HTMLTextAreaElement || mirror instanceof HTMLInputElement)) {
          const proto = Object.getPrototypeOf(mirror);
          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(mirror, target); else mirror.value = target;
          mirror.dispatchEvent(new Event('input', { bubbles: true }));
          mirror.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
    } catch (_) {}
    try {
      inputEl.dispatchEvent(new InputEvent('input', { data: target, inputType: 'insertText', bubbles: true, cancelable: true }));
    } catch (_) {}
    inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    inputEl.dispatchEvent(new Event('change', { bubbles: true }));
    return readInputText(inputEl).length;
  };

  // ── Attachment injection ──────────────────────────────────────────────
  const decodeBase64 = (b64) => {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  };

  const inferExtension = (mime) => {
    const m = String(mime || '').toLowerCase();
    const map = { 'image/png':'png', 'image/jpeg':'jpg', 'image/webp':'webp', 'image/gif':'gif', 'application/pdf':'pdf', 'text/plain':'txt' };
    return map[m] || (m.includes('/') ? m.split('/')[1].split('+')[0] : 'bin');
  };

  const injectAttachments = async (cfg, inputEl, attachments) => {
    if (!attachments || !attachments.length) return 0;
    const files = [];
    for (let i = 0; i < attachments.length; i++) {
      try {
        const att = attachments[i];
        const mime = String(att?.mime_type || 'application/octet-stream');
        const b64 = String(att?.data_base64 || '');
        if (!b64) continue;
        const bytes = decodeBase64(b64);
        const blob = new Blob([bytes], { type: mime });
        const name = String(att?.name || `attachment_${i+1}.${inferExtension(mime)}`).trim();
        files.push(new File([blob], name, { type: mime }));
      } catch (_) {}
    }
    if (!files.length) return 0;

    // Find file input
    let fileInput = null;
    for (const sel of cfg.file_input_selectors || []) {
      try {
        const el = document.querySelector(sel);
        if (el && el instanceof HTMLInputElement && el.type === 'file') { fileInput = el; break; }
      } catch (_) {}
    }
    if (!fileInput) {
      for (const sel of cfg.upload_button_selectors || []) {
        try {
          const btn = document.querySelector(sel);
          if (btn) { btn.click(); await new Promise(r => setTimeout(r, 100)); }
          for (const fsel of cfg.file_input_selectors || []) {
            const fi = document.querySelector(fsel);
            if (fi && fi instanceof HTMLInputElement && fi.type === 'file') { fileInput = fi; break; }
          }
          if (fileInput) break;
        } catch (_) {}
      }
    }
    if (fileInput) {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(f);
      try { fileInput.files = dt.files; } catch (_) {
        try { Object.defineProperty(fileInput, 'files', { value: dt.files, configurable: true }); } catch (_) {}
      }
      fileInput.dispatchEvent(new Event('input', { bubbles: true }));
      fileInput.dispatchEvent(new Event('change', { bubbles: true }));
      await new Promise(r => setTimeout(r, 250));
      return files.length;
    }

    // Fallback: image paste. Two strategies in order:
    //
    // 1. document.execCommand('paste') — reads from the REAL system clipboard that
    //    the Rust bridge pre-populated via osascript before evaluating this script.
    //    This triggers a TRUSTED paste event (isTrusted: true) that Gemini's editor
    //    accepts.  Synthetic ClipboardEvent is always isTrusted: false and is silently
    //    rejected by Gemini's React/Lit frontend.
    //
    // 2. ClipboardEvent fallback — kept for sites (Claude, ChatGPT) that do honour
    //    synthetic paste events.
    //
    // Returns 1 only when a thumbnail appears, 0 when all methods fail.
    const imageFile = files.find(f => String(f.type || '').startsWith('image/'));
    if (imageFile && inputEl) {
      try {
        inputEl.focus();
        await new Promise(r => setTimeout(r, 100));

        // Strategy 1: synthetic ClipboardEvent (isTrusted: false — may be ignored by some editors)
        const dt = new DataTransfer();
        dt.items.add(imageFile);
        const pasteEvent = new ClipboardEvent('paste', {
          bubbles: true,
          cancelable: true,
          clipboardData: dt,
        });
        inputEl.dispatchEvent(pasteEvent);
        console.log('[KimBridge] ClipboardEvent paste dispatched:', imageFile.name);

        let waited = 0;
        let thumbnailFound = false;
        while (waited < 1000) {
          await new Promise(r => setTimeout(r, 100));
          waited += 100;
          if (document.querySelector('img[src^="blob:"], img[src^="data:"], file-attachment, thumbnail-view')) {
            thumbnailFound = true;
            break;
          }
        }
        if (thumbnailFound) {
          await dismissPopups();
          console.log('[KimBridge] ClipboardEvent paste succeeded');
          return 1;
        }

        // All methods failed — return 0 so send() can append a fallback note
        console.warn('[KimBridge] All paste strategies failed after 2.5s — returning 0');
        return 0;
      } catch (e) {
        console.warn('[KimBridge] injectAttachments error:', e);
      }
    }
    return 0;
  };

  // ── Main send function ───────────────────────────────────────────────
  const send = async (prompt, reqId, site, attachments, callbackUrl, completionHash, modelTier) => {
    window.__kimModelTier = modelTier;
    const siteKey = SITE_CONFIGS[site] ? site : 'claude';
    const cfg = SITE_CONFIGS[siteKey];
    const baselineResponseCount = countResponseNodes(cfg, siteKey);
    const baselineResponseText = getLatestResponseText(cfg, siteKey) || '';
    const HARD_TIMEOUT = 120000;
    const hardDeadlineAt = Date.now() + HARD_TIMEOUT;

    const reportResult = (payload) => {
      // 1. Try Tauri/WebKit IPC
      if (!ipcEmit(payload)) {
        // 2. Fallback: Image Ping to the Rust bridge orchestrator (CSP-safe)
        if (callbackUrl) {
          try {
            const pingUrl = callbackUrl.replace('/callback', '/ping');
            const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
            const img = new Image();
            img.src = `${pingUrl}?req_id=${encodeURIComponent(reqId)}&data=${encodeURIComponent(encoded)}`;
          } catch (e) {
            console.warn('[KimBridge] ping fallback failed:', e);
          }
        }
      }

      // 3. Fallback: Legacy store (for title polling). Only final payloads
      // go here; live progress is display-only and must never satisfy Rust's
      // completion collector.
      if (!payload.event || payload.event === 'done' || payload.event === 'error') {
        try {
          const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
          if (typeof window.__kimBridgeStore !== 'object' || window.__kimBridgeStore === null) window.__kimBridgeStore = {};
          window.__kimBridgeStore[reqId] = { ready: true, data: encoded, err: null, ts: Date.now() };
        } catch (_) {}
      }
    };

    const decodeJsonString = (value) => {
      try { return JSON.parse('"' + String(value || '').replace(/"/g, '\\"') + '"'); } catch (_) {}
      return String(value || '').replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\t/g, '\t');
    };

    // extractBridgeTextField removed (was dead code returning empty string)

    const cleanProgressText = (raw) => {
      let text = String(raw || '');
      if (completionHash) text = text.replace(completionHash, '');
      text = text.replace(/KIM_[a-f0-9]{8}/gi, '').trim();
      return text;
    };

    let lastProgressText = '';
    let lastProgressAt = 0;
    const reportProgress = (raw) => {
      const text = cleanProgressText(raw);
      if (!text || text === lastProgressText) return;
      const now = Date.now();
      if (now - lastProgressAt < 800 && text.length <= lastProgressText.length + 12) return;
      lastProgressText = text;
      lastProgressAt = now;
      const payload = { ok: true, event: 'progress', req_id: reqId, site: siteKey, text };
      ipcEmit(payload);
    };

    // Mark this as the current in-flight request. Any earlier send() call
    // still running will detect that it's been superseded and bail out
    // before clobbering shared state (input box, _lastHash, response polling).
    window.__kimBridge._currentReqId = reqId;
    const isSuperseded = () => window.__kimBridge._currentReqId !== reqId;

    try {
      // 0. Auto-enable DeepThink (R1) expert mode for DeepSeek
      if (siteKey === 'deepseek') {
        try {
          if (!window.__kimBridge._expertToggled) {
            const allEls = Array.from(document.querySelectorAll('*'));
            const expertText = allEls.find(el => {
              // Ensure we get the actual leaf element (like the span), not a giant wrapper div
              if (el.children.length > 0) return false;
              const text = el.textContent ? el.textContent.trim().toLowerCase() : '';
              return text === 'expert' || text === 'deepthink' || text === 'deepthink (r1)';
            });
            
            if (expertText) {
              console.log('[KimBridge] Enabling Expert Mode (first turn)...');
              
              // Many React/Vue apps ignore raw .click() in favor of pointer events
              const rect = expertText.getBoundingClientRect();
              const evOpts = { 
                bubbles: true, cancelable: true, 
                clientX: rect.x + rect.width/2, 
                clientY: rect.y + rect.height/2 
              };
              
              expertText.dispatchEvent(new PointerEvent('pointerdown', evOpts));
              expertText.dispatchEvent(new MouseEvent('mousedown', evOpts));
              expertText.dispatchEvent(new PointerEvent('pointerup', evOpts));
              expertText.dispatchEvent(new MouseEvent('mouseup', evOpts));
              expertText.click();
              
              window.__kimBridge._expertToggled = true;
              // Brief pause to let UI react
              await new Promise(r => setTimeout(r, 300));
            }
          }
        } catch (e) {
          console.warn('[KimBridge] Error toggling Expert mode:', e);
        }
      }

      if (siteKey === 'gemini') {
        const rawTier = (window.__kimModelTier == null ? '' : String(window.__kimModelTier)).trim().toLowerCase();
        const tier = rawTier === 'advanced' ? 'pro' : (rawTier === 'fast' ? 'flash' : rawTier);
        const hasExplicitTier = tier === 'flash' || tier === 'pro' || tier === 'thinking';
        if (!hasExplicitTier) {
          console.log('[KimBridge] Gemini model auto-switch skipped (no explicit model tier)');
        } else {
        const selectGeminiPro = async () => {
            let modelBtn = null;
            const walker1 = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
            while (walker1.nextNode()) {
                const node = walker1.currentNode;
                const txt = (node.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase().trim();
                const words = txt.split(' ');
                
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
                const currentTxt = (modelBtn.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase();
                const isTargetFlash = tier === 'flash' || tier === 'fast';
                const isTargetPro = tier === 'pro' || tier === 'advanced';
                const isTargetThinking = tier === 'thinking';
                
                let needsSwitch = false;
                if (isTargetFlash && !currentTxt.includes('flash') && !currentTxt.includes('fast')) {
                    if (currentTxt !== 'gemini') needsSwitch = true;
                }
                if (isTargetPro && !currentTxt.includes('advanced') && !currentTxt.includes('pro')) needsSwitch = true;
                if (isTargetThinking && !currentTxt.includes('thinking')) needsSwitch = true;
                
                if (needsSwitch) {
                    console.log(`[KimBridge] Switching Gemini Model Tier to ${tier}`);
                    modelBtn.click();
                    await new Promise(r => setTimeout(r, 700));
                    
                    let targetClicked = false;
                    const targetTerms = isTargetThinking ? ['thinking'] : (isTargetFlash ? ['flash', 'fast', 'gemini'] : ['advanced', 'pro']);
                    
                    const overlays = document.querySelectorAll('.cdk-overlay-container, [role="menu"], [role="listbox"], [role="dialog"]');
                    const menuRoot = Array.from(overlays).find(o => isVisible(o)) || document.body;
                    
                    const walker2 = document.createTreeWalker(menuRoot, NodeFilter.SHOW_TEXT, null, false);
                    let targetNode = null;
                    while (walker2.nextNode()) {
                        const node = walker2.currentNode;
                        const txt = (node.textContent || '').replace(/[\\s\\u00A0]+/g, ' ').toLowerCase();
                        if (targetTerms.some(t => txt.includes(t)) && !txt.includes('upgrade') && !txt.includes('learn')) {
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
                        await new Promise(r => setTimeout(r, 1200));
                    }

                    if (!targetClicked) {
                        console.warn(`[KimBridge] Failed to find target Gemini model '${tier}' in menu`);
                        document.body.click();
                        await new Promise(r => setTimeout(r, 200));
                    }
                } else {
                    console.log(`[KimBridge] Gemini Model already correct (${tier})`);
                }
            } else {
                console.warn('[KimBridge] Gemini Model selector button not found');
            }
        };
        await selectGeminiPro();
        }
      }

      // 1. Find input (use shadow-DOM-aware finder for Gemini)
      const inputEl = (siteKey === 'gemini' ? findGeminiInput() : null) || findElement(cfg.input_selectors, { visible: true });
      if (!inputEl) {
        throw new Error(`Could not find input selector for ${siteKey}`);
      }
      inputEl.focus();

      const uploadedCount = await injectAttachments(cfg, inputEl, attachments);

      // 3. Inject text — verify with rAF loop instead of hardcoded sleep.
      // When attachments were expected but couldn't be injected (uploadedCount === 0),
      // append an explanatory note so the model can respond gracefully instead of
      // looping with repeated take_screenshot calls.
      const effectivePrompt = (attachments && attachments.length > 0 && uploadedCount === 0)
        ? prompt + '\n[System note: The screenshot could not be attached to this message (image upload unavailable in current interface). Please respond with what you can based on text context, or NEED_HELP if you truly cannot proceed without the image.]'
        : prompt;
      const injectedLen = await injectPromptText(inputEl, effectivePrompt);
      if (injectedLen < Math.min(8, normalizeText(effectivePrompt).length) || !promptMatchesInput(inputEl, effectivePrompt)) {
        console.warn('[KimBridge] Prompt text may not have been fully accepted by the chat input. Proceeding anyway.');
      }

      // Allow UI framework to sync state before sending
      await new Promise(r => setTimeout(r, 80));

      // 4a. Wait for the PREVIOUS request's completion hash to appear in the
      //     DOM before clicking Send.  This ensures we never interrupt an
      //     in-progress generation.  The hash is provider-agnostic — it lives
      //     in the response text itself, so it works regardless of UI changes.
      {
        const prevHash = window.__kimBridge._lastHash;
        if (prevHash) {
          const PRE_SEND_TIMEOUT = 120000; // up to 120s for long responses
          const preDeadline = Date.now() + PRE_SEND_TIMEOUT;
          let noStopCount = 0;
          while (Date.now() < preDeadline) {
            if (isSuperseded()) { console.log('[KimBridge] send superseded during pre-send wait, bailing'); ipcEmit({ ok: false, event: 'error', req_id: reqId, error: 'Request superseded by newer send', site: siteKey }); return; }
            const pageText = getLatestResponseText(cfg, siteKey) || '';
            if (pageText.includes(prevHash)) break;

            if (!isAnyStopVisible(cfg)) {
              noStopCount++;
              // If stop button is missing for ~1.5 seconds, assume done
              if (noStopCount >= 5) {
                console.log('[KimBridge] prevHash not found, but stop button is gone. Proceeding.');
                break;
              }
            } else {
              noStopCount = 0;
            }
            await new Promise(r => setTimeout(r, 300));
          }
          // Short settle delay removed for latency
        }
      }

      if (!promptMatchesInput(inputEl, effectivePrompt)) {
        throw new Error('Prompt changed after injection. Refusing to send a partial prompt.');
      }

      // 4b. Submit via send button (preferred) or synthetic Enter (fallback).
      inputEl.focus();
      let sendClicked = false;
      for (let attempt = 0; attempt < 15; attempt++) {
        const sendBtn = findElement(cfg.send_selectors || [], { visible: true, enabled: true });
        if (sendBtn) {
          sendBtn.click();
          sendClicked = true;
          break;
        }
        await new Promise(r => setTimeout(r, 100));
      }
      if (!sendClicked) {
        // Fallback: synthetic Enter
        inputEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        inputEl.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        inputEl.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      }
      console.log('[KimBridge] Message submitted via', sendClicked ? 'send button' : 'synthetic Enter');

      // 4c. Record THIS request's hash AFTER submit so the NEXT request can wait for it
      if (completionHash) {
        window.__kimBridge._lastHash = completionHash;
      }

      // 5. Emit "sent" immediately via IPC only — NOT via the legacy store,
      //    because the store is used by Rust's title-pull fallback which can't
      //    distinguish 'sent' from 'done' (BridgeCompleteResponse has no event
      //    field).  Writing 'sent' to the store would cause the collector to
      //    return response=null before the LLM has finished.
      ipcEmit({ ok: true, event: 'sent', req_id: reqId, site: siteKey });

      // 6. Poll DOM for the completion hash
      //    The LLM was instructed to append the hash at the very end of its
      //    response.  When we see it in the page text, the response is complete
      //    and we know exactly which text belongs to this request.
      const POLL_MS = 300;
      let responseText = '';
      let lastTextLength = 0;
      let idleCount = 0;
      let sawNewResponse = false;

      while (Date.now() < hardDeadlineAt) {
        await new Promise(r => setTimeout(r, POLL_MS));
        if (isSuperseded()) { console.log('[KimBridge] send superseded during response wait, bailing'); ipcEmit({ ok: false, event: 'error', req_id: reqId, error: 'Request superseded by newer send', site: siteKey }); return; }

        const latestText = getLatestResponseText(cfg, siteKey) || '';
        const latestCount = countResponseNodes(cfg, siteKey);
        if (
          latestCount > baselineResponseCount
          || (latestText && latestText !== baselineResponseText)
        ) {
          sawNewResponse = true;
        }
        reportProgress(latestText);
        if (completionHash && latestText.includes(completionHash)) {
          responseText = latestText.replace(completionHash, '').trim();
          break;
        }

        if (latestText.length > lastTextLength) {
          idleCount = 0;
          lastTextLength = latestText.length;
        } else if (latestText.length > 0) {
          idleCount++;
        }

        if (latestText && latestText.length > 0 && !isAnyStopVisible(cfg)) {
          if (sawNewResponse && idleCount >= 10) { // 3 seconds of no text growth
            console.log('[KimBridge] stop button gone and text idle. Falling back to latest text.');
            responseText = latestText;
            break;
          }
        }
      }

      if (!responseText) {
        // Last-ditch: try to grab whatever text is on the page
        const lastTry = getLatestResponseText(cfg, siteKey) || '';
        const lastTryCount = countResponseNodes(cfg, siteKey);
        const hasNewTurn =
          lastTryCount > baselineResponseCount
          || (lastTry && lastTry !== baselineResponseText);
        if (completionHash && lastTry.includes(completionHash)) {
          responseText = lastTry.replace(completionHash, '').trim();
        } else if (lastTry.length > 0 && hasNewTurn) {
          responseText = lastTry;
        } else {
          throw new Error(`Timed out waiting for completion hash in response (${HARD_TIMEOUT}ms); no new response turn detected`);
        }
      }

      // 7. Emit "done" with full response
      reportResult({ ok: true, event: 'done', req_id: reqId, response: responseText, site: siteKey, attachments_uploaded: uploadedCount || 0 });

    } catch (err) {
      const message = (err && err.message) ? err.message : String(err);
      reportResult({ ok: false, event: 'error', req_id: reqId, error: message, site: site || 'unknown' });
    }
  };

  // ── Detect which site we're on ────────────────────────────────────────
  const detectSite = () => {
    const href = window.location.href.toLowerCase();
    if (href.includes('gemini.google.com')) return 'gemini';
    if (href.includes('claude.ai')) return 'claude';
    if (href.includes('chatgpt.com')) return 'chatgpt';
    if (href.includes('chat.deepseek.com')) return 'deepseek';
    if (href.includes('grok.com')) return 'grok';
    return null;
  };

  // ── Check if page is ready (input element exists) ─────────────────────
  const checkReady = () => {
    const site = detectSite();
    if (!site) return false;
    const cfg = SITE_CONFIGS[site];
    if (!cfg) return false;
    if (site === 'gemini') return !!findGeminiInput();
    return !!findElement(cfg.input_selectors, { visible: true });
  };

  // ── Clear _lastHash when URL changes (new chat detected) ──────────────
  let _lastObservedUrl = window.location.href;
  setInterval(() => {
    const currentUrl = window.location.href;
    if (currentUrl !== _lastObservedUrl) {
      _lastObservedUrl = currentUrl;
      if (window.__kimBridge) {
        window.__kimBridge._lastHash = null;
        console.log('[KimBridge] URL changed, cleared _lastHash');
      }
    }
  }, 1000);

  // ── Public API ─────────────────────────────────────────────────────────
  window.__kimBridge = {
    _v: 10,
    _lastHash: null, // Tracks the completion hash of the most recent request
    _currentReqId: null, // Tracks the in-flight req_id; older send()s bail when this changes
    send,
    detectSite,
    checkReady,
    SITE_CONFIGS,
    // Legacy compat
    getLatestResponseText: (site) => {
      const siteKey = SITE_CONFIGS[site] ? site : 'claude';
      return getLatestResponseText(SITE_CONFIGS[siteKey], siteKey);
    },
  };

  console.log('[KimBridge] Persistent bridge v2 installed for', detectSite() || 'unknown site');
})();
"#;

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
            "collectorTimeoutS": BRIDGE_COMPLETION_TIMEOUT_S,
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
            "timeoutS": BRIDGE_COMPLETION_TIMEOUT_S,
            "mode": "sentinel_v1",
        }),
    );

    let result = collect_bridge_payload(
        window,
        &req_id,
        Duration::from_secs(BRIDGE_COMPLETION_TIMEOUT_S),
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
            let url = tauri::Url::parse(&format!("http://localhost{}", full_uri)).unwrap();
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

            // The window stays offscreen (1x1 at -10000,-10000) during
            // headless operation.  JS keeps running at 1x1, no need to
            // show it to the user.
            if should_keep_browser_visible() {
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
            let saved_frontmost = save_frontmost_app();

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
            let claw_dir = query_param(&raw_url, "claw_dir");
            let base = session_base_dir(&session_type, kim_dir, claw_dir);
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
                claw_dir: Option<String>,
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
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.claw_dir);
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
                claw_dir: Option<String>,
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
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.claw_dir);
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
                claw_dir: Option<String>,
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
            let base = session_base_dir(&stype, parsed.kim_dir, parsed.claw_dir);
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
                .filter(|s| !s.trim().is_empty())
                .unwrap_or_else(|| "browser".to_string());

            let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();

            let mut cmd = std::process::Command::new(&python);
            cmd.args(["-m", "orchestrator.agent"])
                .arg("--task")
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

    let result = collect_bridge_payload(
        &window,
        &req_id,
        Duration::from_secs(BRIDGE_COMPLETION_TIMEOUT_S),
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
// Claw file-bridge command watcher (Tasks 4/5/7 of the browser-parity work)
// ---------------------------------------------------------------------------

const CLAW_BRIDGE_DIR: &str = "/tmp/claw_bridge";

/// Spawn a background thread that:
///  - Polls `/tmp/claw_bridge/browser_cmd.json` every 500 ms and dispatches
///    show/hide/switch_site actions to the Kim webview window.
///  - Writes `/tmp/claw_bridge/bridge_status.json` every 5 s so Claw's
///    `/browser status` command can report the current bridge state.
fn start_bridge_file_watcher(app_handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        let bridge_dir = std::path::Path::new(CLAW_BRIDGE_DIR);
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
            std::thread::spawn(move || {
                std::thread::sleep(std::time::Duration::from_millis(3300));
                let _ = win_for_close.close();
            });
        }
        Err(e) => eprintln!("[Kim] screenshot flash window error: {e}"),
    }
}

#[tauri::command]
async fn show_screenshot_flash(app_handle: tauri::AppHandle) {
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

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => first.to_uppercase().collect::<String>() + chars.as_str(),
    }
}

fn start_webview_bridge_server(app_handle: tauri::AppHandle) -> Result<(), String> {
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

    // Write only base_url to kim_sessions/.bridge_url (no cleartext tokens on disk)
    let sessions_dir = default_project_root().join("kim_sessions");
    let _ = std::fs::create_dir_all(&sessions_dir);
    let _ = std::fs::write(sessions_dir.join(".bridge_url"), &base_url);
    // Best-effort remove legacy cleartext token
    let _ = std::fs::remove_file(sessions_dir.join(".bridge_token"));

    std::thread::spawn(move || {
        eprintln!(
            "[Kim] In-app browser bridge listening at {} (mode=sentinel_v1, timeout={}s)",
            base_url,
            BRIDGE_COMPLETION_TIMEOUT_S,
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

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[tauri::command]
async fn list_sessions(
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<Vec<SessionInfo>, String> {
    let kim_base = kim_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);

    let mut sessions = read_sessions_from_dir(&kim_base, "kim")?;

    if let Some(claw_path) = claw_dir {
        let claw_base = PathBuf::from(claw_path);
        let claw_sessions = read_sessions_from_dir(&claw_base, "claw")?;
        sessions.extend(claw_sessions);
    }

    Ok(sessions)
}

#[tauri::command]
async fn delete_sessions(
    session_ids: Vec<String>,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<(), String> {
    for session_id in session_ids {
        validate_session_id(&session_id)?;

        let mut deleted = false;

        let dirs_to_search: Vec<PathBuf> = {
            let mut v = vec![kim_dir
                .as_deref()
                .map(PathBuf::from)
                .unwrap_or_else(default_sessions_dir)];
            if let Some(claw_path) = &claw_dir {
                v.push(PathBuf::from(claw_path));
            }
            v
        };

        // Sessions are stored as <base>/<date_dir>/<session_id>.jsonl
        // We need to search all date subdirectories
        for base in &dirs_to_search {
            if !base.exists() {
                continue;
            }
            // Search date subdirectories (e.g., 2026-04-23/)
            if let Ok(entries) = std::fs::read_dir(base) {
                for entry in entries.filter_map(|e| e.ok()) {
                    let date_dir = entry.path();
                    if !date_dir.is_dir() {
                        continue;
                    }
                    // Look for <session_id>.jsonl
                    let jsonl_path = date_dir.join(format!("{}.jsonl", session_id));
                    if jsonl_path.exists() {
                        if let Err(e) = std::fs::remove_file(&jsonl_path) {
                            eprintln!("Failed to delete session file {}: {}", session_id, e);
                        } else {
                            deleted = true;
                            // Also delete companion summary file if it exists
                            let summary_path = date_dir.join(format!("{}.summary.txt", session_id));
                            if summary_path.exists() {
                                let _ = std::fs::remove_file(&summary_path);
                            }
                            let browser_meta_path = date_dir.join(browser_session_meta_filename(&session_id));
                            if browser_meta_path.exists() {
                                let _ = std::fs::remove_file(&browser_meta_path);
                            }
                        }
                    }
                }
            }
        }

        if !deleted {
            eprintln!("Session {} not found for deletion.", session_id);
        }
    }

    Ok(())
}

#[tauri::command]
async fn summarize_session(
    session_id: String,
    session_type: String,
    project_root: Option<String>,
) -> Result<(), String> {
    validate_session_id(&session_id)?;

    // Build the list of directories to search, same logic as load_session_messages
    let mut dirs_to_search: Vec<PathBuf> = vec![default_sessions_dir()];
    if session_type == "claw" {
        if let Some(ref pr) = project_root {
            let claw_dir = PathBuf::from(pr).join(".claw").join("sessions");
            if claw_dir.exists() {
                dirs_to_search.push(claw_dir);
            }
        }
    }

    // Find the JSONL file
    let mut jsonl_path: Option<PathBuf> = None;
    for base in &dirs_to_search {
        if !base.exists() { continue; }
        if let Ok(entries) = fs::read_dir(base) {
            let mut date_dirs: Vec<_> = entries
                .filter_map(|e| e.ok())
                .filter(|e| e.path().is_dir())
                .collect();
            date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));
            for entry in date_dirs {
                let candidate = entry.path().join(format!("{}.jsonl", session_id));
                if candidate.exists() {
                    jsonl_path = Some(candidate);
                    break;
                }
            }
        }
        if jsonl_path.is_some() { break; }
    }

    let path = jsonl_path.ok_or_else(|| format!("Session file not found: {}", session_id))?;

    // Read the JSONL and extract summary components
    let file = fs::File::open(&path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);

    let mut first_user_text: Option<String> = None;
    let mut last_assistant_text: Option<String> = None;
    let mut touched_files: Vec<String> = Vec::new();

    for line in reader.lines().map_while(Result::ok) {
        let trimmed = line.trim();
        if trimmed.is_empty() { continue; }
        let value: serde_json::Value = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => continue,
        };

        // Handle both Kim format (role at top level) and Claw format (type: "message")
        let (role, content) = if let Some(r) = value.get("role").and_then(|v| v.as_str()) {
            (r.to_string(), value.get("content").cloned())
        } else if value.get("type").and_then(|v| v.as_str()) == Some("message") {
            let msg = value.get("message").unwrap_or(&serde_json::Value::Null);
            let r = msg.get("role").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let c = msg.get("blocks").or_else(|| msg.get("content")).cloned();
            (r, c)
        } else {
            continue;
        };

        let text = match &content {
            Some(serde_json::Value::String(s)) => Some(s.clone()),
            Some(serde_json::Value::Array(items)) => {
                let texts: Vec<String> = items.iter()
                    .filter(|b| b.get("type").and_then(|v| v.as_str()) == Some("text"))
                    .filter_map(|b| b.get("text").and_then(|v| v.as_str()).map(|s| s.to_string()))
                    .collect();
                if texts.is_empty() { None } else { Some(texts.join("\n")) }
            }
            _ => None,
        };

        if role == "user" {
            if let Some(ref t) = text {
                let clean = t.trim();
                // Skip tool result pseudo-user messages
                if !clean.starts_with("[Tool result:") && !clean.is_empty() && first_user_text.is_none() {
                    first_user_text = Some(clean.strip_prefix("Task: ").unwrap_or(clean).to_string());
                }
            }
        } else if role == "assistant" {
            if let Some(ref t) = text {
                let clean = t.trim()
                    .trim_start_matches("TASK_COMPLETE:")
                    .trim();
                if !clean.is_empty() {
                    last_assistant_text = Some(clean.to_string());
                }
            }
            if let Some(serde_json::Value::Array(items)) = &content {
                for block in items {
                    if block.get("type").and_then(|v| v.as_str()) == Some("tool_use") {
                        let name = block.get("name").and_then(|v| v.as_str()).unwrap_or("");
                        if matches!(name, "write_file" | "edit_file" | "create_file") {
                            // In Claw JSONL, input is often a JSON string rather than an object.
                            let mut path_str = None;
                            if let Some(input_val) = block.get("input") {
                                if let Some(s) = input_val.as_str() {
                                    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(s) {
                                        path_str = parsed.get("path").or_else(|| parsed.get("file_path"))
                                            .and_then(|v| v.as_str())
                                            .map(|s| s.to_string());
                                    }
                                } else if let Some(obj) = input_val.as_object() {
                                    path_str = obj.get("path").or_else(|| obj.get("file_path"))
                                        .and_then(|v| v.as_str())
                                        .map(|s| s.to_string());
                                }
                            }
                            if let Some(p) = path_str {
                                let fname = p.rsplit('/').next().unwrap_or(&p);
                                if !touched_files.contains(&fname.to_string()) {
                                    touched_files.push(fname.to_string());
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // Build summary text
    let mut summary_parts: Vec<String> = Vec::new();
    if let Some(ref task) = first_user_text {
        let truncated = if task.chars().count() > 100 {
            format!("{}…", task.chars().take(100).collect::<String>())
        } else {
            task.clone()
        };
        summary_parts.push(format!("Task: {}", truncated));
    }
    if let Some(ref result) = last_assistant_text {
        let truncated = if result.chars().count() > 200 {
            format!("{}…", result.chars().take(200).collect::<String>())
        } else {
            result.clone()
        };
        summary_parts.push(format!("Result: {}", truncated));
    }
    if !touched_files.is_empty() {
        summary_parts.push(format!("Files: {}", touched_files.join(", ")));
    }

    let summary = if summary_parts.is_empty() {
        "No summary available.".to_string()
    } else {
        summary_parts.join("\n")
    };

    // Write summary file next to the JSONL: abc.jsonl → abc.summary.txt
    let summary_path = path.with_file_name(format!("{}.summary.txt", session_id));
    fs::write(&summary_path, &summary).map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
async fn load_session_messages(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<Vec<KimMessage>, String> {
    // Reject path-traversal attempts on the session_id.
    validate_session_id(&session_id)?;
    // Search kim dir first, then claw dir
    let dirs_to_search: Vec<PathBuf> = {
        let mut v = vec![kim_dir
            .map(PathBuf::from)
            .unwrap_or_else(default_sessions_dir)];
        if let Some(claw_path) = claw_dir {
            v.push(PathBuf::from(claw_path));
        }
        v
    };

    for base in &dirs_to_search {
        if !base.exists() {
            continue;
        }
        if let Some(date) = session_date.as_deref() {
            validate_session_id(date)?;
            let date_dir = base.join(date);
            let candidate = date_dir.join(format!("{}.jsonl", session_id));
            if candidate.exists() {
                let (canon_candidate, canon_dir) = match (
                    candidate.canonicalize(),
                    date_dir.canonicalize(),
                ) {
                    (Ok(c), Ok(d)) => (c, d),
                    _ => continue,
                };
                if !canon_candidate.starts_with(&canon_dir) {
                    return Err("Resolved session path escapes its date directory".to_string());
                }
                return parse_jsonl(&canon_candidate);
            }
            continue;
        }
        let mut date_dirs: Vec<_> = fs::read_dir(base)
            .map_err(|e| e.to_string())?
            .filter_map(|e| e.ok())
            .filter(|e| e.path().is_dir())
            .collect();
        date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for date_entry in date_dirs {
            let date_dir = date_entry.path();
            let candidate = date_dir.join(format!("{}.jsonl", session_id));
            if !candidate.exists() {
                continue;
            }
            // Defense in depth: canonicalize and assert the resolved path is
            // still inside its intended date directory.
            let (canon_candidate, canon_dir) = match (
                candidate.canonicalize(),
                date_dir.canonicalize(),
            ) {
                (Ok(c), Ok(d)) => (c, d),
                _ => continue,
            };
            if !canon_candidate.starts_with(&canon_dir) {
                return Err("Resolved session path escapes its date directory".to_string());
            }
            return parse_jsonl(&canon_candidate);
        }
    }

    Err(format!("Session not found: {}", session_id))
}

#[tauri::command]
async fn get_app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

// ---------------------------------------------------------------------------
// Worked-for run history persistence — stores the activity-feed snapshot for
// each completed task as a sidecar `<session_id>.runs.json` next to the
// session JSONL file. Survives chat reloads.
// ---------------------------------------------------------------------------

fn find_session_date_dir(
    session_id: &str,
    session_date: Option<&str>,
    kim_dir: Option<&str>,
    claw_dir: Option<&str>,
) -> Option<PathBuf> {
    let mut dirs: Vec<PathBuf> = vec![
        kim_dir.map(PathBuf::from).unwrap_or_else(default_sessions_dir),
    ];
    if let Some(p) = claw_dir { dirs.push(PathBuf::from(p)); }
    for base in &dirs {
        if !base.exists() { continue; }
        if let Some(date) = session_date {
            let dd = base.join(date);
            if dd.join(format!("{}.jsonl", session_id)).exists() {
                return Some(dd);
            }
        }
        if let Ok(entries) = fs::read_dir(base) {
            for entry in entries.filter_map(|e| e.ok()) {
                let dd = entry.path();
                if dd.is_dir() && dd.join(format!("{}.jsonl", session_id)).exists() {
                    return Some(dd);
                }
            }
        }
    }
    None
}

#[tauri::command]
async fn save_run_history(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
    runs: serde_json::Value,
) -> Result<(), String> {
    validate_session_id(&session_id)?;
    if let Some(d) = session_date.as_deref() { validate_session_id(d)?; }

    let dir = find_session_date_dir(&session_id, session_date.as_deref(), kim_dir.as_deref(), claw_dir.as_deref())
        .or_else(|| {
            // Fallback: write into today's directory under the kim sessions dir.
            let base = kim_dir.as_deref().map(PathBuf::from).unwrap_or_else(default_sessions_dir);
            let today = chrono_now().get(0..10).map(|s| s.to_string()).unwrap_or_default();
            let dd = base.join(today);
            fs::create_dir_all(&dd).ok()?;
            Some(dd)
        })
        .ok_or_else(|| "Could not locate a session directory to save runs.json into".to_string())?;

    let path = dir.join(format!("{}.runs.json", session_id));
    let body = serde_json::to_string(&runs).map_err(|e| e.to_string())?;
    fs::write(&path, body).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn load_run_history(
    session_id: String,
    session_date: Option<String>,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
) -> Result<serde_json::Value, String> {
    validate_session_id(&session_id)?;
    if let Some(d) = session_date.as_deref() { validate_session_id(d)?; }

    let dir = match find_session_date_dir(&session_id, session_date.as_deref(), kim_dir.as_deref(), claw_dir.as_deref()) {
        Some(d) => d,
        None => return Ok(serde_json::json!([])),
    };
    let path = dir.join(format!("{}.runs.json", session_id));
    if !path.exists() {
        return Ok(serde_json::json!([]));
    }
    let body = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    serde_json::from_str(&body).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_platform_info() -> String {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    match (os, arch) {
        ("macos",   "aarch64") => "macOS (Apple Silicon)".into(),
        ("macos",   _        ) => "macOS (Intel)".into(),
        ("windows", "x86_64" ) => "Windows x64".into(),
        ("windows", "x86"    ) => "Windows x86".into(),
        ("linux",   "aarch64") => "Linux ARM64".into(),
        ("linux",   _        ) => "Linux x64".into(),
        (os, arch)             => format!("{os} ({arch})"),
    }
}

/// Pull the latest source from git, refresh Python deps, then restart the app.
/// Progress lines are emitted as "kim-update-progress" events.
#[tauri::command]
async fn run_update(app_handle: tauri::AppHandle) -> Result<(), String> {
    let kim_root = default_project_root();

    // ── Step 1: git pull ──────────────────────────────────────────────────
    let _ = app_handle.emit("kim-update-progress", "Pulling latest source from GitHub…");

    // Try to find git
    let git_cmd = if cfg!(target_os = "windows") { "git.exe" } else { "git" };
    let git_out = std::process::Command::new(git_cmd)
        .args(["pull", "--ff-only"])
        .current_dir(&kim_root)
        .output()
        .map_err(|e| format!("git not found — make sure Git is installed: {e}"))?;

    if !git_out.status.success() {
        let stderr = String::from_utf8_lossy(&git_out.stderr);
        return Err(format!("git pull failed: {stderr}"));
    }
    let git_stdout = String::from_utf8_lossy(&git_out.stdout).trim().to_string();
    let already_latest = git_stdout.contains("Already up to date") || git_stdout.contains("Already up-to-date");
    let _ = app_handle.emit(
        "kim-update-progress",
        if git_stdout.is_empty() { "Source updated.".to_string() } else { git_stdout.clone() },
    );

    if already_latest {
        let _ = app_handle.emit("kim-update-progress", "Source is already up to date — no restart needed.");
        return Ok(());
    }

    // ── Step 2: pip install ───────────────────────────────────────────────
    let _ = app_handle.emit("kim-update-progress", "Updating Python dependencies…");

    let python = find_python_interpreter(&kim_root)
        .map_err(|e| format!("Python not found: {e}"))?;

    let pip_out = std::process::Command::new(&python)
        .args(["-m", "pip", "install", "-r", "requirements.txt", "-q", "--disable-pip-version-check"])
        .current_dir(&kim_root)
        .output();

    match pip_out {
        Ok(out) if out.status.success() => {
            let _ = app_handle.emit("kim-update-progress", "Python dependencies updated.");
        }
        Ok(out) => {
            let msg = String::from_utf8_lossy(&out.stderr).trim().to_string();
            let _ = app_handle.emit("kim-update-progress", format!("Warning (pip): {msg}"));
        }
        Err(e) => {
            let _ = app_handle.emit("kim-update-progress", format!("Warning: pip update skipped ({e})."));
        }
    }

    // ── Step 3: Restart ───────────────────────────────────────────────────
    let _ = app_handle.emit("kim-update-progress", "Update complete — restarting Kim…");
    tokio::time::sleep(Duration::from_millis(800)).await;

    // app_handle.restart() closes the process but doesn't reliably reopen the
    // .app bundle on macOS. Use `open -a Kim` then exit instead.
    #[cfg(target_os = "macos")]
    {
        // Try to reopen by app bundle name first, fall back to exe path.
        let reopened = std::process::Command::new("open")
            .args(["-a", "Kim"])
            .spawn()
            .is_ok();
        if !reopened {
            if let Ok(exe) = std::env::current_exe() {
                // Walk up to find the .app bundle and open it.
                let mut path = exe.as_path();
                loop {
                    if path.extension().is_some_and(|e| e == "app") {
                        let _ = std::process::Command::new("open").arg(path).spawn();
                        break;
                    }
                    match path.parent() {
                        Some(p) => path = p,
                        None => break,
                    }
                }
            }
        }
        std::process::exit(0);
    }
    #[cfg(not(target_os = "macos"))]
    {
        app_handle.restart();
    }
}

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

#[tauri::command]
async fn hide_main_window(app_handle: tauri::AppHandle) -> Result<(), String> {
    // Hide only the "main" window so the screenshot-flash overlay stays visible.
    // app_handle.hide() would hide every window including the flash overlay.
    if let Some(win) = app_handle.get_webview_window("main") {
        let _ = win.hide();
    }
    Ok(())
}

#[tauri::command]
async fn show_main_window(app_handle: tauri::AppHandle) -> Result<(), String> {
    if let Some(win) = app_handle.get_webview_window("main") {
        let _ = win.show();
        let _ = win.set_focus();
    }
    Ok(())
}

#[tauri::command]
async fn set_task_active_mode(app_handle: tauri::AppHandle, active: bool) -> Result<(), String> {
    let cancel_label = "cancel-widget";
    if active {
        // Hide main window
        if let Some(main_win) = app_handle.get_webview_window("main") {
            let _ = main_win.hide();
        }

        // Show or create cancel widget
        if let Some(cancel_win) = app_handle.get_webview_window(cancel_label) {
            let _ = cancel_win.show();
            let _ = cancel_win.set_focus();
        } else {
            let cancel_win = tauri::WebviewWindowBuilder::new(
                &app_handle,
                cancel_label,
                tauri::WebviewUrl::App("/?window=cancel".into()),
            )
            .title("Cancel Task")
            .inner_size(180.0, 50.0)
            .resizable(false)
            .always_on_top(true)
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .build()
            .map_err(|e| format!("Failed to build cancel widget: {}", e))?;

            // Position at bottom center
            if let Ok(Some(monitor)) = cancel_win.current_monitor() {
                let size = monitor.size();
                let scale_factor = monitor.scale_factor();
                let width = 180.0 * scale_factor;
                let height = 50.0 * scale_factor;
                let x = (size.width as f64 - width) / 2.0;
                let y = size.height as f64 - height - (80.0 * scale_factor); // 80px from bottom
                let _ = cancel_win.set_position(tauri::Position::Physical(tauri::PhysicalPosition::new(x as i32, y as i32)));
            }
        }
    } else {
        // Destroy (not just hide) the cancel widget so a stale instance can
        // never end up stacked under a new one on the next run. (issue #3 §5)
        if let Some(cancel_win) = app_handle.get_webview_window(cancel_label) {
            let _ = cancel_win.close();
        }

        // Show main window
        if let Some(main_win) = app_handle.get_webview_window("main") {
            let _ = main_win.show();
            let _ = main_win.set_focus();
        }
    }
    Ok(())
}

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
    claw_dir: Option<String>,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, claw_dir);
    let date_dir = resolve_session_date_dir(&base, &session_id, session_date.as_deref())?;
    Ok(read_browser_session_meta_from_dir(&date_dir, &session_id).unwrap_or_default())
}

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
    claw_dir: Option<String>,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, claw_dir);
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
    claw_dir: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<BrowserSessionMeta, String> {
    validate_session_id(&session_id)?;
    let Some(win) = app_handle.get_webview_window("kim-browser-signin") else {
        return session_browser_meta_read(session_id, session_date, session_type, kim_dir, claw_dir).await;
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
        let base = session_base_dir(&stype, kim_dir, claw_dir);
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
        claw_dir,
    ).await
}

#[tauri::command]
async fn restore_browser_for_session(
    session_id: String,
    session_date: Option<String>,
    session_type: Option<String>,
    preferred_site: Option<String>,
    kim_dir: Option<String>,
    claw_dir: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<BrowserRestoreResult, String> {
    validate_session_id(&session_id)?;
    if is_bridge_task_running() {
        return Err("Cannot restore provider browser while Kim is running a task.".to_string());
    }

    let stype = session_type.unwrap_or_else(|| "kim".to_string());
    let base = session_base_dir(&stype, kim_dir, claw_dir);
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
    } else if meta.browser_threads.get(&site).is_some() {
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
/// Real env vars take priority over file values; `claw` and `python` already
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

async fn selected_ollama_claw_model(
    mode: Option<&str>,
    base_url: Option<&str>,
    local_model: Option<&str>,
    cloud_model: Option<&str>,
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
        return Err("Pick or pull an Ollama local model before running Claw with Ollama Local.".to_string());
    }
    Ok(cloud_model
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("gpt-oss:120b-cloud")
        .to_string())
}

async fn configure_claw_direct_provider(
    cmd: &mut tokio::process::Command,
    provider_arg: &str,
    kim_root: &Path,
    ollama_base_url: Option<&str>,
    ollama_mode: Option<&str>,
    ollama_local_model: Option<&str>,
    ollama_cloud_model: Option<&str>,
) -> Result<String, String> {
    let provider = provider_arg.trim().to_ascii_lowercase();
    match provider.as_str() {
        "ollama" => {
            let model = selected_ollama_claw_model(
                ollama_mode,
                ollama_base_url,
                ollama_local_model,
                ollama_cloud_model,
            ).await?;
            cmd.arg("--model").arg(&model)
                .env("OPENAI_BASE_URL", ollama_openai_base_url(ollama_base_url))
                // Required by OpenAI-compatible clients; ignored by Ollama.
                .env("OPENAI_API_KEY", "ollama");
            Ok(format!("Ollama via local daemon ({model})"))
        }
        "openai" => {
            let key = read_env_file_var(kim_root, "OPENAI_API_KEY")
                .ok_or_else(|| "Claw with OpenAI needs OPENAI_API_KEY in the environment or Kim's .env.".to_string())?;
            let model = read_first_env_file_var(kim_root, &["CLAW_OPENAI_MODEL", "OPENAI_MODEL"])
                .unwrap_or_else(|| "openai/gpt-4o".to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key);
            if let Some(base) = read_env_file_var(kim_root, "OPENAI_BASE_URL") {
                cmd.env("OPENAI_BASE_URL", base);
            }
            Ok(format!("OpenAI-compatible API ({model})"))
        }
        "deepseek" => {
            let key = read_env_file_var(kim_root, "DEEPSEEK_API_KEY")
                .ok_or_else(|| "Claw with DeepSeek needs DEEPSEEK_API_KEY in the environment or Kim's .env.".to_string())?;
            let model = read_first_env_file_var(kim_root, &["CLAW_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"])
                .unwrap_or_else(|| "deepseek-chat".to_string());
            let base = read_env_file_var(kim_root, "DEEPSEEK_BASE_URL")
                .unwrap_or_else(|| "https://api.deepseek.com/v1".to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key)
                .env("OPENAI_BASE_URL", base);
            Ok(format!("DeepSeek API ({model})"))
        }
        "gemini" => {
            let key = read_env_file_var(kim_root, "GOOGLE_API_KEY")
                .ok_or_else(|| "Claw with Gemini direct API needs GOOGLE_API_KEY in the environment or Kim's .env. Kim's Google OAuth token is only wired into the Chat provider path.".to_string())?;
            let model = read_first_env_file_var(kim_root, &["CLAW_GEMINI_MODEL", "GEMINI_MODEL"])
                .unwrap_or_else(|| "gemini-2.0-flash".to_string());
            let base = read_env_file_var(kim_root, "GEMINI_OPENAI_BASE_URL")
                .unwrap_or_else(|| "https://generativelanguage.googleapis.com/v1beta/openai".to_string());
            cmd.arg("--model").arg(&model)
                .env("OPENAI_API_KEY", key)
                .env("OPENAI_BASE_URL", base);
            Ok(format!("Gemini OpenAI-compatible API ({model})"))
        }
        _ => {
            let key = read_first_env_file_var(kim_root, &["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
                .ok_or_else(|| "Claw needs an Anthropic API key for Claude direct mode. Add ANTHROPIC_API_KEY to Kim's .env, or switch the provider dropdown to Ollama/Browser.".to_string())?;
            cmd.env("ANTHROPIC_API_KEY", key);
            for key in ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAW_MODEL", "CLAUDE_MODEL", "ANTHROPIC_MODEL"] {
                if let Some(value) = read_env_file_var(kim_root, key) {
                    cmd.env(key, value);
                }
            }
            Ok("Claude direct API".to_string())
        }
    }
}

/// Locate the `claw` binary. Search order:
/// 1. CLAW_BIN env var (explicit override)
/// 2. <kim_root>/pythonExperimentTool/... (nested layout — pythonExperimentTool inside kim-pro)
/// 3. <kim_root.parent()>/pythonExperimentTool/... (sibling layout — classic fork structure)
/// 4. `claw` on PATH
fn find_claw_binary(kim_root: &Path) -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CLAW_BIN") {
        let path = PathBuf::from(p);
        if path.is_file() {
            return Some(path);
        }
    }
    // Search both the kim_root itself and its parent so the binary is found
    // regardless of whether pythonExperimentTool is nested inside kim-pro
    // (e.g. kim-pro/pythonExperimentTool/...) or lives alongside it as a
    // sibling (e.g. <fork>/kim-pro + <fork>/pythonExperimentTool/...).
    let mut roots: Vec<PathBuf> = vec![kim_root.to_path_buf()];
    if let Some(parent) = kim_root.parent() {
        roots.push(parent.to_path_buf());
    }
    for root in &roots {
        for sub in &["release", "debug"] {
            let p = root.join(format!("pythonExperimentTool/claw-code/rust/target/{}/claw", sub));
            if p.is_file() {
                return Some(p);
            }
        }
    }
    // Fall back to PATH
    if let Ok(out) = std::process::Command::new("which").arg("claw").output() {
        if out.status.success() {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !s.is_empty() {
                return Some(PathBuf::from(s));
            }
        }
    }
    None
}

#[tauri::command]
async fn send_task(
    task: String,
    provider: Option<String>,
    project_root: Option<String>,
    resume_session_id: Option<String>,
    google_authuser: Option<u32>,
    // Forwarded as KIM_CONTEXT_BUDGET_TOKENS for orchestrator context meter.
    context_budget_tokens: Option<u32>,
    ollama_base_url: Option<String>,
    ollama_mode: Option<String>,
    ollama_local_model: Option<String>,
    ollama_cloud_model: Option<String>,
    ollama_context_limit_override: Option<u32>,
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    use std::process::Stdio;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    // Refuse to start a second task if one is already running or spawning.
    {
        let guard = state.lock().await;
        if guard.pid.is_some() || guard.starting {
            return Err("A task is already running. Stop it before starting a new one.".to_string());
        }
    }

    // The Kim repo root (where orchestrator/, mcp_server/, and venv/ live).
    // This is ALWAYS used for finding the Python interpreter, setting PYTHONPATH,
    // and as the working directory so `python -m orchestrator.agent` resolves.
    let kim_root = default_project_root();

    // The target project the user wants Kim/Claw to work on.  For the normal
    // Chat tab this is typically the Kim repo itself.  For the Code tab it is
    // the user's external code project.  MCP tools use PROJECT_ROOT to know
    // which directory to operate in (file reads/writes, git, search, etc.).
    let target_root = project_root
        .map(PathBuf::from)
        .filter(|p| p.exists())
        .unwrap_or_else(|| kim_root.clone());

    // Claw mode is ONLY when the target project is a real external project,
    // i.e. distinct from the Kim repo itself. The Chat tab passes the Kim
    // repo as project_root (so MCP tools know the workspace), but those
    // sessions must still land in kim_sessions/, not .claw/sessions/.
    let is_claw = target_root.canonicalize().ok()
        != kim_root.canonicalize().ok();

    let session_dir = if is_claw {
        target_root.join(".claw").join("sessions")
    } else {
        kim_root.join("kim_sessions")
    };

    // Default to Ollama (no API key in Kim) when the caller omits a provider.
    // The frontend normally sends an explicit provider, but keeping this
    // fallback aligned with Settings avoids surprise Browser runs.
    let provider_arg = provider
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "ollama".to_string());
    // Whether the user picked a browser-backed provider in the composer (e.g.
    // "browser:gemini"). For Code-tab tasks this routes Claw through Kim's
    // BrowserProvider via the file-bridge instead of calling Anthropic.
    let is_browser_provider = provider_arg == "browser" || provider_arg.starts_with("browser:");

    // Code (Claw) sessions run the `claw` binary directly against the user's
    // project, with Claw's own coding-agent system prompt. Chat (Kim) sessions
    // continue to run `python -m orchestrator.agent` with the desktop-control
    // system prompt. Routing them through different binaries is what keeps the
    // two personas separate end-to-end.
    let mut cmd = if is_claw {
        let claw_bin = find_claw_binary(&kim_root)
            .ok_or_else(|| "Claw binary not found. Build it with `cargo build --release` in pythonExperimentTool/claw-code/rust, or set CLAW_BIN to the binary path.".to_string())?;

        if is_browser_provider {
            // ── Browser-bridge mode ──────────────────────────────────────
            // Spawn `python -m orchestrator.run_claw_bridge`, which spawns
            // Claw with CLAW_FILE_BRIDGE=1 and relays each LLM request
            // through Kim's BrowserProvider. No Anthropic key required.
            let python = find_python_interpreter(&kim_root)?;
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] Routing to Claw via Kim's browser provider ({})", provider_arg),
            );
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] claw binary: {}", claw_bin.display()),
            );
            let mut c = Command::new(&python);
            c.args(["-m", "orchestrator.run_claw_bridge"])
                .arg("--task").arg(&task)
                .arg("--cwd").arg(target_root.to_string_lossy().to_string())
                .arg("--provider").arg(&provider_arg)
                .current_dir(&kim_root)
                .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
                // Tell run_claw_subtask exactly which claw binary to spawn,
                // so it doesn't have to repeat the search dance.
                .env("CLAW_BIN", claw_bin.to_string_lossy().to_string())
                // Forward webview-bridge creds so BrowserProvider can drive
                // the in-app sign-in window in headless mode if configured.
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            c
        } else {
            // ── Direct API mode ──────────────────────────────────────────
            let mut c = Command::new(&claw_bin);
            c.arg("--output-format").arg("json")
                .current_dir(&target_root)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let route = configure_claw_direct_provider(
                &mut c,
                &provider_arg,
                &kim_root,
                ollama_base_url.as_deref(),
                ollama_mode.as_deref(),
                ollama_local_model.as_deref(),
                ollama_cloud_model.as_deref(),
            ).await?;
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] Routing unchanged task to Claw via {}: {}", route, claw_bin.display()),
            );
            c.arg("prompt").arg(&task);
            c
        }
    } else {
        let python = find_python_interpreter(&kim_root)?;
        let mut c = Command::new(&python);
        c.args(["-m", "orchestrator.agent"])
            .arg("--task")
            .arg(&task)
            .arg("--session-dir")
            .arg(session_dir.to_string_lossy().to_string())
            .current_dir(&kim_root)
            // Tell the MCP server which directory to operate on (file tools, git, etc.).
            // For the Chat tab this is the Kim repo itself.
            .env("PROJECT_ROOT", target_root.to_str().unwrap_or(""))
            // Ensure `import orchestrator` and `import mcp_server` always resolve
            // from the Kim repo, regardless of the target project.
            .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        c
    };

    let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();
    // The webview-bridge creds let BrowserProvider drive the in-app sign-in
    // window. Forward them whenever a browser provider is in play, including
    // Claw-via-browser-bridge runs, so headless flows stay consistent.
    if !is_claw || is_browser_provider {
        if let Some(cfg) = &bridge_cfg {
            cmd.env("KIM_WEBVIEW_BRIDGE_URL", &cfg.base_url)
                .env("KIM_WEBVIEW_BRIDGE_TOKEN", &cfg.token)
                .env("KIM_WEBVIEW_WINDOW_LABEL", "kim-browser-signin");
        }
    }

    if is_browser_provider {
        let restore_status = browser_restore_status_for_session(
            &session_dir,
            resume_session_id.as_deref(),
            &provider_arg,
        );
        cmd.env("KIM_BROWSER_RESTORE_STATUS", restore_status);
    }

    if !is_claw && provider_arg.trim().eq_ignore_ascii_case("gemini") {
        let google_env = google_oauth::google_oauth_env_for_agent().await.map_err(|err| {
            format!(
                "Google for Kim is not connected. Open Settings → Account → Google for Kim (API), then Continue with Google. {}",
                err
            )
        })?;
        for (key, value) in google_env.as_env_pairs() {
            cmd.env(key, value);
        }
    }

    // Chrome CDP launch is needed whenever a browser provider is in play —
    // both for Kim's Chat-tab tasks and for Claw running in browser-bridge
    // mode (which calls into BrowserProvider via run_claw_bridge).
    if is_browser_provider {
        if bridge_cfg.is_none() {
            let root_for_chrome = kim_root.clone();
            match tokio::task::spawn_blocking(move || launch_chrome_for_cdp(&root_for_chrome)).await {
                Ok(Ok(true)) => {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                Ok(Ok(false)) => {}
                Ok(Err(e)) => {
                    eprintln!("[Kim] Chrome launch skipped: {}", e);
                }
                Err(e) => {
                    eprintln!("[Kim] Chrome launch task panicked: {}", e);
                }
            }
        } else {
            eprintln!("[Kim] Browser provider using in-app bridge (no Chrome CDP launch)");
        }
    }

    let resume_arg = if is_claw {
        None
    } else {
        resume_session_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
    };
    if let Some(resume_id) = resume_arg {
        cmd.arg("--resume").arg(resume_id);
    }

    if !is_claw {
        cmd.arg("--provider").arg(&provider_arg);
    }

    if !is_claw && provider_arg.trim().eq_ignore_ascii_case("ollama") {
        if let Some(base_url) = ollama_base_url.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_BASE_URL", base_url);
        }
        if let Some(mode) = ollama_mode.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_MODE", mode);
        }
        if let Some(model) = ollama_local_model.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_LOCAL_MODEL", model);
        }
        if let Some(model) = ollama_cloud_model.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_CLOUD_MODEL", model);
        }
        if let Some(limit) = ollama_context_limit_override.filter(|n| *n > 0) {
            cmd.env("KIM_OLLAMA_CONTEXT_LIMIT_OVERRIDE", limit.to_string());
        }
    }

    if let Some(idx) = google_authuser {
        cmd.env("KIM_GEMINI_AUTHUSER", idx.to_string());
    }

    if !is_claw {
        if let Some(budget) = context_budget_tokens.filter(|b| *b > 0) {
            cmd.env("KIM_CONTEXT_BUDGET_TOKENS", budget.to_string());
        }
    }

    #[cfg(unix)]
    {
        cmd.process_group(0);
    }

    // Reserve the runner slot immediately before spawning. Command setup can be
    // slow, and two frontend invocations can otherwise both pass the initial
    // pid check and spawn separate agents.
    {
        let mut guard = state.lock().await;
        if guard.pid.is_some() || guard.starting {
            return Err("A task is already running. Stop it before starting a new one.".to_string());
        }
        guard.starting = true;
    }

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => {
            let mut guard = state.lock().await;
            guard.starting = false;
            return Err(format!("Failed to start Kim: {}", e));
        }
    };

    // Record the PID so cancel_task can signal it.
    let child_pid = child.id();
    {
        let mut guard = state.lock().await;
        guard.pid = child_pid;
        guard.starting = false;
    }
    // Also sync to the bridge-accessible static so kimctl cancel works.
    if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = child_pid;
    }

    let stdout_handle = if let Some(stdout) = child.stdout.take() {
        let reader = tokio::io::BufReader::new(stdout);
        let app = app_handle.clone();
        Some(tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-output", line);
            }
        }))
    } else {
        None
    };

    let stderr_handle = if let Some(stderr) = child.stderr.take() {
        let reader = tokio::io::BufReader::new(stderr);
        let app = app_handle.clone();
        Some(tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-error", line);
            }
        }))
    } else {
        None
    };

    let status = child.wait().await.map_err(|e| e.to_string())?;

    if let Some(handle) = stdout_handle {
        let _ = handle.await;
    }
    if let Some(handle) = stderr_handle {
        let _ = handle.await;
    }

    // Clear the recorded PID regardless of exit reason (normal, error, cancelled).
    {
        let mut guard = state.lock().await;
        guard.pid = None;
        guard.starting = false;
    }
    // Clear bridge-accessible PID too.
    if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = None;
    }
    if let Ok(mut guard) = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = None;
    }

    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    if is_claw {
        if let Some(session) = newest_claw_session(&target_root) {
            let _ = app_handle.emit("kim-agent-code-session", session);
        }
    }

    let _ = app_handle.emit("kim-agent-done", status.success());

    // Always return Ok — the frontend learns about failure via the kim-agent-done
    // event (payload = false). Returning Err here causes a second error path in the
    // JS catch block, leading to duplicate error messages and UI state conflicts.
    Ok(if status.success() { "Task completed".to_string() } else { "Task ended".to_string() })
}

// ---------------------------------------------------------------------------
// Cancel a running task — SIGTERM, then SIGKILL after 2s if still alive.
// ---------------------------------------------------------------------------

/// Clear the task PID from BOTH tracking stores so subsequent cancel/status
/// calls don't see a stale PID regardless of how the task was started.
async fn clear_task_pid(state: &TaskState) {
    let mut guard = state.lock().await;
    guard.pid = None;
    guard.starting = false;
    if let Ok(mut bridge_guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *bridge_guard = None;
    }
}

#[tauri::command]
async fn cancel_task(
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    let (pid, starting) = {
        let guard = state.lock().await;
        (guard.pid, guard.starting)
    };

    // Fall back to the bridge-registered PID. Tasks started via kimctl /v1/task
    // register in BRIDGE_TASK_PID, not TaskState — so the GUI Stop button needs
    // to check both stores or it silently no-ops while the task keeps running.
    let pid = pid.or_else(|| {
        BRIDGE_TASK_PID
            .get_or_init(|| StdMutex::new(None))
            .lock()
            .ok()
            .and_then(|g| *g)
    });

    let Some(pid) = pid else {
        if starting {
            return Err("Task is still starting. Try stopping again in a moment.".to_string());
        }
        return Err("No task is currently running.".to_string());
    };

    // Step 1 — graceful signal.
    send_signal(pid, false)
        .map_err(|e| format!("Failed to send stop signal: {}", e))?;

    // Restore the UI immediately. If the agent is cancelled while a screenshot
    // tool is blocking, Python may never reach its finally block that normally
    // shows the main window again.
    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    // Step 2 — wait up to 2 seconds for the process to exit; if it's still
    // alive, force kill. We poll by re-sending signal 0 (existence check).
    let app = app_handle.clone();
    // Clone the inner Arc so the spawned task has its own handle —
    // dereference State<'_, TaskState> to TaskState (= Arc<Mutex<…>>),
    // then Arc::clone just bumps the refcount.
    let state_clone: TaskState = Arc::clone(&*state);
    tokio::spawn(async move {
        for _ in 0..20 {
            tokio::time::sleep(Duration::from_millis(100)).await;
            if !process_exists(pid) {
                clear_task_pid(&state_clone).await;
                let _ = app.emit("kim-agent-cancelled", true);
                return;
            }
        }
        // Still alive after 2s → SIGKILL / taskkill /F.
        let _ = send_signal(pid, true);
        clear_task_pid(&state_clone).await;
        let _ = app.emit("kim-agent-cancelled", true);
    });

    Ok("Cancelling task…".to_string())
}

// ── Platform-specific signalling ─────────────────────────────────────────────

#[cfg(unix)]
fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    let sig = if force { "-KILL" } else { "-TERM" };
    // Kill the process group (-pid) so child processes are also terminated (#64).
    // If group kill fails (e.g. the process is not a group leader), fall back
    // to killing only the parent process.
    let neg_pid = format!("-{}", pid);
    let status = Command::new("kill")
        .args([sig, &neg_pid])
        .status();
    match status {
        Ok(s) if s.success() => Ok(()),
        _ => {
            // Fallback: kill just the parent process
            let status = Command::new("kill")
                .args([sig, &pid.to_string()])
                .status()?;
            if !status.success() {
                return Err(std::io::Error::other(
                    format!("kill {} {} failed with {}", sig, pid, status),
                ));
            }
            Ok(())
        }
    }
}

#[cfg(unix)]
fn process_exists(pid: u32) -> bool {
    use std::process::Command;
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(windows)]
fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    // Windows has no SIGTERM; use taskkill. /T kills the process tree so
    // the Python interpreter and any child processes it spawned go too.
    let mut cmd = Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T"]);
    if force {
        cmd.arg("/F");
    }
    let status = cmd.status()?;
    if !status.success() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("taskkill failed with {}", status),
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn process_exists(pid: u32) -> bool {
    use std::process::Command;
    // `tasklist /FI "PID eq N"` prints a header only when there's no match.
    match Command::new("tasklist")
        .args(["/FI", &format!("PID eq {}", pid), "/NH"])
        .output()
    {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout);
            s.contains(&pid.to_string())
        }
        Err(_) => false,
    }
}

// ---------------------------------------------------------------------------
// Voice config (config.yaml — voice:/enabled, voice:/engine, voice:/voice_id)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct VoiceConfig {
    pub enabled: bool,
    pub engine: String,    // "kokoro" | "maya1" | "http" | "hume"
    pub voice_id: String,  // kokoro voice_id OR hume voice_name OR http voice
}

fn config_yaml_path(project_root: Option<String>) -> PathBuf {
    project_root
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root)
        .join("config.yaml")
}

/// Extract a scalar value from a single line inside a top-level `voice:` block.
/// We use this instead of a full YAML parser to keep dependencies minimal and
/// to preserve the user's existing comments/ordering on write.
fn extract_voice_scalar<'a>(yaml: &'a str, key: &str) -> Option<&'a str> {
    let mut in_voice = false;
    for line in yaml.lines() {
        // A new top-level key ends the voice block.
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            in_voice = line.trim_end() == "voice:";
            continue;
        }
        if in_voice {
            // Accept direct children indented with 1–4 spaces or a tab.
            let trimmed = line.trim_start_matches([' ', '\t']);
            let indent = line.len() - trimmed.len();
            if (1..=4).contains(&indent) {
                if let Some(rest) = trimmed.strip_prefix(&format!("{}:", key)) {
                    let v = rest.trim().trim_matches(|c| c == '"' || c == '\'');
                    return Some(v);
                }
            }
        }
    }
    None
}

#[tauri::command]
async fn read_voice_config(project_root: Option<String>) -> Result<VoiceConfig, String> {
    let path = config_yaml_path(project_root);
    if !path.exists() {
        return Ok(VoiceConfig {
            enabled: true,
            engine: "kokoro".to_string(),
            voice_id: "af_heart".to_string(),
        });
    }
    let yaml = fs::read_to_string(&path).map_err(|e| e.to_string())?;

    let enabled = extract_voice_scalar(&yaml, "enabled")
        .map(|v| v.eq_ignore_ascii_case("true"))
        .unwrap_or(true);
    let engine = extract_voice_scalar(&yaml, "engine")
        .unwrap_or("kokoro")
        .to_string();
    let voice_id = extract_voice_scalar(&yaml, "voice_id")
        .unwrap_or("af_heart")
        .to_string();

    Ok(VoiceConfig { enabled, engine, voice_id })
}

/// Replace one `  key: value` line inside the `voice:` block, or insert it
/// if missing. Preserves all other lines (comments, formatting, order).
fn upsert_voice_scalar(yaml: &str, key: &str, value: &str) -> String {
    let mut out: Vec<String> = Vec::with_capacity(yaml.lines().count() + 1);
    let mut in_voice = false;
    let mut voice_block_start: Option<usize> = None;
    let mut voice_block_end: Option<usize> = None;
    let mut replaced = false;

    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            if in_voice {
                voice_block_end = Some(out.len());
            }
            in_voice = line.trim_end() == "voice:";
            if in_voice {
                voice_block_start = Some(out.len());
            }
            out.push(line.to_string());
            continue;
        }

        if in_voice {
            let trimmed = line.trim_start();
            let indent = line.len() - trimmed.len();
            if indent == 2 && trimmed.starts_with(&format!("{}:", key)) {
                out.push(format!("  {}: {}", key, value));
                replaced = true;
                continue;
            }
        }
        out.push(line.to_string());
    }
    if in_voice {
        voice_block_end = Some(out.len());
    }

    if !replaced {
        match (voice_block_start, voice_block_end) {
            (Some(_), Some(end)) => {
                out.insert(end, format!("  {}: {}", key, value));
            }
            _ => {
                // No voice: block at all — append one.
                out.push("voice:".to_string());
                out.push(format!("  {}: {}", key, value));
            }
        }
    }

    let mut s = out.join("\n");
    if yaml.ends_with('\n') && !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

#[tauri::command]
async fn write_voice_config(
    config: VoiceConfig,
    project_root: Option<String>,
) -> Result<(), String> {
    let path = config_yaml_path(project_root);

    let original = if path.exists() {
        fs::read_to_string(&path).map_err(|e| e.to_string())?
    } else {
        String::from("voice:\n")
    };

    let mut updated = upsert_voice_scalar(&original, "enabled", if config.enabled { "true" } else { "false" });
    updated = upsert_voice_scalar(&updated, "engine", &config.engine);
    updated = upsert_voice_scalar(&updated, "voice_id", &config.voice_id);

    fs::write(&path, updated).map_err(|e| e.to_string())?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Phone relay (config.yaml `relay:` block + RELAY_PC_API_KEY env var)
// ---------------------------------------------------------------------------
//
// The PC reads `relay.url` from config.yaml so the user can point Kim at a
// different relay (self-hosted, staging, etc.) without rebuilding. The PC
// authenticates to the relay with `RELAY_PC_API_KEY` — which we keep in the
// .env file rather than the YAML so it doesn't end up in screenshots.

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct RelayConfig {
    /// e.g. "https://kim-relay.fly.dev". Empty string = phone-relay disabled.
    pub url: String,
    /// True when RELAY_PC_API_KEY is present in the environment. We never
    /// expose the key itself to the frontend — only whether it's set, so the
    /// UI can prompt the user before they try to pair.
    pub pc_key_configured: bool,
}

/// Same shape as `extract_voice_scalar` but parameterised over the top-level
/// block name. Used for any flat `block: { key: value, ... }` shape.
fn extract_block_scalar<'a>(yaml: &'a str, block: &str, key: &str) -> Option<&'a str> {
    let block_marker = format!("{}:", block);
    let key_prefix = format!("{}:", key);
    let mut in_block = false;
    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            in_block = line.trim_end() == block_marker;
            continue;
        }
        if in_block {
            let trimmed = line.trim_start_matches([' ', '\t']);
            let indent = line.len() - trimmed.len();
            if (1..=4).contains(&indent) {
                if let Some(rest) = trimmed.strip_prefix(&key_prefix) {
                    let v = rest.trim().trim_matches(|c| c == '"' || c == '\'');
                    return Some(v);
                }
            }
        }
    }
    None
}

/// Like `upsert_voice_scalar`, but generic over the block name.
fn upsert_block_scalar(yaml: &str, block: &str, key: &str, value: &str) -> String {
    let block_marker = format!("{}:", block);
    let key_prefix = format!("{}:", key);
    let mut out: Vec<String> = Vec::with_capacity(yaml.lines().count() + 1);
    let mut in_block = false;
    let mut block_start: Option<usize> = None;
    let mut block_end: Option<usize> = None;
    let mut replaced = false;

    for line in yaml.lines() {
        if !line.starts_with(char::is_whitespace) && line.trim_end().ends_with(':') {
            if in_block {
                block_end = Some(out.len());
            }
            in_block = line.trim_end() == block_marker;
            if in_block {
                block_start = Some(out.len());
            }
            out.push(line.to_string());
            continue;
        }

        if in_block {
            let trimmed = line.trim_start();
            let indent = line.len() - trimmed.len();
            if indent == 2 && trimmed.starts_with(&key_prefix) {
                out.push(format!("  {}: {}", key, value));
                replaced = true;
                continue;
            }
        }
        out.push(line.to_string());
    }
    if in_block {
        block_end = Some(out.len());
    }

    if !replaced {
        match (block_start, block_end) {
            (Some(_), Some(end)) => {
                out.insert(end, format!("  {}: {}", key, value));
            }
            _ => {
                out.push(format!("{}:", block));
                out.push(format!("  {}: {}", key, value));
            }
        }
    }

    let mut s = out.join("\n");
    if yaml.ends_with('\n') && !s.ends_with('\n') {
        s.push('\n');
    }
    s
}

fn read_pc_api_key(project_root: Option<String>) -> String {
    // Env wins so deployments can override without editing files.
    if let Ok(v) = std::env::var("RELAY_PC_API_KEY") {
        if !v.is_empty() {
            return v;
        }
    }
    // Fall back to .env in the project root.
    let env_path = project_root
        .as_deref()
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root)
        .join(".env");
    if let Ok(contents) = fs::read_to_string(&env_path) {
        for line in contents.lines() {
            let line = line.trim();
            if let Some(rest) = line.strip_prefix("RELAY_PC_API_KEY=") {
                return rest.trim().trim_matches(|c| c == '"' || c == '\'').to_string();
            }
        }
    }
    String::new()
}

#[tauri::command]
async fn read_relay_config(project_root: Option<String>) -> Result<RelayConfig, String> {
    let path = config_yaml_path(project_root.clone());
    let url = if path.exists() {
        let yaml = fs::read_to_string(&path).map_err(|e| e.to_string())?;
        extract_block_scalar(&yaml, "relay", "url")
            .unwrap_or("")
            .to_string()
    } else {
        String::new()
    };
    let pc_key_configured = !read_pc_api_key(project_root).is_empty();
    Ok(RelayConfig { url, pc_key_configured })
}

#[tauri::command]
async fn write_relay_url(url: String, project_root: Option<String>) -> Result<(), String> {
    let path = config_yaml_path(project_root);
    let original = if path.exists() {
        fs::read_to_string(&path).map_err(|e| e.to_string())?
    } else {
        String::from("relay:\n")
    };
    let updated = upsert_block_scalar(&original, "relay", "url", url.trim());
    fs::write(&path, updated).map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RelayPairInit {
    pub pair_code: String,
    pub expires_at: String,
    /// The relay URL we used — echoed back so the frontend can encode it in
    /// the QR payload without re-reading config.
    pub url: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RelayPairStatus {
    pub claimed: bool,
    pub expired: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub device_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claimed_at: Option<String>,
}

/// Strip a trailing "/" so we never end up with "https://relay//pair/init".
fn trim_url(url: &str) -> &str {
    url.trim().trim_end_matches('/')
}

#[tauri::command]
async fn relay_pair_init(project_root: Option<String>) -> Result<RelayPairInit, String> {
    let cfg = read_relay_config(project_root.clone()).await?;
    let url = trim_url(&cfg.url);
    if url.is_empty() {
        return Err(
            "No relay URL configured. Set `relay.url` in config.yaml before pairing.".into(),
        );
    }
    let pc_key = read_pc_api_key(project_root);
    if pc_key.is_empty() {
        return Err(
            "RELAY_PC_API_KEY isn't set. Add it to .env so the PC can authenticate to the relay."
                .into(),
        );
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post(format!("{}/pair/init", url))
        .header("X-API-Key", &pc_key)
        .header("Content-Type", "application/json")
        .body("{}")
        .send()
        .await
        .map_err(|e| format!("Network error talking to relay: {}", e))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("Relay /pair/init returned {}: {}", status.as_u16(), text));
    }
    #[derive(Deserialize)]
    struct Body {
        pair_code: String,
        expires_at: String,
    }
    let body: Body = serde_json::from_str(&text).map_err(|e| format!(
        "Relay returned unparseable JSON: {} (raw: {})", e, text
    ))?;
    Ok(RelayPairInit {
        pair_code: body.pair_code,
        expires_at: body.expires_at,
        url: url.to_string(),
    })
}

#[tauri::command]
async fn relay_pair_status(
    pair_code: String,
    project_root: Option<String>,
) -> Result<RelayPairStatus, String> {
    let cfg = read_relay_config(project_root.clone()).await?;
    let url = trim_url(&cfg.url);
    if url.is_empty() {
        return Err("No relay URL configured.".into());
    }
    let pc_key = read_pc_api_key(project_root);
    if pc_key.is_empty() {
        return Err("RELAY_PC_API_KEY isn't set.".into());
    }
    let code = pair_code.trim().to_uppercase();
    if code.is_empty() {
        return Err("Empty pair_code.".into());
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("{}/pair/status/{}", url, code))
        .header("X-API-Key", &pc_key)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if status.as_u16() == 404 {
        // Treat unknown codes as expired so the UI gives up cleanly instead
        // of looping forever on a stale code.
        return Ok(RelayPairStatus {
            claimed: false,
            expired: true,
            device_name: None,
            claimed_at: None,
        });
    }
    if !status.is_success() {
        return Err(format!("Relay /pair/status returned {}: {}", status.as_u16(), text));
    }
    let parsed: RelayPairStatus = serde_json::from_str(&text)
        .map_err(|e| format!("Relay returned unparseable JSON: {} (raw: {})", e, text))?;
    Ok(parsed)
}

// ---------------------------------------------------------------------------
// Account — ~/.config/kim/account.json (platform-native config dir)
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct GoogleAccountEntry {
    pub email: String,
    pub authuser_index: u32,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct GoogleApiAccount {
    #[serde(default)]
    pub email: String,
    #[serde(default)]
    pub connected: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub needs_reauth: Option<bool>,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum GoogleAccountEntryCompat {
    Entry(GoogleAccountEntry),
    Email(String),
}

fn deserialize_google_accounts<'de, D>(deserializer: D) -> Result<Vec<GoogleAccountEntry>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = Vec::<GoogleAccountEntryCompat>::deserialize(deserializer)?;
    let mut seen = std::collections::HashSet::new();
    let mut accounts = Vec::new();
    for (idx, item) in raw.into_iter().enumerate() {
        let entry = match item {
            GoogleAccountEntryCompat::Entry(entry) => {
                if entry.email.trim().is_empty() {
                    None
                } else {
                    Some(GoogleAccountEntry {
                        email: entry.email.trim().to_lowercase(),
                        authuser_index: entry.authuser_index,
                    })
                }
            }
            GoogleAccountEntryCompat::Email(email) => {
                let email = email.trim().to_lowercase();
                if email.is_empty() {
                    None
                } else {
                    Some(GoogleAccountEntry {
                        email,
                        authuser_index: idx as u32,
                    })
                }
            }
        };
        if let Some(entry) = entry {
            if seen.insert(entry.email.clone()) {
                accounts.push(entry);
            }
        }
    }
    Ok(accounts)
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct KimAccount {
    pub display_name: String,
    pub github_username: Option<String>,
    /// Personal Access Token stored locally, never sent anywhere except GitHub.
    pub github_token: Option<String>,
    pub github_avatar_url: Option<String>,
    /// ID of the private backup Gist so restore can find it later.
    pub gist_id: Option<String>,
    pub created_at: String,
    /// Explicit project root paths the user has added to the Code tab.
    /// Kim scans <path>/.claw/sessions/ for each — never ~/.claude/projects/.
    #[serde(default)]
    pub code_projects: Vec<String>,
    /// Google accounts with their authuser indices for Gemini browser sign-in.
    #[serde(default, deserialize_with = "deserialize_google_accounts")]
    pub google_accounts: Vec<GoogleAccountEntry>,
    /// Active Google account email for Gemini browser sessions.
    pub google_active_account: Option<String>,
    /// Official Gemini API OAuth connection metadata. Refresh token is not stored in account.json.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub google_api_account: Option<GoogleApiAccount>,
}

fn account_dir() -> PathBuf {
    dirs::config_dir()
        .or_else(dirs::home_dir)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("kim")
}

fn account_path() -> PathBuf {
    account_dir().join("account.json")
}

#[tauri::command]
async fn load_account() -> Result<Option<KimAccount>, String> {
    let path = account_path();
    if !path.exists() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path).map_err(|e| e.to_string())?;
    let account: KimAccount = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    Ok(Some(account))
}

#[tauri::command]
async fn save_account(account: KimAccount) -> Result<(), String> {
    let dir = account_dir();
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let raw = serde_json::to_string_pretty(&account).map_err(|e| e.to_string())?;
    fs::write(account_path(), raw).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
async fn clear_account() -> Result<(), String> {
    let path = account_path();
    if path.exists() {
        fs::remove_file(&path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
async fn reset_onboarding() -> Result<(), String> {
    clear_account().await
}

#[tauri::command]
async fn delete_all_sessions() -> Result<(), String> {
    let base = default_sessions_dir();
    fs::create_dir_all(&base).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(&base).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with('.') {
            continue;
        }
        if path.is_dir() {
            fs::remove_dir_all(path).map_err(|e| e.to_string())?;
        } else if path.extension().and_then(|e| e.to_str()).is_some_and(|ext| matches!(ext, "jsonl" | "json" | "md" | "zip")) {
            fs::remove_file(path).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Ollama — local daemon status, sign-in launcher, model management
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
struct OllamaModelInfo {
    name: String,
    size: u64,
    modified_at: Option<String>,
    family: Option<String>,
    parameter_size: Option<String>,
    quantization_level: Option<String>,
    cloud: bool,
    installed: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
struct OllamaStatus {
    installed: bool,
    running: bool,
    version: Option<String>,
    state: String,
    message: String,
    installed_path: Option<String>,
    local_models: Vec<OllamaModelInfo>,
    cloud_models: Vec<OllamaModelInfo>,
    cloud_connected: bool,
    cloud_message: Option<String>,
    selected_model: Option<String>,
    selected_model_available: bool,
    selected_mode: String,
    context_limit: Option<u32>,
    context_limit_source: Option<String>,
    error: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct OllamaPullProgress {
    model: String,
    line: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct OllamaPullFinished {
    model: String,
    success: bool,
    error: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaTagsResponse {
    #[serde(default)]
    models: Vec<OllamaTagModel>,
}

#[derive(Deserialize, Default)]
struct OllamaTagModel {
    name: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    modified_at: Option<String>,
    #[serde(default)]
    details: Option<OllamaTagDetails>,
}

#[derive(Deserialize, Default)]
struct OllamaTagDetails {
    #[serde(default)]
    family: Option<String>,
    #[serde(default)]
    parameter_size: Option<String>,
    #[serde(default)]
    quantization_level: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaVersionResponse {
    #[serde(default)]
    version: Option<String>,
}

#[derive(Deserialize, Default)]
struct OllamaShowResponse {
    #[serde(default)]
    parameters: Option<String>,
    #[serde(default)]
    modelfile: Option<String>,
}

fn known_ollama_cloud_models() -> Vec<String> {
    vec![
        "gpt-oss:20b-cloud".to_string(),
        "gpt-oss:120b-cloud".to_string(),
    ]
}

fn find_ollama_binary() -> Option<String> {
    #[cfg(windows)]
    let probe = ("where", vec!["ollama"]);
    #[cfg(not(windows))]
    let probe = ("which", vec!["ollama"]);

    let out = std::process::Command::new(probe.0).args(probe.1).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let first = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())?
        .to_string();
    Some(first)
}

fn parse_ollama_num_ctx(text: &str) -> Option<u32> {
    for line in text.lines() {
        let normalized = line.trim().replace(['=', ':'], " ");
        let parts: Vec<&str> = normalized.split_whitespace().collect();
        for idx in 0..parts.len().saturating_sub(1) {
            if parts[idx] == "num_ctx" {
                if let Ok(n) = parts[idx + 1].parse::<u32>() {
                    return Some(n);
                }
            }
        }
    }
    None
}

fn parse_context_column(raw: &str) -> Option<u32> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    let (num, mult) = if let Some(num) = trimmed.strip_suffix(['K', 'k']) {
        (num, 1_000f64)
    } else if let Some(num) = trimmed.strip_suffix(['M', 'm']) {
        (num, 1_000_000f64)
    } else {
        (trimmed, 1f64)
    };
    let parsed = num.trim().parse::<f64>().ok()?;
    Some((parsed * mult).round() as u32)
}

fn parse_ollama_ps_context(stdout: &str, model: &str) -> Option<u32> {
    let wanted = model.trim().to_lowercase();
    for line in stdout.lines() {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.to_ascii_lowercase().starts_with("name ") {
            continue;
        }
        if !stripped.to_ascii_lowercase().starts_with(&wanted) {
            continue;
        }
        let cols: Vec<&str> = stripped.split_whitespace().collect();
        if cols.len() < 2 {
            continue;
        }
        return cols.last().and_then(|s| parse_context_column(s));
    }
    None
}

async fn ollama_context_from_ps(model: &str) -> Option<u32> {
    let out = tokio::time::timeout(
        Duration::from_secs(5),
        tokio::process::Command::new("ollama").arg("ps").output(),
    )
    .await
    .ok()?
    .ok()?;
    if !out.status.success() {
        return None;
    }
    parse_ollama_ps_context(&String::from_utf8_lossy(&out.stdout), model)
}

async fn ollama_context_from_show(base_url: &str, model: &str) -> Option<u32> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/show", base_url.trim_end_matches('/')))
        .json(&serde_json::json!({ "model": model }))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let payload: OllamaShowResponse = resp.json().await.ok()?;
    payload
        .parameters
        .as_deref()
        .and_then(parse_ollama_num_ctx)
        .or_else(|| payload.modelfile.as_deref().and_then(parse_ollama_num_ctx))
}

async fn ollama_version(base_url: &str) -> Result<Option<String>, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/version", base_url.trim_end_matches('/')))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let payload: OllamaVersionResponse = resp.json().await.map_err(|e| e.to_string())?;
    Ok(payload.version)
}

async fn ollama_tags(base_url: &str) -> Result<Vec<OllamaModelInfo>, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/tags", base_url.trim_end_matches('/')))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }
    let payload: OllamaTagsResponse = resp.json().await.map_err(|e| e.to_string())?;
    Ok(payload
        .models
        .into_iter()
        .map(|m| OllamaModelInfo {
            name: m.name,
            size: m.size,
            modified_at: m.modified_at,
            family: m.details.as_ref().and_then(|d| d.family.clone()),
            parameter_size: m.details.as_ref().and_then(|d| d.parameter_size.clone()),
            quantization_level: m.details.as_ref().and_then(|d| d.quantization_level.clone()),
            cloud: false,
            installed: true,
        })
        .collect())
}

async fn ollama_chat_probe(base_url: &str, model: &str) -> Result<(), String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/api/chat", base_url.trim_end_matches('/')))
        .json(&serde_json::json!({
            "model": model,
            "messages": [{ "role": "user", "content": "Reply with OK." }],
            "stream": false
        }))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if resp.status().is_success() {
        return Ok(());
    }
    let status = resp.status();
    let detail = resp.text().await.unwrap_or_default();
    Err(if detail.trim().is_empty() {
        format!("HTTP {}", status)
    } else {
        detail
    })
}

fn friendly_ollama_cloud_message(detail: &str) -> String {
    let lowered = detail.to_lowercase();
    if lowered.contains("sign in") || lowered.contains("unauthorized") || lowered.contains("forbidden") {
        "Sign in to Ollama to use cloud models".to_string()
    } else if lowered.contains("not found") || lowered.contains("pull") {
        "Cloud model unavailable; use a local model or install the cloud model".to_string()
    } else {
        "Cloud model unavailable; use a local model or sign in".to_string()
    }
}

#[tauri::command]
async fn ollama_get_status(
    base_url: Option<String>,
    selected_model: Option<String>,
    mode: Option<String>,
    context_limit_override: Option<u32>,
) -> Result<OllamaStatus, String> {
    let base_url = base_url.unwrap_or_else(|| "http://localhost:11434".to_string());
    let selected_mode = mode.unwrap_or_else(|| "local".to_string()).to_lowercase();
    let installed_path = find_ollama_binary();
    if installed_path.is_none() {
        return Ok(OllamaStatus {
            installed: false,
            running: false,
            version: None,
            state: "not_installed".to_string(),
            message: "Ollama is not installed".to_string(),
            installed_path: None,
            local_models: vec![],
            cloud_models: known_ollama_cloud_models()
                .into_iter()
                .map(|name| OllamaModelInfo {
                    name,
                    size: 0,
                    modified_at: None,
                    family: None,
                    parameter_size: None,
                    quantization_level: None,
                    cloud: true,
                    installed: false,
                })
                .collect(),
            cloud_connected: false,
            cloud_message: Some("Install Ollama from ollama.com/download".to_string()),
            selected_model,
            selected_model_available: false,
            selected_mode,
            context_limit: context_limit_override,
            context_limit_source: context_limit_override.map(|_| "override".to_string()),
            error: None,
        });
    }

    let version = match ollama_version(&base_url).await {
        Ok(version) => version,
        Err(_) => {
            return Ok(OllamaStatus {
                installed: true,
                running: false,
                version: None,
                state: "installed_not_running".to_string(),
                message: "Ollama is installed but not running".to_string(),
                installed_path,
                local_models: vec![],
                cloud_models: known_ollama_cloud_models()
                    .into_iter()
                    .map(|name| OllamaModelInfo {
                        name,
                        size: 0,
                        modified_at: None,
                        family: None,
                        parameter_size: None,
                        quantization_level: None,
                        cloud: true,
                        installed: false,
                    })
                    .collect(),
                cloud_connected: false,
                cloud_message: Some("Start Ollama to use local or cloud models".to_string()),
                selected_model,
                selected_model_available: false,
                selected_mode,
                context_limit: context_limit_override,
                context_limit_source: context_limit_override.map(|_| "override".to_string()),
                error: None,
            });
        }
    };

    let local_models = ollama_tags(&base_url).await.unwrap_or_default();
    let local_names: std::collections::HashSet<String> = local_models
        .iter()
        .map(|m| m.name.to_lowercase())
        .collect();
    let mut cloud_models: Vec<OllamaModelInfo> = known_ollama_cloud_models()
        .into_iter()
        .map(|name| OllamaModelInfo {
            installed: local_names.contains(&name.to_lowercase()),
            cloud: true,
            name,
            size: 0,
            modified_at: None,
            family: None,
            parameter_size: None,
            quantization_level: None,
        })
        .collect();
    if let Some(extra) = selected_model.as_ref().filter(|m| m.ends_with("-cloud")) {
        if !cloud_models.iter().any(|m| m.name == *extra) {
            cloud_models.push(OllamaModelInfo {
                installed: local_names.contains(&extra.to_lowercase()),
                cloud: true,
                name: extra.clone(),
                size: 0,
                modified_at: None,
                family: None,
                parameter_size: None,
                quantization_level: None,
            });
        }
    }

    let selected = selected_model.clone().filter(|m| !m.trim().is_empty()).or_else(|| {
        if selected_mode == "cloud" {
            Some("gpt-oss:120b-cloud".to_string())
        } else {
            local_models.first().map(|m| m.name.clone())
        }
    });
    let selected_available = selected
        .as_ref()
        .map(|m| {
            if selected_mode == "cloud" {
                m.ends_with("-cloud") || local_names.contains(&m.to_lowercase())
            } else {
                local_names.contains(&m.to_lowercase())
            }
        })
        .unwrap_or(false);

    let (cloud_connected, cloud_message) = if selected_mode == "cloud" {
        let probe_model = selected
            .clone()
            .filter(|m| m.ends_with("-cloud"))
            .unwrap_or_else(|| "gpt-oss:120b-cloud".to_string());
        match ollama_chat_probe(&base_url, &probe_model).await {
            Ok(()) => (true, Some("Connected to Ollama".to_string())),
            Err(detail) => (false, Some(friendly_ollama_cloud_message(&detail))),
        }
    } else {
        (false, Some("Local models work without any Ollama account.".to_string()))
    };

    let (context_limit, context_limit_source) = if let Some(model) = selected.as_ref() {
        if let Some(limit) = ollama_context_from_ps(model).await {
            (Some(limit), Some("ollama_ps".to_string()))
        } else if let Some(limit) = ollama_context_from_show(&base_url, model).await {
            (Some(limit), Some("api_show".to_string()))
        } else if let Some(limit) = context_limit_override {
            (Some(limit), Some("override".to_string()))
        } else {
            (None, Some("unknown".to_string()))
        }
    } else if let Some(limit) = context_limit_override {
        (Some(limit), Some("override".to_string()))
    } else {
        (None, Some("unknown".to_string()))
    };

    let state = if selected_mode == "cloud" {
        if cloud_connected { "connected" } else { "running_not_signed_in" }
    } else if !local_models.is_empty() {
        "connected"
    } else {
        "error"
    };
    let message = match state {
        "connected" => "Connected to Ollama".to_string(),
        "running_not_signed_in" => "Sign in to Ollama to use cloud models".to_string(),
        _ => {
            if local_models.is_empty() {
                "Ollama is running, but no local models are installed".to_string()
            } else {
                "Ollama status unavailable".to_string()
            }
        }
    };

    Ok(OllamaStatus {
        installed: true,
        running: true,
        version,
        state: state.to_string(),
        message,
        installed_path,
        local_models,
        cloud_models,
        cloud_connected,
        cloud_message,
        selected_model: selected,
        selected_model_available: selected_available,
        selected_mode,
        context_limit,
        context_limit_source,
        error: None,
    })
}

#[tauri::command]
async fn ollama_test_model(
    base_url: Option<String>,
    model: String,
) -> Result<bool, String> {
    let base_url = base_url.unwrap_or_else(|| "http://localhost:11434".to_string());
    ollama_chat_probe(&base_url, &model).await.map(|_| true)
}

#[tauri::command]
async fn ollama_signin() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let script = r#"tell application "Terminal"
activate
do script "ollama signin"
end tell"#;
        std::process::Command::new("osascript")
            .arg("-e")
            .arg(script)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", "cmd", "/K", "ollama signin"])
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let candidates = [
            ("x-terminal-emulator", vec!["-e", "sh", "-lc", "ollama signin"]),
            ("gnome-terminal", vec!["--", "sh", "-lc", "ollama signin"]),
            ("konsole", vec!["-e", "sh", "-lc", "ollama signin"]),
        ];
        for (cmd, args) in candidates {
            if std::process::Command::new(cmd).args(args.clone()).spawn().is_ok() {
                return Ok(());
            }
        }
        return Err("Could not launch a terminal for `ollama signin`.".to_string());
    }
}

#[tauri::command]
async fn ollama_pull_model(
    model: String,
    app_handle: tauri::AppHandle,
) -> Result<(), String> {
    let model_name = model.trim().to_string();
    if model_name.is_empty() {
        return Err("Model name is required".to_string());
    }
    tokio::spawn(async move {
        use tokio::io::AsyncBufReadExt;
        use tokio::process::Command;

        let mut cmd = Command::new("ollama");
        cmd.arg("pull")
            .arg(&model_name)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        match cmd.spawn() {
            Ok(mut child) => {
                if let Some(stdout) = child.stdout.take() {
                    let app = app_handle.clone();
                    let model = model_name.clone();
                    tokio::spawn(async move {
                        let mut lines = tokio::io::BufReader::new(stdout).lines();
                        while let Ok(Some(line)) = lines.next_line().await {
                            let _ = app.emit("ollama-pull-progress", OllamaPullProgress { model: model.clone(), line });
                        }
                    });
                }
                if let Some(stderr) = child.stderr.take() {
                    let app = app_handle.clone();
                    let model = model_name.clone();
                    tokio::spawn(async move {
                        let mut lines = tokio::io::BufReader::new(stderr).lines();
                        while let Ok(Some(line)) = lines.next_line().await {
                            let _ = app.emit("ollama-pull-progress", OllamaPullProgress { model: model.clone(), line });
                        }
                    });
                }
                let finished = child.wait().await.map_err(|e| e.to_string());
                match finished {
                    Ok(status) if status.success() => {
                        let _ = app_handle.emit("ollama-pull-finished", OllamaPullFinished {
                            model: model_name,
                            success: true,
                            error: None,
                        });
                    }
                    Ok(status) => {
                        let _ = app_handle.emit("ollama-pull-finished", OllamaPullFinished {
                            model: model_name,
                            success: false,
                            error: Some(format!("`ollama pull` exited with {}", status)),
                        });
                    }
                    Err(err) => {
                        let _ = app_handle.emit("ollama-pull-finished", OllamaPullFinished {
                            model: model_name,
                            success: false,
                            error: Some(err),
                        });
                    }
                }
            }
            Err(err) => {
                let _ = app_handle.emit("ollama-pull-finished", OllamaPullFinished {
                    model: model_name,
                    success: false,
                    error: Some(err.to_string()),
                });
            }
        }
    });
    Ok(())
}

// ---------------------------------------------------------------------------
// GitHub PAT verification — get user identity from token
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct GitHubUser {
    pub login: String,
    pub name: Option<String>,
    pub avatar_url: String,
}

#[tauri::command]
async fn verify_github_pat(token: String) -> Result<GitHubUser, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get("https://api.github.com/user")
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("GitHub returned {}. Check that your token has 'read:user' scope.", resp.status()));
    }

    let user: GitHubUser = resp.json().await.map_err(|e| e.to_string())?;
    Ok(user)
}

// ---------------------------------------------------------------------------
// Data export — ZIP | JSON | Markdown
// ---------------------------------------------------------------------------

#[tauri::command]
async fn export_data(
    format: String,
    output_path: Option<String>,
    sessions_dir: Option<String>,
) -> Result<String, String> {
    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);
    let out = output_path
        .map(PathBuf::from)
        .unwrap_or_else(|| default_export_path(&format));

    match format.as_str() {
        "zip" => export_as_zip(&base, &out),
        "json" => export_as_json(&base, &out),
        "markdown" => export_as_markdown(&base, &out),
        _ => Err(format!("Unknown format: {}. Use 'zip', 'json', or 'markdown'.", format)),
    }
}

fn default_export_path(format: &str) -> PathBuf {
    let ext = match format {
        "markdown" => "md",
        "zip" => "zip",
        _ => "json",
    };
    let stamp = chrono_now().replace([':', '/'], "-");
    dirs::download_dir()
        .or_else(dirs::document_dir)
        .or_else(dirs::home_dir)
        .unwrap_or_else(account_dir)
        .join(format!("kim-export-{stamp}.{ext}"))
}

fn sanitized_account_json(gist_id_hint: Option<&str>) -> String {
    let Ok(raw) = fs::read_to_string(account_path()) else {
        return "{}".to_string();
    };
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return "{}".to_string();
    };
    if let Some(obj) = value.as_object_mut() {
        obj.remove("github_token");
        if let Some(gist_id) = gist_id_hint.map(str::trim).filter(|s| !s.is_empty()) {
            obj.insert("gist_id".to_string(), serde_json::Value::String(gist_id.to_string()));
        }
    }
    serde_json::to_string_pretty(&value).unwrap_or_else(|_| "{}".to_string())
}

fn export_as_zip(sessions_base: &Path, out: &Path) -> Result<String, String> {
    use std::io::Write;
    use zip::write::FileOptions;

    let file = std::fs::File::create(out).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipWriter::new(file);
    let opts: FileOptions<'_, ()> = FileOptions::default().compression_method(zip::CompressionMethod::Deflated);

    let mut count = 0usize;
    collect_jsonl_files(sessions_base, &mut |rel, data| {
        zip.start_file(rel, opts).ok();
        zip.write_all(data).ok();
        count += 1;
    });

    // Include account metadata, but never export credentials/tokens.
    if account_path().exists() {
        let data = sanitized_account_json(None);
        zip.start_file("account.json", opts).ok();
        let _ = zip.write_all(data.as_bytes());
    }

    zip.finish().map_err(|e| e.to_string())?;
    Ok(format!("Exported {} session files to {}", count, out.display()))
}

fn export_as_json(sessions_base: &Path, out: &Path) -> Result<String, String> {
    let mut sessions: Vec<serde_json::Value> = Vec::new();

    collect_jsonl_files(sessions_base, &mut |rel, data| {
        let text = String::from_utf8_lossy(data);
        let messages: Vec<serde_json::Value> = text
            .lines()
            .filter_map(|l| serde_json::from_str(l).ok())
            .collect();
        sessions.push(serde_json::json!({ "session": rel, "messages": messages }));
    });

    let payload = serde_json::json!({
        "version": 1,
        "exported_at": chrono_now(),
        "sessions": sessions,
    });
    let raw = serde_json::to_string_pretty(&payload).map_err(|e| e.to_string())?;
    fs::write(out, raw).map_err(|e| e.to_string())?;
    Ok(format!("Exported {} sessions to {}", sessions.len(), out.display()))
}

fn export_as_markdown(sessions_base: &Path, out: &Path) -> Result<String, String> {
    use std::fmt::Write as FmtWrite;
    let mut md = String::new();
    let _ = writeln!(md, "# Kim Session Export\n\nExported: {}\n", chrono_now());

    let mut count = 0usize;
    collect_jsonl_files(sessions_base, &mut |rel, data| {
        let _ = writeln!(md, "---\n\n## {}\n", rel);
        let text = String::from_utf8_lossy(data);
        for line in text.lines() {
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(line) {
                let role = msg["role"].as_str().unwrap_or("unknown");
                let content = match msg["content"].as_str() {
                    Some(s) => s.to_string(),
                    None => msg["content"].to_string(),
                };
                let _ = writeln!(md, "**{}**: {}\n", role, content);
            }
        }
        count += 1;
    });

    fs::write(out, &md).map_err(|e| e.to_string())?;
    Ok(format!("Exported {} sessions as Markdown to {}", count, out.display()))
}

fn collect_jsonl_files<F>(base: &Path, cb: &mut F)
where
    F: FnMut(String, &[u8]),
{
    if !base.exists() {
        return;
    }
    let Ok(date_dirs) = fs::read_dir(base) else { return };
    for de in date_dirs.filter_map(|e| e.ok()).filter(|e| e.path().is_dir()) {
        let date = de.file_name().to_string_lossy().to_string();
        let Ok(files) = fs::read_dir(de.path()) else { continue };
        for fe in files.filter_map(|e| e.ok()) {
            let name = fe.file_name().to_string_lossy().to_string();
            if name.ends_with(".jsonl") {
                if let Ok(data) = fs::read(fe.path()) {
                    cb(format!("{}/{}", date, name), &data);
                }
            }
        }
    }
}

fn chrono_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    unix_secs_to_utc_iso(secs)
}

fn unix_secs_to_utc_iso(secs: u64) -> String {
    // Convert Unix seconds to (year, month, day, h, m, s) UTC.
    let s = secs % 60;
    let m = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    // Days since Unix epoch (1970-01-01)
    let mut days = secs / 86400;
    // Gregorian calendar algorithm (Julian Day Number method)
    let mut year = 1970u64;
    loop {
        let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
        let days_in_year = if leap { 366 } else { 365 };
        if days < days_in_year { break; }
        days -= days_in_year;
        year += 1;
    }
    let leap = (year.is_multiple_of(4) && !year.is_multiple_of(100)) || year.is_multiple_of(400);
    let month_days: [u64; 12] = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut month = 1u64;
    for &md in &month_days {
        if days < md { break; }
        days -= md;
        month += 1;
    }
    let day = days + 1;
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z", year, month, day, h, m, s)
}

// ---------------------------------------------------------------------------
// Data import — restore from a Kim ZIP or JSON export
// ---------------------------------------------------------------------------

#[tauri::command]
async fn import_data(
    file_path: Option<String>,
    path: Option<String>,
    sessions_dir: Option<String>,
) -> Result<String, String> {
    let file_path = file_path
        .or(path)
        .ok_or_else(|| "File path is required.".to_string())?;
    let src = PathBuf::from(&file_path);
    if !src.exists() {
        return Err(format!("File not found: {}", file_path));
    }

    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);
    fs::create_dir_all(&base).map_err(|e| e.to_string())?;

    let ext = src.extension().and_then(|e| e.to_str()).unwrap_or("");
    match ext {
        "zip" => import_from_zip(&src, &base),
        "json" => import_from_json(&src, &base),
        _ => Err("Unsupported file type. Use a .zip or .json exported from Kim.".to_string()),
    }
}

fn import_from_zip(src: &Path, base: &Path) -> Result<String, String> {
    use std::io::Read;

    let file = std::fs::File::open(src).map_err(|e| e.to_string())?;
    let mut zip = zip::ZipArchive::new(file).map_err(|e| e.to_string())?;
    let mut count = 0usize;

    for i in 0..zip.len() {
        let mut entry = zip.by_index(i).map_err(|e| e.to_string())?;
        let name = entry.name().to_string();

        if name == "account.json" {
            // Restore account if it doesn't exist yet (don't overwrite existing).
            let acct_path = account_path();
            if !acct_path.exists() {
                let mut buf = Vec::new();
                entry.read_to_end(&mut buf).ok();
                if let Ok(mut account) = serde_json::from_slice::<KimAccount>(&buf) {
                    account.github_token = None;
                    if let Some(parent) = acct_path.parent() {
                        fs::create_dir_all(parent).ok();
                    }
                    if let Ok(raw) = serde_json::to_vec_pretty(&account) {
                        fs::write(&acct_path, raw).ok();
                    }
                }
            }
            continue;
        }

        if !name.ends_with(".jsonl") {
            continue;
        }

        let dest = base.join(&name);

        // Guard against path traversal attacks (e.g. "../../etc/passwd" in the ZIP).
        // Canonicalize the destination's parent (which always exists after create_dir_all)
        // and verify the final path stays inside `base`.
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            if let (Ok(canon_dest_parent), Ok(canon_base)) = (parent.canonicalize(), base.canonicalize()) {
                let full = canon_dest_parent.join(dest.file_name().unwrap_or_default());
                if !full.starts_with(&canon_base) {
                    // Silently skip the offending entry.
                    continue;
                }
            }
        } else {
            fs::create_dir_all(base).map_err(|e| e.to_string())?;
        }
        let mut buf = Vec::new();
        entry.read_to_end(&mut buf).map_err(|e| e.to_string())?;
        fs::write(&dest, &buf).map_err(|e| e.to_string())?;
        count += 1;
    }

    Ok(format!("Imported {} session files.", count))
}

fn import_from_json(src: &Path, base: &Path) -> Result<String, String> {
    let raw = fs::read_to_string(src).map_err(|e| e.to_string())?;
    let payload: serde_json::Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;

    let sessions = payload["sessions"].as_array().ok_or("Invalid export format: missing 'sessions' array.")?;
    let mut count = 0usize;

    for session in sessions {
        let rel = session["session"].as_str().unwrap_or("unknown/session.jsonl");

        // Guard against path traversal attacks (#32)
        // Fast-path rejection: disallow .., leading /, and Windows drive prefixes
        if rel.contains("..") || rel.starts_with('/') || rel.starts_with('\\') {
            return Err(format!("path traversal attempt: {:?}", rel));
        }
        if rel.len() >= 2 && rel.as_bytes()[1] == b':' {
            return Err(format!("path traversal attempt (drive prefix): {:?}", rel));
        }

        let dest = base.join(rel);

        // Canonicalize and verify the destination stays inside base
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            if let (Ok(canon_dest_parent), Ok(canon_base)) = (parent.canonicalize(), base.canonicalize()) {
                let full = canon_dest_parent.join(dest.file_name().unwrap_or_default());
                if !full.starts_with(&canon_base) {
                    return Err(format!("path traversal attempt: {:?}", rel));
                }
            }
        } else {
            fs::create_dir_all(base).map_err(|e| e.to_string())?;
        }

        let messages = session["messages"].as_array().cloned().unwrap_or_default();

        let mut lines = String::new();
        for msg in &messages {
            if let Ok(line) = serde_json::to_string(msg) {
                lines.push_str(&line);
                lines.push('\n');
            }
        }
        fs::write(&dest, &lines).map_err(|e| e.to_string())?;
        count += 1;
    }

    Ok(format!("Imported {} sessions.", count))
}

// ---------------------------------------------------------------------------
// GitHub Gist backup / restore
// ---------------------------------------------------------------------------

#[tauri::command]
async fn backup_to_gist(
    token: String,
    sessions_dir: Option<String>,
    existing_gist_id: Option<String>,
) -> Result<String, String> {
    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);

    // Build an index of all sessions (not the full content — Gist has 10MB limit).
    let mut index: Vec<serde_json::Value> = Vec::new();
    collect_jsonl_files(&base, &mut |rel, data| {
        let line_count = data.iter().filter(|&&b| b == b'\n').count();
        index.push(serde_json::json!({ "path": rel, "messages": line_count }));
    });

    let account_data = sanitized_account_json(existing_gist_id.as_deref());

    let payload = serde_json::json!({
        "version": 1,
        "backed_up_at": chrono_now(),
        "session_index": index,
    });

    let files = serde_json::json!({
        "kim_backup.json": { "content": serde_json::to_string_pretty(&payload).unwrap_or_default() },
        "kim_account.json": { "content": account_data },
    });

    let client = reqwest::Client::new();

    let gist_url = match &existing_gist_id {
        Some(id) => format!("https://api.github.com/gists/{}", id),
        None => "https://api.github.com/gists".to_string(),
    };

    let body = serde_json::json!({
        "description": "Kim Desktop backup (private)",
        "public": false,
        "files": files,
    });

    let req = if existing_gist_id.is_some() {
        client.patch(&gist_url)
    } else {
        client.post(&gist_url)
    };

    let resp = req
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let err = resp.text().await.unwrap_or_default();
        return Err(format!("GitHub returned {}: {}", status, err));
    }

    let gist: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;
    let gist_id = gist["id"].as_str().unwrap_or("").to_string();
    Ok(gist_id)
}

#[tauri::command]
async fn restore_from_gist(
    token: String,
    gist_id: String,
) -> Result<KimAccount, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("https://api.github.com/gists/{}", gist_id))
        .header("Authorization", format!("Bearer {}", token))
        .header("User-Agent", "Kim-Desktop/0.1")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("GitHub returned {}. Check the Gist ID.", resp.status()));
    }

    let gist: serde_json::Value = resp.json().await.map_err(|e| e.to_string())?;

    // Restore account from gist.
    if let Some(acct_content) = gist["files"]["kim_account.json"]["content"].as_str() {
        if let Ok(mut account) = serde_json::from_str::<KimAccount>(acct_content) {
            account.github_token = None;
            account.gist_id = Some(gist_id.clone());
            // Write to local account file if not present.
            if !account_path().exists() {
                let dir = account_dir();
                fs::create_dir_all(&dir).ok();
                if let Ok(raw) = serde_json::to_string_pretty(&account) {
                    fs::write(account_path(), raw).ok();
                }
            }
            return Ok(account);
        }
    }

    Err("Gist found but kim_account.json was empty or invalid.".to_string())
}

// ---------------------------------------------------------------------------
// Claw (Code) projects — grouped by project directory + git branch
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ClawSession {
    pub session_id: String,
    /// YYYY-MM-DD bucket under <project>/.claw/sessions. Used to locate the file on disk.
    pub date: String,
    /// ISO timestamp (UTC) derived from file modified time. Used for sorting/display.
    pub updated_at: String,
    pub message_count: usize,
    pub summary: Option<String>,
    pub title: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ClawBranch {
    pub name: String,           // git branch name, or "main" / "unknown"
    pub sessions: Vec<ClawSession>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ClawProject {
    pub path: String,           // decoded project path
    pub name: String,           // last path component (display name)
    pub current_branch: String, // current git branch in that dir
    pub branches: Vec<ClawBranch>,
}

fn read_git_branch(project_path: &Path) -> String {
    let head = project_path.join(".git").join("HEAD");
    if let Ok(content) = fs::read_to_string(&head) {
        let s = content.trim();
        if let Some(branch) = s.strip_prefix("ref: refs/heads/") {
            return branch.to_string();
        }
        if s.len() >= 8 {
            return s[..8].to_string();
        }
    }
    "main".to_string()
}

/// List Claw sessions for explicitly-added project paths.
/// Scans <project>/.claw/sessions/ — NEVER ~/.claude/projects/.
/// Claw and Claude Code must never mix.
#[tauri::command]
async fn list_claw_projects(project_paths: Vec<String>) -> Result<Vec<ClawProject>, String> {
    let mut result: Vec<ClawProject> = Vec::new();

    for raw_path in project_paths {
        let project_path = PathBuf::from(&raw_path);
        if !project_path.exists() {
            continue;
        }

        let name = project_path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| raw_path.clone());

        let current_branch = read_git_branch(&project_path);

        // Claw stores sessions at <project>/.claw/sessions/ — this is the
        // ONLY place we look. ~/.claude/projects is off-limits.
        let claw_sessions_dir = project_path.join(".claw").join("sessions");
        let sessions = if claw_sessions_dir.exists() {
            read_project_sessions(&claw_sessions_dir)
        } else {
            vec![]
        };

        // Build branch list. For now we group all sessions under the current
        // branch. Future: walk git log to bucket sessions by branch.
        let branches = if sessions.is_empty() {
            vec![]
        } else {
            vec![ClawBranch {
                name: current_branch.clone(),
                sessions,
            }]
        };

        result.push(ClawProject {
            path: raw_path,
            name,
            current_branch,
            branches,
        });
    }

    Ok(result)
}

/// Add a project root path to the account's code_projects list.
#[tauri::command]
async fn add_code_project(path: String) -> Result<Vec<String>, String> {
    let account_path = account_path();
    let mut account: KimAccount = if account_path.exists() {
        let raw = fs::read_to_string(&account_path).map_err(|e| e.to_string())?;
        serde_json::from_str(&raw).map_err(|e| e.to_string())?
    } else {
        return Err("No account found".to_string());
    };

    let canonical = PathBuf::from(&path)
        .canonicalize()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or(path);

    if !account.code_projects.contains(&canonical) {
        account.code_projects.push(canonical);
    }

    let json = serde_json::to_string_pretty(&account).map_err(|e| e.to_string())?;
    fs::write(&account_path, json).map_err(|e| e.to_string())?;
    Ok(account.code_projects)
}

/// Remove a project root path from the account's code_projects list.
#[tauri::command]
async fn remove_code_project(path: String) -> Result<Vec<String>, String> {
    let account_path = account_path();
    let mut account: KimAccount = if account_path.exists() {
        let raw = fs::read_to_string(&account_path).map_err(|e| e.to_string())?;
        serde_json::from_str(&raw).map_err(|e| e.to_string())?
    } else {
        return Err("No account found".to_string());
    };

    account.code_projects.retain(|p| p != &path);

    let json = serde_json::to_string_pretty(&account).map_err(|e| e.to_string())?;
    fs::write(&account_path, json).map_err(|e| e.to_string())?;
    Ok(account.code_projects)
}

/// Open a filesystem path in the OS file manager.
/// On macOS this opens Finder (equivalent to "Open in Finder").
#[tauri::command]
async fn open_in_finder(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if !p.exists() {
        return Err(format!("Path does not exist: {path}"));
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        std::process::Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
}

fn read_project_sessions(dir: &Path) -> Vec<ClawSession> {
    // Claw stores sessions bucketed by date: <dir>/<YYYY-MM-DD>/<session_id>.jsonl.
    // Walk each date subdirectory, then collect the .jsonl files inside.
    let mut sessions = Vec::new();
    let Ok(date_entries) = fs::read_dir(dir) else { return sessions };

    let mut date_dirs: Vec<_> = date_entries
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .collect();
    date_dirs.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for date_entry in date_dirs {
        let date = date_entry.file_name().to_string_lossy().to_string();
        let date_path = date_entry.path();
        let Ok(file_entries) = fs::read_dir(&date_path) else { continue };

        let mut files: Vec<_> = file_entries
            .filter_map(|e| e.ok())
            .filter(|e| {
                let n = e.file_name();
                let s = n.to_string_lossy();
                s.ends_with(".jsonl") && !s.contains(".summary")
            })
            .collect();
        files.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

        for fe in files {
            if sessions.len() >= 50 {
                return sessions;
            }
            let session_id = fe.path()
                .file_stem()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            let modified_secs = fe
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let modified_at = unix_secs_to_utc_iso(modified_secs);
            let message_count = count_lines(&fe.path()).unwrap_or(0);
            let summary_path = date_path.join(format!("{}.summary.txt", session_id));
            let summary = if summary_path.exists() {
                fs::read_to_string(&summary_path)
                    .ok()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            };
            let title = infer_session_title(&fe.path(), summary.as_ref(), &session_id);
            sessions.push(ClawSession {
                session_id,
                date: date.clone(),
                updated_at: modified_at,
                message_count,
                summary,
                title,
            });
        }
    }
    sessions
}

fn newest_claw_session(project_path: &Path) -> Option<CompletedClawSession> {
    let sessions_dir = project_path.join(".claw").join("sessions");
    let date_entries = fs::read_dir(&sessions_dir).ok()?;
    let mut newest: Option<(SystemTime, PathBuf, String, String)> = None;

    for date_entry in date_entries.filter_map(|e| e.ok()) {
        let date_path = date_entry.path();
        if !date_path.is_dir() {
            continue;
        }
        let date = date_entry.file_name().to_string_lossy().to_string();
        let Ok(file_entries) = fs::read_dir(&date_path) else { continue };
        for file_entry in file_entries.filter_map(|e| e.ok()) {
            let path = file_entry.path();
            let name = file_entry.file_name().to_string_lossy().to_string();
            if !name.ends_with(".jsonl") || name.contains(".summary") {
                continue;
            }
            let modified = file_entry
                .metadata()
                .and_then(|m| m.modified())
                .unwrap_or(SystemTime::UNIX_EPOCH);
            let is_newest = newest
                .as_ref()
                .map(|(prev, _, _, _)| modified > *prev)
                .unwrap_or(true);
            if is_newest {
                let session_id = path
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .to_string();
                newest = Some((modified, path, date.clone(), session_id));
            }
        }
    }

    let (_, path, date, session_id) = newest?;
    let summary_path = sessions_dir.join(&date).join(format!("{}.summary.txt", session_id));
    let summary = if summary_path.exists() {
        fs::read_to_string(summary_path)
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    } else {
        None
    };
    let message_count = count_lines(&path).unwrap_or(0);
    let title = infer_session_title(&path, summary.as_ref(), &session_id);
    let project_path_str = project_path.to_string_lossy().to_string();

    Some(CompletedClawSession {
        session_key: format!("claw:{}:{}", date, session_id),
        session_id,
        title,
        date,
        message_count,
        has_summary: summary.is_some(),
        summary,
        session_type: "claw".to_string(),
        project_path: project_path_str,
    })
}

// ---------------------------------------------------------------------------
// Feedback — POST to a private Discord webhook (URL never exposed to frontend)
// ---------------------------------------------------------------------------

/// The Discord webhook URL is embedded at compile time from the
/// KIM_DISCORD_WEBHOOK environment variable.  Set it before `cargo build`:
///   export KIM_DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
/// Leave unset (or empty) to silently no-op — the user sees a success message
/// so they're not confused, but nothing is transmitted.
const DISCORD_WEBHOOK_URL: &str = match option_env!("KIM_DISCORD_WEBHOOK") {
    Some(url) => url,
    None => "",
};

#[derive(serde::Deserialize)]
pub struct FeedbackPayload {
    pub category: String,    // "bug" | "feature" | "general" | "praise" | "other"
    pub message: String,
    pub contact: Option<String>, // optional email the user chose to share
}

#[tauri::command]
async fn send_feedback(payload: FeedbackPayload) -> Result<(), String> {
    if DISCORD_WEBHOOK_URL.is_empty() {
        // Webhook not configured — silently succeed so UX is clean.
        return Ok(());
    }

    let category_label = match payload.category.as_str() {
        "bug"     => "🐛 Bug Report",
        "feature" => "✨ Feature Request",
        "praise"  => "🙏 Praise",
        "general" => "💬 General Feedback",
        _         => "📝 Feedback",
    };

    let contact_field = payload.contact
        .filter(|s| !s.trim().is_empty())
        .map(|email| serde_json::json!({
            "name": "Contact",
            "value": email,
            "inline": true,
        }));

    let mut fields = vec![
        serde_json::json!({
            "name": "Category",
            "value": category_label,
            "inline": true,
        }),
        serde_json::json!({
            "name": "Message",
            "value": &payload.message,
            "inline": false,
        }),
    ];
    if let Some(cf) = contact_field {
        fields.push(cf);
    }

    let color = match payload.category.as_str() {
        "bug"     => 0xef4444u32, // red
        "feature" => 0x6366f1,    // indigo
        "praise"  => 0x22c55e,    // green
        _         => 0x64748b,    // slate
    };

    let body = serde_json::json!({
        "embeds": [{
            "title": format!("{} — Kim Desktop", category_label),
            "color": color,
            "fields": fields,
            "footer": { "text": format!("Kim Desktop — {}", chrono_now()) },
        }]
    });

    let client = reqwest::Client::new();
    client
        .post(DISCORD_WEBHOOK_URL)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Network error: {}", e))?
        .error_for_status()
        .map_err(|e| format!("Webhook error: {}", e))?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let task_state: TaskState = Arc::new(Mutex::new(RunningTask::default()));

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
        .invoke_handler(tauri::generate_handler![
            list_sessions,
            delete_sessions,
            load_session_messages,
            summarize_session,
            save_run_history,
            load_run_history,
            get_app_version,
            get_platform_info,
            run_update,
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
            read_voice_config,
            write_voice_config,
            read_relay_config,
            write_relay_url,
            relay_pair_init,
            relay_pair_status,
            google_oauth::google_oauth_status,
            google_oauth::google_oauth_start,
            google_oauth::google_oauth_disconnect,
            google_oauth::google_oauth_test,
            google_oauth::google_oauth_setup_free_tier_project,
            load_account,
            save_account,
            clear_account,
            reset_onboarding,
            delete_all_sessions,
            ollama_get_status,
            ollama_test_model,
            ollama_signin,
            ollama_pull_model,
            verify_github_pat,
            export_data,
            import_data,
            backup_to_gist,
            restore_from_gist,
            list_claw_projects,
            add_code_project,
            remove_code_project,
            open_in_finder,
            send_feedback,
            show_screenshot_flash,
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
