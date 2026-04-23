use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::time::{Duration, Instant};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use tauri::{Emitter, Manager, State};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use tokio::sync::Mutex;

// ---------------------------------------------------------------------------
// Shared state — currently running agent child (for cancellation)
// ---------------------------------------------------------------------------

#[derive(Default)]
pub struct RunningTask {
    /// PID of the running agent subprocess, if any.
    pid: Option<u32>,
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

static WEBVIEW_BRIDGE_CFG: OnceLock<WebviewBridgeConfig> = OnceLock::new();
static WEBVIEW_BRIDGE_LOCK: OnceLock<StdMutex<()>> = OnceLock::new();
static WEBVIEW_BRIDGE_REQ_COUNTER: AtomicU64 = AtomicU64::new(1);
static WEBVIEW_BRIDGE_RESULTS: OnceLock<StdMutex<HashMap<String, BridgeCompleteResponse>>> = OnceLock::new();
const BRIDGE_COLLECTOR_MODE: &str = "title_pulse_v3";
const BRIDGE_COMPLETION_TIMEOUT_S: u64 = 160;

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct SessionInfo {
    pub session_id: String,
    pub title: String,
    pub date: String,
    pub message_count: usize,
    pub has_summary: bool,
    pub summary: Option<String>,
    pub session_type: String, // "kim" or "claw"
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

            sessions.push(SessionInfo {
                session_id,
                title,
                date: date_str.clone(),
                message_count,
                has_summary,
                summary,
                session_type: session_type.to_string(),
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
            Err(e) => eprintln!("Skipping malformed JSONL line {}: {}", i + 1, e),
        }
    }

    Ok(messages)
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
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("/Users/adammaged/Desktop/Personal/kim/.cursor/debug-16b33e.log")
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

fn default_site_url(site: &str) -> &'static str {
    match normalize_site(site).as_str() {
        "chatgpt" => "https://chatgpt.com",
        "gemini" => "https://gemini.google.com",
        "deepseek" => "https://chat.deepseek.com",
        "grok" => "https://grok.com",
        _ => "https://claude.ai/new",
    }
}

fn open_browser_signin_window_impl(
    url: &str,
    provider_name: Option<String>,
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
        let js_url = serde_json::to_string(trimmed).map_err(|e| e.to_string())?;
        let _ = existing.eval(format!("window.location.href = {};", js_url));
        let _ = existing.show();
        let _ = existing.set_focus();
        return Ok("Opened in existing Kim browser window".to_string());
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
    .visible(true)
    .build()
    .map_err(|e| format!("Failed to open Kim browser window: {}", e))?;

    let window_for_close = window.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            // Keep the webview session alive for background/headless execution.
            api.prevent_close();
            let _ = window_for_close.hide();
        }
    });

    if let Some(existing) = app_handle.get_webview_window(label) {
        let _ = existing.set_focus();
    }

    Ok("Opened in Kim browser window".to_string())
}

