//! Provider facade: shared types, the provider registry, and the top-level
//! `stream_kim_request` router. The individual streaming routes live in
//! submodules:
//! - [`bridge`] — desktop/browser providers via the Kim desktop bridge
//! - [`llm_stream`] — direct OpenAI-compatible + Anthropic SSE streaming
//! - [`sse`] — SSE line processors and the `<think>` stream parser
//! - [`codex_stream`] — Code mode (Codex subprocess / browser codex bridge)
//! - [`responses_proxy`] — Responses-API proxy so Codex can talk to ollama

use std::path::{Path, PathBuf};

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

mod bridge;
mod codex_stream;
mod llm_stream;
mod responses_proxy;
mod sse;
mod typed_events;

pub use bridge::bridge_token_source;

use bridge::{is_bridge_available, stream_via_bridge};
use codex_stream::stream_codex_subprocess;
use llm_stream::{stream_anthropic, stream_openai_compatible};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone)]
pub enum AppEvent {
    ThoughtChunk(String),
    ToolEvent {
        verb: String,
        target: String,
    },
    TextChunk(String),
    Done(bool),
    Err(String),
    /// Codex app-server (parity Part 4): native approval request. The REPL
    /// must answer via the codex decision channel or codex auto-declines on
    /// timeout. `risk == "network"` marks a managed-network escalation;
    /// `risk == "files"` a file-change (patch) approval.
    ApprovalRequest {
        id: String,
        command: String,
        cwd: String,
        reason: String,
        risk: String,
    },
    /// Live output chunk from a codex-executed command.
    CommandOutput(String),
    /// Turn plan checklist: (step text, status) pairs.
    PlanUpdate(Vec<(String, String)>),
    /// Whole-turn unified diff snapshot.
    DiffUpdate(String),
    /// Codex-side cumulative token usage.
    TokenUsage {
        input: u64,
        output: u64,
    },
    /// Turn lifecycle phase: started | completed | interrupted | failed.
    TurnPhase(String),
}

/// Control plumbing for one code-mode turn on the app-server transport
/// (parity Part 4): the REPL sends approval-decision JSON lines down
/// `decision_rx` → the bridge child's stdin, and `pid_slot` exposes the
/// child's pid so Ctrl-C can SIGTERM it (the service maps SIGTERM →
/// `turn/interrupt`) before escalating to a hard kill.
pub struct CodexTurnControl {
    pub decision_rx: tokio::sync::mpsc::UnboundedReceiver<String>,
    pub pid_slot: std::sync::Arc<std::sync::Mutex<Option<u32>>>,
}