fn build_bridge_complete_script(
    site: &str,
    prompt: &str,
    req_id: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
) -> Result<String, String> {
    let site_json = serde_json::to_string(site).map_err(|e| e.to_string())?;
    let prompt_json = serde_json::to_string(prompt).map_err(|e| e.to_string())?;
    let req_json = serde_json::to_string(req_id).map_err(|e| e.to_string())?;
    let attachments_json = serde_json::to_string(attachments).map_err(|e| e.to_string())?;
    let callback_url_json = serde_json::to_string(callback_url).map_err(|e| e.to_string())?;
    let callback_token_json = serde_json::to_string(callback_token).map_err(|e| e.to_string())?;

        let mut script = r#"
(() => {
    setTimeout(async () => {
    try {
  const __kimSite = __KIM_SITE__;
  const __kimPrompt = __KIM_PROMPT__;
  const __kimReqId = __KIM_REQID__;
    const __kimAttachments = __KIM_ATTACHMENTS__;
    const __kimCallbackUrl = __KIM_CALLBACK_URL__;
    const __kimCallbackToken = __KIM_CALLBACK_TOKEN__;
  const __KIM_DONE_PREFIX = "__KIMBRIDGE_DONE__";
    let __kimFinished = false;
    let __kimWatchdog = null;

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
      send_selectors: ["button[aria-label*='Send']", "button[type='submit']", "div[role='button']"],
      stop_selectors: ["button[aria-label*='Stop']", "div[role='button'][class*='stop']"],
      response_selectors: ["div.ds-markdown"],
            upload_button_selectors: ["button[aria-label*='Upload']", "button[aria-label*='Attach']", "div[role='button']"],
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

  // #region agent log
  const __kimDbg = (hypothesisId, message, data) => {
    fetch('http://127.0.0.1:7243/ingest/52674002-420c-4794-b88a-e97e502fc8b6',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'16b33e'},body:JSON.stringify({sessionId:'16b33e',runId:__kimReqId,hypothesisId,location:'desktop/src-tauri/src/lib.rs:build_bridge_complete_script',message,data,timestamp:Date.now()})}).catch(()=>{});
  };
  // #endregion

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

    const doneBase = `${__KIM_DONE_PREFIX}:${__kimReqId}`;
    let pulseCount = 0;
    const pulseDone = () => {
        if (!window.__kimPulsePaused) {
            try { document.title = `${doneBase}:${Date.now()}`; } catch (_) {}
        }
        pulseCount += 1;
        if (pulseCount < 600) setTimeout(pulseDone, 100);
    };
    pulseDone();

    // Fire-and-forget callback; never block done signaling on network.
    Promise.resolve().then(async () => {
        try {
            await fetch(__kimCallbackUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Kim-Token': __kimCallbackToken,
                },
                body: JSON.stringify({
                    req_id: __kimReqId,
                    payload,
                }),
            });
            __kimDbg('H2', 'callback posted', { reqId: __kimReqId, url: __kimCallbackUrl });
        } catch (err) {
            __kimDbg('H3', 'callback post failed', { reqId: __kimReqId, error: String(err) });
        }
    });
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

    const findElement = (selectors, opts = { visible: false, enabled: false }) => {
        for (const sel of selectors || []) {
            let nodes = [];
            try {
                nodes = Array.from(document.querySelectorAll(sel));
            } catch (_) {
                continue;
            }
            for (const el of nodes) {
                if (opts.visible && !isVisible(el)) continue;
                if (opts.enabled && !isEnabled(el)) continue;
                return el;
            }
        }
        return null;
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
            const uploadSel = findSelector(cfg.upload_button_selectors);
            if (uploadSel) {
                try {
                    const uploadBtn = document.querySelector(uploadSel);
                    uploadBtn?.click();
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

        // Fallback: image clipboard paste when no file input is exposed.
        const imageFile = files.find(f => String(f.type || '').startsWith('image/'));
        if (imageFile && inputEl) {
            try {
                inputEl.focus();
                const item = new ClipboardItem({ [imageFile.type]: imageFile });
                await navigator.clipboard.write([item]);
                const isMac = navigator.platform.toLowerCase().includes('mac');
                const combo = isMac ? { metaKey: true, ctrlKey: false } : { metaKey: false, ctrlKey: true };
                inputEl.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'v', code: 'KeyV', bubbles: true, ...combo,
                }));
                await sleep(450);
                return 1;
            } catch (_) {}
        }

        return 0;
    };

    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();

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

    const injectPromptText = async (inputEl, promptText) => {
        const target = String(promptText || '');
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
            inputEl.innerHTML = '';
            const lines = target.split('\n');
            for (const line of lines) {
                const div = document.createElement('div');
                div.textContent = line;
                inputEl.appendChild(div);
            }
        }

        // Gemini rich-textarea can keep source-of-truth in an inner textarea.
        try {
            const rich = inputEl.closest('rich-textarea');
            const mirror = rich ? rich.querySelector('textarea, input') : null;
            if (mirror && (mirror instanceof HTMLTextAreaElement || mirror instanceof HTMLInputElement)) {
                const proto = Object.getPrototypeOf(mirror);
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(mirror, target); else mirror.value = target;
                mirror.dispatchEvent(new Event('input', { bubbles: true }));
                mirror.dispatchEvent(new Event('change', { bubbles: true }));
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

                const inputEl = findElement(cfg.input_selectors, { visible: true, enabled: false });
        if (!inputEl) {
            throw new Error(`Could not find input selector for ${siteKey}. selectorDiag=${selectorDiagText}`);
    }
    inputEl.focus();

        const uploadedCount = await injectAttachments(cfg, inputEl);
        if (uploadedCount > 0) {
            await sleep(200);
        }

        const injectedLen = await injectPromptText(inputEl, __kimPrompt);
        if (injectedLen < Math.min(8, normalizeText(__kimPrompt).length)) {
            throw new Error('Prompt text was not accepted by the chat input.');
        }

    await sleep(80);

        const getSendButton = () => {
                        return findElement(cfg.send_selectors, { visible: true, enabled: true });
        };

        // Give reactive UIs a moment to enable the send button.
                for (let i = 0; i < 10; i++) {
                    ensureWithinDeadline('wait_send_button');
                        const btn = getSendButton();
                        if (btn) break;
                await sleep(120);
        }

        const stateBeforeSend = captureResponseState(cfg, siteKey);
    let sent = false;
                let usedButton = false;
        const sendEl = getSendButton();
        if (sendEl) {
            sendEl.click();
            sent = true;
                        usedButton = true;
    }
    if (!sent) {
            inputEl.focus();
            inputEl.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
            }));
            inputEl.dispatchEvent(new KeyboardEvent('keypress', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
            }));
            inputEl.dispatchEvent(new KeyboardEvent('keyup', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
            }));
            sent = true;
            await sleep(350);
            const postEnterState = captureResponseState(cfg, siteKey);
            const stillReadyToSend = !!getSendButton();
            if (!hasNewResponseSince(stateBeforeSend, postEnterState) && stillReadyToSend) {
                sent = false;
            }
    }
        if (!sent) {
            try {
                const form = inputEl.closest('form');
                if (form) {
                                        if (typeof form.requestSubmit === 'function') {
                                                form.requestSubmit();
                                        } else {
                                                form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                                        }
                    sent = true;
                }
            } catch (_) {}
        }
                if (!sent) {
                    throw new Error(`Could not submit prompt to provider UI. selectorDiag=${selectorDiagText}`);
                }
        // #region agent log
                __kimDbg('H1', 'send stage finished', { sent, usedButton, hasForm: !!(inputEl && inputEl.closest && inputEl.closest('form')), inputTextLen: readInputText(inputEl).length });
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
            await sleep(250);
            sawStop = isAnyStopVisible(cfg);
        }

        if (sawStop) {
            const doneDeadline = Date.now() + GENERATION_DONE_WAIT_MS;
            while (Date.now() < doneDeadline) {
                ensureWithinDeadline('wait_generation_done');
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
"#.to_string();

    script = script.replace("__KIM_SITE__", &site_json);
    script = script.replace("__KIM_PROMPT__", &prompt_json);
    script = script.replace("__KIM_REQID__", &req_json);
    script = script.replace("__KIM_ATTACHMENTS__", &attachments_json);
    script = script.replace("__KIM_CALLBACK_URL__", &callback_url_json);
    script = script.replace("__KIM_CALLBACK_TOKEN__", &callback_token_json);
    Ok(script)
}

/// Pull-based bridge payload collector.
///
/// Protocol:
///   1. JS posts `{req_id, payload}` to `/v1/callback`.
///   2. Rust callback handler stores payload in `WEBVIEW_BRIDGE_RESULTS`.
///   3. Collector polls the map until payload appears or timeout elapses.
fn collect_bridge_payload_from_title(
    window: &tauri::WebviewWindow,
    req_id: &str,
    timeout: Duration,
) -> Result<BridgeCompleteResponse, String> {
    let started = Instant::now();
    let result_store = WEBVIEW_BRIDGE_RESULTS.get_or_init(|| StdMutex::new(HashMap::new()));
    let done_prefix = format!("__KIMBRIDGE_DONE__:{}", req_id);
    let fatal_prefix = "__KIMBRIDGE_FATAL__:";
    let null_marker = "__KIMBRIDGE_NULL__";
    let req_id_json = serde_json::to_string(req_id)
        .map_err(|e| format!("Failed to encode req_id for JS: {}", e))?;
    let mut saw_done_pulse = false;
    let mut eval_pull_attempts: u64 = 0;
    let mut last_eval_pull_at = Instant::now() - Duration::from_secs(3);

    loop {
        match result_store.lock() {
            Ok(mut guard) => {
                if let Some(payload) = guard.remove(req_id) {
                    return Ok(payload);
                }
            }
            Err(_) => {
                return Err("Bridge results lock poisoned.".to_string());
            }
        }

        if let Ok(title) = window.title() {
            if let Some(msg) = title.strip_prefix(fatal_prefix) {
                let fatal_message = msg.trim();
                agent_debug_log(
                    "H3",
                    "collect saw fatal title",
                    serde_json::json!({ "reqId": req_id, "title": title }),
                );
                return Err(format!("Bridge script fatal error: {}", fatal_message));
            }
        }

        // Primary path: poll JS store via eval every 2 seconds.
        if last_eval_pull_at.elapsed() >= Duration::from_millis(500) {
            last_eval_pull_at = Instant::now();
            eval_pull_attempts += 1;
            match pull_payload_from_js_store(window, &req_id_json, null_marker) {
                Ok(Some(payload)) => return Ok(payload),
                Ok(None) => {}
                Err(e) => {
                    agent_debug_log(
                        "H3",
                        "collect eval pull failed",
                        serde_json::json!({ "reqId": req_id, "error": e }),
                    );
                }
            }
        }

        // Secondary path: observe done pulse for diagnostics/confirmation.
        if !saw_done_pulse {
            if let Ok(title) = window.title() {
                if title.starts_with(&done_prefix) {
                    saw_done_pulse = true;
                    agent_debug_log(
                        "H2",
                        "collect saw done pulse",
                        serde_json::json!({ "reqId": req_id, "title": title }),
                    );
                }
            }
        }

        if started.elapsed() >= timeout {
            agent_debug_log(
                "H3",
                "collect timeout waiting callback payload",
                serde_json::json!({
                    "reqId": req_id,
                    "sawDonePulse": saw_done_pulse,
                    "evalPullAttempts": eval_pull_attempts,
                }),
            );
            return Err("Timed out waiting for in-app browser completion response.".to_string());
        }

        std::thread::sleep(Duration::from_millis(20));
    }
}

fn pull_payload_from_js_store(
    window: &tauri::WebviewWindow,
    req_id_json: &str,
    null_marker: &str,
) -> Result<Option<BridgeCompleteResponse>, String> {
    let write_js = format!(
        r#"(() => {{
            window.__kimPulsePaused = true;
            const entry = (window.__kimBridgeStore || {{}})[{req_id_json}];
            const val = (entry && typeof entry.data === 'string' && entry.data.length > 0)
                ? entry.data : '{null_marker}';
            document.title = val;
        }})()"#,
        req_id_json = req_id_json,
        null_marker = null_marker,
    );
    window
        .eval(&write_js)
        .map_err(|e| e.to_string())?;
    std::thread::sleep(Duration::from_millis(60));

    let encoded = window.title().map_err(|e| e.to_string())?;

    // Resume pulseDone regardless of outcome.
    let _ = window.eval("(() => { window.__kimPulsePaused = false; })()");

    // A real base64 payload should not contain spaces; if we read a page title,
    // treat it as not-ready and let the caller retry.
    if encoded == null_marker || encoded.trim().is_empty() || encoded.contains(' ') {
        return Ok(None);
    }

    let decoded = base64::engine::general_purpose::STANDARD
        .decode(&encoded)
        .map_err(|e| format!("base64 decode failed: {}", e))?;
    let decoded_str = String::from_utf8(decoded)
        .map_err(|e| format!("utf8 decode failed: {}", e))?;
    let payload: BridgeCompleteResponse = serde_json::from_str(&decoded_str)
        .map_err(|e| format!("json parse failed: {}", e))?;

    let clear_js = format!(
        "try {{ delete (window.__kimBridgeStore || {{}})[{req_id_json}]; }} catch(_) {{}}",
        req_id_json = req_id_json,
    );
    let _ = window.eval(&clear_js);

    Ok(Some(payload))
}

fn run_bridge_completion_once(
    window: &tauri::WebviewWindow,
    site: &str,
    prompt: &str,
    attachments: &[BridgeAttachment],
    callback_url: &str,
    callback_token: &str,
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
            "collectorMode": BRIDGE_COLLECTOR_MODE,
            "collectorTimeoutS": BRIDGE_COMPLETION_TIMEOUT_S,
        }),
    ); 

    if let Ok(mut guard) = WEBVIEW_BRIDGE_RESULTS
        .get_or_init(|| StdMutex::new(HashMap::new()))
        .lock()
    {
        guard.remove(&req_id);
    }

    let script = build_bridge_complete_script(
        site,
        prompt,
        &req_id,
        attachments,
        callback_url,
        callback_token,
    )
        .map_err(|e| format!("Script build failed: {}", e))?;

    agent_debug_log(
        "H1",
        "bridge eval begin",
        serde_json::json!({ "reqId": req_id, "scriptLen": script.len() }),
    );

    if let Err(e) = window.eval(&script) {
        agent_debug_log(
            "H3",
            "bridge eval failed",
            serde_json::json!({ "reqId": req_id, "error": e.to_string() }),
        );
        return Err(format!("Failed to evaluate in-app script: {}", e));
    }

    agent_debug_log(
        "H1",
        "bridge eval returned",
        serde_json::json!({ "reqId": req_id }),
    );

    agent_debug_log(
        "H1",
        "bridge collect begin",
        serde_json::json!({
            "reqId": req_id,
            "timeoutS": BRIDGE_COMPLETION_TIMEOUT_S,
            "mode": BRIDGE_COLLECTOR_MODE,
        }),
    );

    let result = collect_bridge_payload_from_title(
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

    if !(method == Method::Get && path == "/v1/health") {
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

    match (method, path.as_str()) {
        (Method::Get, "/v1/health") => {
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

            let window = if let Some(w) = app_handle.get_webview_window("kim-browser-signin") {
                w
            } else {
                let open_result = open_browser_signin_window_impl(
                    default_site_url(&site),
                    Some(site.clone()),
                    &app_handle,
                );
                if let Err(e) = open_result {
                    respond_json(
                        request,
                        500,
                        serde_json::json!({"ok": false, "error": format!("Could not open in-app browser window: {}", e)}),
                    );
                    return;
                }
                respond_json(
                    request,
                    409,
                    serde_json::json!({
                        "ok": false,
                        "error": "In-app browser window opened. Sign in to the provider, then resend your task.",
                    }),
                );
                return;
            };

            // If the user previously closed the browser window (which hides it rather
            // than destroying it so the login session stays alive), show it briefly
            // before injecting JS.  WKWebView reports offsetParent=null for every
            // element while a window is hidden, which causes our isVisible() helper
            // to reject all input/send selectors.  Showing upfront avoids an
            // unnecessary failure → hidden_retry_needed cycle on every call.
            let was_hidden = window.is_visible().map(|v| !v).unwrap_or(false);
            if was_hidden {
                let _ = window.show();
                let _ = window.set_focus();
                // Give the window and its compositor layer time to become active.
                std::thread::sleep(Duration::from_millis(150));
            }

            let callback_url = WEBVIEW_BRIDGE_CFG
                .get()
                .map(|cfg| format!("{}/v1/callback", cfg.base_url))
                .unwrap_or_else(|| "http://127.0.0.1:18991/v1/callback".to_string());

            let mut completion = run_bridge_completion_once(
                &window,
                &site,
                &parsed.prompt,
                &parsed.attachments,
                &callback_url,
                token.as_str(),
            );

            // If the input selector still wasn't found (page may have navigated away
            // from the chat view), reload the provider's root URL and try once more.
            let needs_nav_retry = match &completion {
                Ok(payload) => {
                    let err = payload.error.clone().unwrap_or_default().to_lowercase();
                    !payload.ok && err.contains("could not find input selector")
                }
                Err(err) => err.to_lowercase().contains("could not find input selector"),
            };

            if needs_nav_retry {
                let nav_url = default_site_url(&site);
                if let Ok(js_url) = serde_json::to_string(nav_url) {
                    let _ = window.eval(format!("window.location.href = {};", js_url));
                    std::thread::sleep(Duration::from_millis(2000));
                    completion = run_bridge_completion_once(
                        &window,
                        &site,
                        &parsed.prompt,
                        &parsed.attachments,
                        &callback_url,
                        token.as_str(),
                    );
                }
            }

            // Hide again if we showed the window just for this request.
            if was_hidden {
                let _ = window.hide();
            }

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
        _ => {
            respond_json(
                request,
                404,
                serde_json::json!({"ok": false, "error": format!("Unknown bridge route: {}", path)}),
            );
        }
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

    let token = format!(
        "kim-{}-{}",
        std::process::id(),
        WEBVIEW_BRIDGE_REQ_COUNTER.fetch_add(1, Ordering::Relaxed)
    );
    let base_url = format!("http://127.0.0.1:{}", port);

    let _ = WEBVIEW_BRIDGE_CFG.set(WebviewBridgeConfig {
        base_url: base_url.clone(),
        token: token.clone(),
    });

    std::thread::spawn(move || {
        eprintln!(
            "[Kim] In-app browser bridge listening at {} (mode={}, timeout={}s)",
            base_url,
            BRIDGE_COLLECTOR_MODE,
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
async fn load_session_messages(
    session_id: String,
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

#[tauri::command]
async fn open_browser_signin_window(
    url: String,
    provider_name: Option<String>,
    app_handle: tauri::AppHandle,
) -> Result<String, String> {
    open_browser_signin_window_impl(&url, provider_name, &app_handle)
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

#[tauri::command]
async fn send_task(
    task: String,
    provider: Option<String>,
    project_root: Option<String>,
    resume_session_id: Option<String>,
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    use std::process::Stdio;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    // Refuse to start a second task if one is already running.
    {
        let guard = state.lock().await;
        if guard.pid.is_some() {
            return Err("A task is already running. Stop it before starting a new one.".to_string());
        }
    }

    let root = project_root
        .map(PathBuf::from)
        .unwrap_or_else(default_project_root);

    let python = find_python_interpreter(&root)?;

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "orchestrator.agent"])
        .arg("--task")
        .arg(&task)
        .current_dir(&root)
        // Tell the orchestrator and MCP server exactly where the kim repo lives.
        // Without this, mcp_session_context falls back to Path.cwd() which may
        // differ from the repo root when running inside the Tauri bundle.
        .env("PROJECT_ROOT", root.to_str().unwrap_or(""))
        // Ensure `import mcp_server.server` resolves from the repo root even when
        // the Python cwd is something unexpected.
        .env("PYTHONPATH", root.to_str().unwrap_or(""))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();
    if let Some(cfg) = &bridge_cfg {
        cmd.env("KIM_WEBVIEW_BRIDGE_URL", &cfg.base_url)
            .env("KIM_WEBVIEW_BRIDGE_TOKEN", &cfg.token)
            .env("KIM_WEBVIEW_WINDOW_LABEL", "kim-browser-signin");
    }

    // Default to the browser provider (no API key required) when the caller
    // omits one or passes an empty string. Never silently fall through to a
    // paid API key provider.
    let provider_arg = provider
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "browser".to_string());

    // For browser provider: ensure Chrome is running with the CDP debug port
    // before the Python agent tries to connect. This is a best-effort launch;
    // if Chrome is already running on :9222 this is a no-op.
    //
    // launch_chrome_for_cdp() uses blocking I/O (TcpStream::connect, fs::create_dir_all,
    // std::process::Command::spawn).  We run it on the blocking thread pool so we don't
    // stall the Tokio executor, then do the post-launch wait with tokio::time::sleep.
    if provider_arg == "browser" || provider_arg.starts_with("browser:") {
        if bridge_cfg.is_none() {
            let root_for_chrome = root.clone();
            match tokio::task::spawn_blocking(move || launch_chrome_for_cdp(&root_for_chrome)).await {
                Ok(Ok(true)) => {
                    // Chrome was freshly spawned — give it 2 s to open the debug port.
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                Ok(Ok(false)) => {
                    // Chrome was already running — no wait needed.
                }
                Ok(Err(e)) => {
                    // Non-fatal: the Python agent will surface a NEED_HELP if it can't connect.
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

    if let Some(resume_id) = resume_session_id
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        cmd.arg("--resume").arg(resume_id);
    }

    cmd.arg("--provider").arg(&provider_arg);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("Failed to start Kim: {}", e))?;

    // Record the PID so cancel_task can signal it.
    let child_pid = child.id();
    {
        let mut guard = state.lock().await;
        guard.pid = child_pid;
    }

    if let Some(stdout) = child.stdout.take() {
        let reader = tokio::io::BufReader::new(stdout);
        let app = app_handle.clone();
        tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-output", line);
            }
        });
    }

    if let Some(stderr) = child.stderr.take() {
        let reader = tokio::io::BufReader::new(stderr);
        let app = app_handle.clone();
        tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-error", line);
            }
        });
    }

    let status = child.wait().await.map_err(|e| e.to_string())?;

    // Clear the recorded PID regardless of exit reason (normal, error, cancelled).
    {
        let mut guard = state.lock().await;
        guard.pid = None;
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

#[tauri::command]
async fn cancel_task(
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    let pid = {
        let guard = state.lock().await;
        guard.pid
    };

    let Some(pid) = pid else {
        return Err("No task is currently running.".to_string());
    };

    // Step 1 — graceful signal.
    send_signal(pid, false)
        .map_err(|e| format!("Failed to send stop signal: {}", e))?;

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
                let mut guard = state_clone.lock().await;
                guard.pid = None;
                let _ = app.emit("kim-agent-cancelled", true);
                return;
            }
        }
        // Still alive after 2s → SIGKILL / taskkill /F.
        let _ = send_signal(pid, true);
        let mut guard = state_clone.lock().await;
        guard.pid = None;
        let _ = app.emit("kim-agent-cancelled", true);
    });

    Ok("Cancelling task…".to_string())
}

// ── Platform-specific signalling ─────────────────────────────────────────────

#[cfg(unix)]
fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    let sig = if force { "-KILL" } else { "-TERM" };
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
// Account — ~/.config/kim/account.json (platform-native config dir)
// ---------------------------------------------------------------------------

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
    output_path: String,
    sessions_dir: Option<String>,
) -> Result<String, String> {
    let base = sessions_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_sessions_dir);

    match format.as_str() {
        "zip" => export_as_zip(&base, &PathBuf::from(&output_path)),
        "json" => export_as_json(&base, &PathBuf::from(&output_path)),
        "markdown" => export_as_markdown(&base, &PathBuf::from(&output_path)),
        _ => Err(format!("Unknown format: {}. Use 'zip', 'json', or 'markdown'.", format)),
    }
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

    // Include account.json if present.
    let acct = account_path();
    if acct.exists() {
        if let Ok(data) = fs::read(&acct) {
            zip.start_file("account.json", opts).ok();
            let _ = zip.write_all(&data);
        }
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
    // Build a readable UTC timestamp without the chrono crate.
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);

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
    file_path: String,
    sessions_dir: Option<String>,
) -> Result<String, String> {
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
                if let Some(parent) = acct_path.parent() {
                    fs::create_dir_all(parent).ok();
                }
                fs::write(&acct_path, &buf).ok();
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
        let messages = session["messages"].as_array().cloned().unwrap_or_default();

        let dest = base.join(rel);
        if let Some(parent) = dest.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }

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

    let account_data = if account_path().exists() {
        fs::read_to_string(account_path()).unwrap_or_default()
    } else {
        "{}".to_string()
    };

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
        if let Ok(account) = serde_json::from_str::<KimAccount>(acct_content) {
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
    pub date: String,
    pub message_count: usize,
    pub summary: Option<String>,
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

fn read_project_sessions(dir: &Path) -> Vec<ClawSession> {
    let mut sessions = Vec::new();
    let Ok(entries) = fs::read_dir(dir) else { return sessions };

    let mut files: Vec<_> = entries
        .filter_map(|e| e.ok())
        .filter(|e| {
            let n = e.file_name();
            let s = n.to_string_lossy();
            s.ends_with(".jsonl") && !s.contains(".summary")
        })
        .collect();
    files.sort_by_key(|b| std::cmp::Reverse(b.file_name()));

    for fe in files.iter().take(50) {
        let session_id = fe.path()
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let message_count = count_lines(&fe.path()).unwrap_or(0);
        let summary_path = dir.join(format!("{}.summary.txt", session_id));
        let summary = if summary_path.exists() {
            fs::read_to_string(&summary_path)
                .ok()
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        } else {
            None
        };
        sessions.push(ClawSession {
            session_id,
            date: String::new(),
            message_count,
            summary,
        });
    }
    sessions
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
            if let Err(e) = start_webview_bridge_server(app.handle().clone()) {
                eprintln!("[Kim] Failed to start in-app browser bridge: {}", e);
            }
            Ok(())
        })
        .manage(task_state)
        .invoke_handler(tauri::generate_handler![
            list_sessions,
            load_session_messages,
            get_app_version,
            open_browser_signin_window,
            send_task,
            cancel_task,
            read_voice_config,
            write_voice_config,
            load_account,
            save_account,
            clear_account,
            verify_github_pat,
            export_data,
            import_data,
            backup_to_gist,
            restore_from_gist,
            list_claw_projects,
            add_code_project,
            remove_code_project,
            send_feedback,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