/// F1/F10/F20: shared child-stderr pump. Drains a child's stderr concurrently
/// with the stdout stream into a bounded tail buffer (last ~8 KiB), so a
/// verbose child can never fill the OS pipe buffer, block in `write(2)`, and
/// hang the turn. The returned handle resolves to the captured tail once the
/// child closes stderr; callers await it only on failure paths.
pub(crate) fn drain_stderr_tail(
    stderr: tokio::process::ChildStderr,
) -> tokio::task::JoinHandle<String> {
    use tokio::io::AsyncReadExt;
    const MAX_TAIL_BYTES: usize = 8 * 1024;
    tokio::spawn(async move {
        let mut stderr = stderr;
        let mut buf = [0u8; 4096];
        let mut tail: Vec<u8> = Vec::new();
        loop {
            match stderr.read(&mut buf).await {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    tail.extend_from_slice(&buf[..n]);
                    if tail.len() > MAX_TAIL_BYTES {
                        let cut = tail.len() - MAX_TAIL_BYTES;
                        tail.drain(..cut);
                    }
                }
            }
        }
        String::from_utf8_lossy(&tail).trim().to_string()
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ImageAttachment {
    pub(crate) name: String,
    pub(crate) path: PathBuf,
    pub(crate) mime_type: &'static str,
    pub(crate) data_base64: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProviderInfo {
    pub name: &'static str,
    pub default_model: &'static str,
    pub key_env: Option<&'static str>,
    pub default_base_url: &'static str,
}

pub const PROVIDERS: &[ProviderInfo] = &[
    ProviderInfo {
        name: "ollama",
        default_model: "llama3.2",
        key_env: None,
        default_base_url: "http://127.0.0.1:11434/v1",
    },
    ProviderInfo {
        name: "openai",
        default_model: "gpt-4o-mini",
        key_env: Some("OPENAI_API_KEY"),
        default_base_url: "https://api.openai.com/v1",
    },
    ProviderInfo {
        name: "claude",
        default_model: "claude-sonnet-4-6",
        key_env: Some("ANTHROPIC_API_KEY"),
        default_base_url: "https://api.anthropic.com/v1",
    },
    ProviderInfo {
        name: "gemini",
        default_model: "gemini-2.0-flash",
        key_env: Some("GEMINI_API_KEY"),
        default_base_url: "https://generativelanguage.googleapis.com/v1beta/openai",
    },
    ProviderInfo {
        name: "deepseek",
        default_model: "deepseek-chat",
        key_env: Some("DEEPSEEK_API_KEY"),
        default_base_url: "https://api.deepseek.com/v1",
    },
    ProviderInfo {
        name: "desktop",
        default_model: "kim-bridge",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
    // Browser providers — keyless, routed through Kim desktop bridge.
    ProviderInfo {
        name: "browser",
        default_model: "browser-default",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
    ProviderInfo {
        name: "browser:claude",
        default_model: "browser-claude",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
    ProviderInfo {
        name: "browser:chatgpt",
        default_model: "browser-chatgpt",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
    ProviderInfo {
        name: "browser:gemini",
        default_model: "browser-gemini",
        key_env: None,
        default_base_url: "http://127.0.0.1:18991",
    },
];

/// Returns true for any browser-backed provider (`browser`, `browser:claude`, etc.).
/// These are keyless and require the Kim desktop app to be running as a bridge.
pub fn is_browser_provider(name: &str) -> bool {
    let n = name.trim();
    n.eq_ignore_ascii_case("browser") || n.to_ascii_lowercase().starts_with("browser:")
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct LocalAgentRoute {
    root: PathBuf,
    python: PathBuf,
    session_dir: PathBuf,
    resume_session_id: String,
}

fn build_local_agent_route(
    available: Option<(PathBuf, PathBuf)>,
    session_id: &str,
) -> Option<LocalAgentRoute> {
    available.map(|(root, python)| LocalAgentRoute {
        session_dir: root.join("kim_sessions"),
        root,
        python,
        resume_session_id: session_id.to_string(),
    })
}

/// Public wrapper over the internal bridge health probe so the REPL can decide,
/// for a chat-mode browser provider, whether to reuse a running desktop bridge
/// or fall through to the local Playwright agent. 400ms timeout per the probe.
pub async fn bridge_is_available(base_url: &str) -> bool {
    is_bridge_available(base_url).await
}

/// True if `dir` (or any ancestor) is inside a git working tree — the same
/// check Codex performs before `codex exec`. Walks up looking for a `.git`
/// marker (a directory in a normal clone, a file in a worktree/submodule).
pub fn is_git_repo(dir: &Path) -> bool {
    let mut cur = dir.to_path_buf();
    loop {
        if cur.join(".git").exists() {
            return true;
        }
        if !cur.pop() {
            return false;
        }
    }
}

pub fn provider_info(name: &str) -> Option<ProviderInfo> {
    PROVIDERS
        .iter()
        .copied()
        .find(|p| p.name == name.trim().to_ascii_lowercase())
}

/// Streaming entry point. Sends `AppEvent`s via `tx`; always terminates with
/// `Done` or `Err`. Never panics — all failures go through `tx`.
pub async fn stream_kim_request(
    config: &KimConfig,
    messages: &[ChatMessage],
    code_mode: bool,
    session_id: &str,
    allow_non_git: bool,
    tx: UnboundedSender<AppEvent>,
    codex_control: Option<CodexTurnControl>,
) {
    let prompt = messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .map(|m| m.content.as_str())
        .unwrap_or_default();
    if prompt.trim().is_empty() {
        let _ = tx.send(AppEvent::Err("Nothing to send.".to_string()));
        return;
    }

    // Code mode: run the Codex coding agent.
    // Browser providers intentionally stay on this path so Code mode can use
    // orchestrator.codex_bridge_service instead of the desktop chat bridge.
    if code_mode {
        stream_codex_subprocess(config, prompt, allow_non_git, session_id, tx, codex_control).await;
        return;
    }

    // Desktop bridge or browser provider in chat mode: forward to running Tauri app.
    // Browser providers pass the provider name (e.g. "browser:claude") to /v1/task so
    // the desktop app can route to the correct browser tab.
    if config.provider == "desktop" || is_browser_provider(&config.provider) {
        if is_bridge_available(&config.desktop_bridge_url).await {
            stream_via_bridge(config, messages, session_id, tx).await;
        } else if is_browser_provider(&config.provider) {
            // The REPL already probes bridge health while choosing between the
            // desktop and local-agent routes. The desktop can disappear in the
            // gap before this second probe (TOCTOU). Resolve the local route
            // again here so that race degrades to Playwright rather than an
            // incorrect "can't run" error.
            if let Some(route) = build_local_agent_route(
                crate::agentic::agentic_available(&config.provider),
                session_id,
            ) {
                let prompt = messages
                    .iter()
                    .rev()
                    .find(|m| m.role == "user")
                    .map(|m| m.content.as_str())
                    .unwrap_or_default();
                crate::agentic::stream_agentic_request(
                    &route.root,
                    &route.python,
                    prompt,
                    &config.provider,
                    &route.session_dir,
                    Some(&route.resume_session_id),
                    tx,
                )
                .await;
            } else {
                let _ = tx.send(AppEvent::Err(
                    "Browser provider can't run: no Python environment for the local browser agent, \
                     and the Kim desktop bridge isn't running.\n\
                     Fix (recommended): from the Kim repo run the installer to build the venv and \
                     Playwright browser — Windows: install.bat, macOS/Linux: ./install.sh — then rerun kim.\n\
                     Or: start the Kim desktop app, or switch with /provider ollama."
                        .to_string(),
                ));
            }
        } else {
            let _ = tx.send(AppEvent::Err(
                "Kim desktop bridge is not running. Start Kim desktop, or switch provider with /provider ollama."
                    .to_string(),
            ));
        }
        return;
    }

    // Chat mode: stream directly to the configured provider.
    // Prepend the system prompt (plus project KIM.md context, if present) then
    // call the appropriate streaming function. (A12)
    let mut system = KIM_CHAT_SYSTEM_PROMPT.to_string();
    if let Some(kim_md) = load_kim_md() {
        system.push_str("\n\n# Project context (from KIM.md)\n");
        system.push_str(&kim_md);
    }
    let mut full = vec![ChatMessage {
        role: "system".to_string(),
        content: system,
    }];
    full.extend_from_slice(messages);

    match config.provider.as_str() {
        "claude" => stream_anthropic(config, &full, tx).await,
        _ => stream_openai_compatible(config, &full, tx).await,
    }
}

/// Resolves the API key for a provider.  Precedence: non-empty environment
/// variable → stored config key.  A blank or whitespace-only env var is treated
/// as absent so that a stale shell export such as `OPENAI_API_KEY=""` cannot
/// shadow a key saved via /login — mirroring bridge_token()'s existing pattern.
pub(crate) fn resolve_api_key(env_val: Option<String>, stored: Option<String>) -> String {
    env_val
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .or_else(|| {
            stored
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
        })
        .unwrap_or_default()
}

/* ===========================================================
Message serializers
=========================================================== */

pub(crate) fn openai_compatible_messages(messages: &[ChatMessage]) -> Result<Vec<Value>, String> {
    messages
        .iter()
        .map(|m| {
            let attachments = image_attachments_from_prompt(&m.content)?;
            if attachments.is_empty() {
                return Ok(json!({ "role": m.role, "content": m.content }));
            }
            let mut content = vec![json!({ "type": "text", "text": m.content })];
            for a in attachments {
                content.push(json!({
                "type": "image_url",
                "image_url": { "url": format!("data:{};base64,{}", a.mime_type, a.data_base64) },
            }));
            }
            Ok(json!({ "role": m.role, "content": content }))
        })
        .collect()
}

pub(crate) fn anthropic_messages_ref(messages: &[&ChatMessage]) -> Result<Vec<Value>, String> {
    messages
        .iter()
        .map(|m| {
            let attachments = image_attachments_from_prompt(&m.content)?;
            if attachments.is_empty() {
                return Ok(json!({ "role": m.role, "content": m.content }));
            }
            let mut content = vec![json!({ "type": "text", "text": m.content })];
            for a in attachments {
                content.push(json!({
                "type": "image",
                "source": { "type": "base64", "media_type": a.mime_type, "data": a.data_base64 },
            }));
            }
            Ok(json!({ "role": m.role, "content": content }))
        })
        .collect()
}

/* ===========================================================
Image attachment helpers
=========================================================== */

pub(crate) fn image_attachments_from_prompt(prompt: &str) -> Result<Vec<ImageAttachment>, String> {
    let mut paths = prompt
        .lines()
        .filter_map(|line| line.trim().strip_prefix("- "))
        .filter_map(|line| {
            let path = PathBuf::from(line.trim());
            image_mime_type(&path).map(|mime| (path, mime))
        })
        .collect::<Vec<_>>();
    paths.sort_by(|l, r| l.0.cmp(&r.0));
    paths.dedup_by(|l, r| l.0 == r.0);
    paths
        .into_iter()
        .map(|(path, mime_type)| {
            let bytes = std::fs::read(&path)
                .map_err(|e| format!("Could not read image {}: {e}", path.display()))?;
            Ok(ImageAttachment {
                name: path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("image")
                    .to_string(),
                path,
                mime_type,
                data_base64: base64::engine::general_purpose::STANDARD.encode(bytes),
            })
        })
        .collect()
}

fn image_mime_type(path: &Path) -> Option<&'static str> {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => Some("image/png"),
        Some("jpg" | "jpeg") => Some("image/jpeg"),
        Some("webp") => Some("image/webp"),
        Some("gif") => Some("image/gif"),
        _ => None,
    }
}

/// Pure helper: wraps an `Option<PathBuf>` from root-discovery into a `Result`
/// with an actionable error message.  Kept pure (no globals) so it is testable
/// without touching the filesystem or environment.
pub(crate) fn kim_root_or_error(
    found: Option<std::path::PathBuf>,
) -> Result<std::path::PathBuf, String> {
    found.ok_or_else(|| {
        "Kim source root not found. \
Run install.sh to write ~/.kim_root, set KIM_PROJECT_ROOT, \
or run kim from inside the Kim repo directory."
            .to_string()
    })
}

/// Normalize a provider base URL to a bare origin (no trailing slash, no `/v1`
/// suffix). Callers append the endpoint they need (`/v1/chat/completions`,
/// `/api/tags`, …). Handles trailing slashes and a `/v1/` suffix correctly. (A16)
pub(crate) fn normalize_base_url(base_url: &str) -> String {
    let trimmed = base_url.trim().trim_end_matches('/');
    trimmed
        .strip_suffix("/v1")
        .unwrap_or(trimmed)
        .trim_end_matches('/')
        .to_string()
}

/// A12: load project context from the nearest KIM.md, walking up from cwd to the
/// repo root (a dir with `.git` or `orchestrator/agent.py`). Capped at ~4KB.
fn load_kim_md() -> Option<String> {
    load_kim_md_from(&std::env::current_dir().ok()?)
}

fn load_kim_md_from(start: &Path) -> Option<String> {
    const CAP: usize = 4096;
    let mut dir = start.to_path_buf();
    loop {
        let candidate = dir.join("KIM.md");
        if candidate.is_file() {
            let mut content = std::fs::read_to_string(&candidate).ok()?;
            if content.len() > CAP {
                // char-boundary-safe truncation
                let end = content
                    .char_indices()
                    .take_while(|(i, _)| *i < CAP)
                    .last()
                    .map(|(i, c)| i + c.len_utf8())
                    .unwrap_or(0);
                content.truncate(end);
                content.push_str("\n…(KIM.md truncated at 4KB)");
            }
            return Some(content);
        }
        // Stop at the repo root (having checked KIM.md there) or filesystem root.
        if dir.join(".git").exists() || dir.join("orchestrator").join("agent.py").exists() {
            break;
        }
        if !dir.pop() {
            break;
        }
    }
    None
}

const KIM_CHAT_SYSTEM_PROMPT: &str = "\
You are Kim, a personal AI assistant. Help with any task: questions, research, \
writing, coding, planning, or analysis. Be direct and conversational. \
Remember the conversation context and build on it. When you write code, format it clearly.";

/* ===========================================================
Tests
=========================================================== */

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_providers_are_in_providers_list() {
        for name in &[
            "browser",
            "browser:claude",
            "browser:chatgpt",
            "browser:gemini",
        ] {
            let info = provider_info(name).unwrap_or_else(|| panic!("missing provider: {name}"));
            assert!(info.key_env.is_none(), "{name} must be keyless");
        }
    }

    #[test]
    fn is_browser_provider_all_variants() {
        assert!(is_browser_provider("browser"));
        assert!(is_browser_provider("browser:claude"));
        assert!(is_browser_provider("browser:chatgpt"));
        assert!(is_browser_provider("browser:gemini"));
        assert!(is_browser_provider("BROWSER"));
        assert!(!is_browser_provider("claude"));
        assert!(!is_browser_provider("openai"));
        assert!(!is_browser_provider("desktop"));
    }

    #[test]
    fn local_browser_fallback_preserves_session_and_session_dir() {
        let root = PathBuf::from("C:/fake/kim");
        let python = root.join("venv/Scripts/python.exe");
        let route =
            build_local_agent_route(Some((root.clone(), python.clone())), "session-from-repl")
                .expect("available local agent should produce a fallback route");

        assert_eq!(route.root, root);
        assert_eq!(route.python, python);
        assert_eq!(route.session_dir, PathBuf::from("C:/fake/kim/kim_sessions"));
        assert_eq!(route.resume_session_id, "session-from-repl");
        assert!(build_local_agent_route(None, "session-from-repl").is_none());
    }

    // ── A12: KIM.md project context loading ─────────────────────────────────

    #[test]
    fn load_kim_md_reads_nearest_file_and_caps_size() {
        let tmp = std::env::temp_dir().join(format!(
            "kim-md-test-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        // .git marker so the walk stops here.
        std::fs::create_dir_all(tmp.join(".git")).unwrap();
        std::fs::write(tmp.join("KIM.md"), "# Project\nUse cargo test.").unwrap();
        let got = load_kim_md_from(&tmp).expect("should find KIM.md");
        assert!(got.contains("Use cargo test."));

        // Oversized KIM.md is truncated with a note.
        std::fs::write(tmp.join("KIM.md"), "x".repeat(9000)).unwrap();
        let big = load_kim_md_from(&tmp).unwrap();
        assert!(big.contains("truncated"));
        assert!(big.len() < 9000);

        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn load_kim_md_returns_none_when_absent() {
        let tmp = std::env::temp_dir().join(format!(
            "kim-md-none-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(tmp.join(".git")).unwrap();
        assert!(load_kim_md_from(&tmp).is_none());
        let _ = std::fs::remove_dir_all(&tmp);
    }

    // ── A16: base-URL normalization ─────────────────────────────────────────

    #[test]
    fn normalize_base_url_strips_v1_and_trailing_slash() {
        assert_eq!(normalize_base_url("http://host:11434"), "http://host:11434");
        assert_eq!(
            normalize_base_url("http://host:11434/"),
            "http://host:11434"
        );
        assert_eq!(
            normalize_base_url("http://host:11434/v1"),
            "http://host:11434"
        );
        // The A16 bug: a trailing slash after /v1 used to defeat the strip.
        assert_eq!(
            normalize_base_url("http://host:11434/v1/"),
            "http://host:11434"
        );
        assert_eq!(normalize_base_url("  http://host/v1/  "), "http://host");
    }

    // #45: the OpenAI model picker must drop non-chat model ids.
    #[test]
    fn openai_chat_model_filter_excludes_non_chat_ids() {
        use crate::commands::is_openai_chat_model_for_test as keep;
        assert!(keep("gpt-4o"));
        assert!(keep("gpt-4o-mini"));
        assert!(keep("o3"));
        assert!(keep("o1-mini"));
        assert!(!keep("gpt-3.5-turbo-instruct"));
        assert!(!keep("gpt-4o-audio-preview"));
        assert!(!keep("gpt-4o-realtime-preview"));
        assert!(!keep("gpt-image-1"));
        assert!(!keep("omni-moderation-latest"));
        assert!(!keep("text-embedding-3-large")); // not gpt/o prefixed anyway
    }

    // ── kim_root_or_error ──────────────────────────────────────────────────────

    #[test]
    fn kim_root_or_error_some_returns_ok() {
        let p = std::path::PathBuf::from("/tmp/kim-repo");
        assert_eq!(kim_root_or_error(Some(p.clone())), Ok(p));
    }

    #[test]
    fn kim_root_or_error_none_returns_actionable_message() {
        let err = kim_root_or_error(None).unwrap_err();
        // Message must mention each remediation path so the user knows what to do.
        assert!(
            err.contains("~/.kim_root"),
            "error should mention ~/.kim_root: {err}"
        );
        assert!(
            err.contains("KIM_PROJECT_ROOT"),
            "error should mention KIM_PROJECT_ROOT: {err}"
        );
        assert!(
            err.contains("Kim repo"),
            "error should mention the Kim repo: {err}"
        );
    }

    // ── resolve_api_key ───────────────────────────────────────────────────────

    #[test]
    fn resolve_api_key_nonempty_env_takes_precedence_over_stored() {
        let result = resolve_api_key(Some("env-key".to_string()), Some("stored-key".to_string()));
        assert_eq!(result, "env-key");
    }

    #[test]
    fn resolve_api_key_empty_env_falls_through_to_stored_key() {
        // ANTHROPIC_API_KEY="" or OPENAI_API_KEY="" in the shell must not
        // shadow a key the user saved via /login, creating an unbreakable loop
        // where login succeeds but every request fails.
        let result = resolve_api_key(Some(String::new()), Some("stored-key".to_string()));
        assert_eq!(result, "stored-key");
    }

    #[test]
    fn resolve_api_key_whitespace_env_falls_through_to_stored_key() {
        let result = resolve_api_key(Some("   ".to_string()), Some("stored-key".to_string()));
        assert_eq!(result, "stored-key");
    }

    #[test]
    fn resolve_api_key_trims_stored_key() {
        let result = resolve_api_key(None, Some("  stored-key\n".to_string()));
        assert_eq!(result, "stored-key");
    }

    #[test]
    fn resolve_api_key_blank_stored_key_is_absent() {
        let result = resolve_api_key(None, Some("   ".to_string()));
        assert!(result.is_empty());
    }

    #[test]
    fn resolve_api_key_both_absent_returns_empty_driving_login_error() {
        // Empty result causes the "Run /login <provider> first." error, which is correct.
        assert!(resolve_api_key(None, None).is_empty());
    }

    #[test]
    fn is_git_repo_walks_up_to_dot_git() {
        // .git in an ancestor → inside a repo, even from a nested subdir.
        let repo = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(repo.path().join(".git")).unwrap();
        let nested = repo.path().join("src").join("deep");
        std::fs::create_dir_all(&nested).unwrap();
        assert!(is_git_repo(&nested), "should find .git in an ancestor dir");

        // A fresh temp dir with no .git anywhere up the tree → not a repo.
        let bare = tempfile::tempdir().unwrap();
        assert!(!is_git_repo(bare.path()), "no .git ancestor → false");
    }
}
