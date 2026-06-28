use std::path::{Path, PathBuf};

/// Maximum output tokens requested from the Anthropic API (#24).
/// 8192 is the published output-token limit for Claude-3 and later models.
/// Formerly hardcoded inline as the slightly-wrong magic number 8096.
const ANTHROPIC_MAX_TOKENS: u32 = 8192;

use base64::Engine;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone)]
pub enum AppEvent {
    ThoughtChunk(String),
    ToolEvent { verb: String, target: String },
    TextChunk(String),
    Done(bool),
    Err(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ImageAttachment {
    name: String,
    path: PathBuf,
    mime_type: &'static str,
    data_base64: String,
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
    _session_id: &str,
    tx: UnboundedSender<AppEvent>,
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
        stream_codex_subprocess(config, prompt, tx).await;
        return;
    }

    // Desktop bridge or browser provider in chat mode: forward to running Tauri app.
    // Browser providers pass the provider name (e.g. "browser:claude") to /v1/task so
    // the desktop app can route to the correct browser tab.
    if config.provider == "desktop" || is_browser_provider(&config.provider) {
        if is_bridge_available(&config.desktop_bridge_url).await {
            stream_via_bridge(config, messages, tx).await;
        } else {
            let _ = tx.send(AppEvent::Err(if is_browser_provider(&config.provider) {
                "Kim desktop app is not running. Start Kim desktop to use browser providers, or switch with /provider ollama.".to_string()
            } else {
                "Kim desktop bridge is not running. Start Kim desktop, or switch provider with /provider ollama.".to_string()
            }));
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

/* ===========================================================
bridge (desktop provider)
=========================================================== */

async fn is_bridge_available(base_url: &str) -> bool {
    let base = base_url.trim_end_matches('/');
    let client = reqwest::Client::new();
    for path in ["/health", "/v1/health"] {
        let url = format!("{base}{path}");
        if client
            .get(&url)
            .timeout(std::time::Duration::from_millis(400))
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            return true;
        }
    }
    false
}

fn bridge_token() -> Option<String> {
    // D2: env first, then the file the desktop bridge writes on every start.
    if let Some(t) = std::env::var("KIM_API_KEY")
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
    {
        return Some(t);
    }
    bridge_token_from_file()
}

/// D2: human-readable description of where (if anywhere) a bridge token was
/// found, for `kim doctor`.
pub fn bridge_token_source() -> String {
    if std::env::var("KIM_API_KEY")
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .is_some()
    {
        "env KIM_API_KEY".to_string()
    } else if bridge_token_from_file().is_some() {
        "~/.kim/bridge_token (paired with desktop)".to_string()
    } else {
        "none — start Kim desktop, or set KIM_API_KEY".to_string()
    }
}

/// D2: read the local-loopback bridge token the desktop app persists to
/// `~/.kim/bridge_token`, so a `kim` install pairs with desktop automatically.
fn bridge_token_from_file() -> Option<String> {
    let path = dirs::home_dir()?.join(".kim").join("bridge_token");
    std::fs::read_to_string(path)
        .ok()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
}

/// Resolves the API key for a provider.  Precedence: non-empty environment
/// variable → stored config key.  A blank or whitespace-only env var is treated
/// as absent so that a stale shell export such as `OPENAI_API_KEY=""` cannot
/// shadow a key saved via /login — mirroring bridge_token()'s existing pattern.
fn resolve_api_key(env_val: Option<String>, stored: Option<String>) -> String {
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

async fn stream_via_bridge(
    config: &KimConfig,
    messages: &[ChatMessage],
    tx: UnboundedSender<AppEvent>,
) {
    let prompt = messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .map(|m| m.content.as_str())
        .unwrap_or_default();
    if prompt.trim().is_empty() {
        let _ = tx.send(AppEvent::Err(
            "Nothing to send to Kim desktop bridge.".to_string(),
        ));
        return;
    }
    let attachments = match image_attachments_from_prompt(prompt) {
        Ok(a) => a,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    };
    let client = reqwest::Client::new();
    let mut request = client
        .post(format!(
            "{}/v1/task",
            config.desktop_bridge_url.trim_end_matches('/')
        ))
        .json(&json!({
            "task": prompt,
            "provider": config.provider,
            "model": config.model,
            "attachments": attachments.iter().map(|a| json!({
                "name": a.name,
                "mime_type": a.mime_type,
                "data_base64": a.data_base64,
            })).collect::<Vec<_>>(),
        }));
    if let Some(token) = bridge_token() {
        request = request.header("X-Kim-Token", token);
    }
    // A13: /v1/task is non-streaming — it returns one blob after the whole run.
    // Emit a heartbeat every ~5s so the user isn't staring at silence.
    let response_future = async {
        let resp = request.send().await?;
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        Ok::<(reqwest::StatusCode, String), reqwest::Error>((status, body))
    };
    tokio::pin!(response_future);
    let mut heartbeat = tokio::time::interval(std::time::Duration::from_secs(5));
    heartbeat.tick().await; // consume the immediate first tick
    let result = loop {
        tokio::select! {
            r = &mut response_future => break r,
            _ = heartbeat.tick() => {
                let _ = tx.send(AppEvent::ThoughtChunk("Kim desktop is working…".to_string()));
            }
        }
    };
    match result {
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Desktop bridge request failed: {e}")));
        }
        Ok((status, body)) => {
            if status.is_success() {
                if let Ok(value) = serde_json::from_str::<Value>(&body) {
                    if let Some(response) = value.get("response").and_then(Value::as_str) {
                        let _ = tx.send(AppEvent::TextChunk(response.to_string()));
                    } else if let Some(session_id) = value.get("session_id").and_then(Value::as_str)
                    {
                        // /v1/task is async: it returns the session id immediately and runs
                        // the agent on the desktop, streaming output to the desktop UI. Poll
                        // the session's run_result so the CLI surfaces the actual answer
                        // instead of just "session started / (no response)".
                        let sessions_dir = value
                            .get("sessions_dir")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        match poll_bridge_session_answer(&sessions_dir, session_id, &tx).await {
                            Some((answer, _success)) if !answer.trim().is_empty() => {
                                let _ = tx.send(AppEvent::TextChunk(crate::markdown::render_markdown(
                                    answer.trim(),
                                )));
                            }
                            Some(_) => {
                                let _ = tx.send(AppEvent::Err(
                                    "Kim desktop finished but returned no answer.".to_string(),
                                ));
                            }
                            None => {
                                let _ = tx.send(AppEvent::Err(format!(
                                    "Kim desktop task timed out (session {session_id}); it may still be running in the desktop app."
                                )));
                            }
                        }
                    } else {
                        let _ = tx.send(AppEvent::TextChunk(body));
                    }
                } else {
                    let _ = tx.send(AppEvent::TextChunk(body));
                }
                let _ = tx.send(AppEvent::Done(true));
            } else {
                let _ = tx.send(AppEvent::Err(format!(
                    "Desktop bridge returned {status}: {body}"
                )));
            }
        }
    }
}

/// `/v1/task` runs the agent asynchronously on the desktop and only returns a
/// session id. Poll that session's JSONL file for the final `run_result` and return
/// `(summary, success)`. Emits a heartbeat every ~5s while waiting; gives up after 5min.
async fn poll_bridge_session_answer(
    sessions_dir: &str,
    session_id: &str,
    tx: &UnboundedSender<AppEvent>,
) -> Option<(String, bool)> {
    use std::time::{Duration, Instant};
    if sessions_dir.is_empty() {
        return None;
    }
    let deadline = Instant::now() + Duration::from_secs(300);
    let mut last_beat = Instant::now();
    loop {
        if let Some(path) = find_session_file(sessions_dir, session_id) {
            if let Some(res) = read_run_result(&path) {
                return Some(res);
            }
        }
        if Instant::now() >= deadline {
            return None;
        }
        if last_beat.elapsed() >= Duration::from_secs(5) {
            let _ = tx.send(AppEvent::ThoughtChunk("Kim desktop is working…".to_string()));
            last_beat = Instant::now();
        }
        tokio::time::sleep(Duration::from_millis(1200)).await;
    }
}

/// Locate `<sessions_dir>/<date>/<session_id>.jsonl` (date subdir varies).
fn find_session_file(sessions_dir: &str, session_id: &str) -> Option<PathBuf> {
    let base = Path::new(sessions_dir);
    let direct = base.join(format!("{session_id}.jsonl"));
    if direct.is_file() {
        return Some(direct);
    }
    for entry in std::fs::read_dir(base).ok()?.flatten() {
        let p = entry.path();
        if p.is_dir() {
            let f = p.join(format!("{session_id}.jsonl"));
            if f.is_file() {
                return Some(f);
            }
        }
    }
    None
}

/// Read the trailing `run_result` line; returns `(summary, success)` once present.
fn read_run_result(path: &Path) -> Option<(String, bool)> {
    let text = std::fs::read_to_string(path).ok()?;
    for line in text.lines().rev() {
        let line = line.trim();
        if line.is_empty() || !line.contains("\"run_result\"") {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            if v.get("type").and_then(Value::as_str) == Some("run_result") {
                let summary = v
                    .get("summary")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let success = v.get("success").and_then(Value::as_bool).unwrap_or(false);
                return Some((summary, success));
            }
        }
    }
    None
}

/* ===========================================================
streaming: OpenAI-compatible
=========================================================== */

async fn stream_openai_compatible(
    config: &KimConfig,
    messages: &[ChatMessage],
    tx: UnboundedSender<AppEvent>,
) {
    let provider = match provider_info(&config.provider) {
        Some(p) => p,
        None => {
            let _ = tx.send(AppEvent::Err(format!(
                "Unknown provider: {}",
                config.provider
            )));
            return;
        }
    };
    let base_url = if provider.name == "ollama" {
        format!("{}/v1", normalize_base_url(&config.ollama_base_url))
    } else {
        provider.default_base_url.to_string()
    };
    let raw = resolve_api_key(
        provider.key_env.and_then(|k| std::env::var(k).ok()),
        config.api_keys.get(provider.name).cloned(),
    );
    let api_key = if raw.is_empty() && provider.name == "ollama" {
        "ollama".to_string()
    } else {
        raw
    };
    if api_key.is_empty() {
        let _ = tx.send(AppEvent::Err(format!(
            "Run /login {} first.",
            provider.name
        )));
        return;
    }

    let request_messages = match openai_compatible_messages(messages) {
        Ok(m) => m,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    };

    let resp = reqwest::Client::new()
        .post(format!(
            "{}/chat/completions",
            base_url.trim_end_matches('/')
        ))
        .bearer_auth(api_key)
        .json(&json!({
            "model": config.model,
            "messages": request_messages,
            "stream": true,
        }))
        .send()
        .await;

    let resp = match resp {
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Provider request failed: {e}")));
            return;
        }
        Ok(r) => r,
    };
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        let _ = tx.send(AppEvent::Err(format!("Provider returned {status}: {body}")));
        return;
    }

    let mut stream = resp.bytes_stream();
    let mut line_buf = String::new();
    let mut parser = ThinkParser::new();

    while let Some(chunk) = stream.next().await {
        let bytes = match chunk {
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Stream error: {e}")));
                return;
            }
            Ok(b) => b,
        };
        line_buf.push_str(&String::from_utf8_lossy(&bytes));
        loop {
            match line_buf.find('\n') {
                None => break,
                Some(pos) => {
                    let line = line_buf[..pos].trim().to_string();
                    line_buf = line_buf[pos + 1..].to_string();
                    process_openai_sse_line(&line, &mut parser, &tx);
                }
            }
        }
    }
    if !line_buf.trim().is_empty() {
        process_openai_sse_line(line_buf.trim(), &mut parser, &tx);
    }
    parser.flush(&tx);
    let _ = tx.send(AppEvent::Done(false));
}

/* ===========================================================
streaming: Anthropic
=========================================================== */

async fn stream_anthropic(
    config: &KimConfig,
    messages: &[ChatMessage],
    tx: UnboundedSender<AppEvent>,
) {
    let api_key = resolve_api_key(
        std::env::var("ANTHROPIC_API_KEY").ok(),
        config.api_keys.get("claude").cloned(),
    );
    if api_key.is_empty() {
        let _ = tx.send(AppEvent::Err("Run /login claude first.".to_string()));
        return;
    }

    let system = messages
        .iter()
        .find(|m| m.role == "system")
        .map(|m| m.content.as_str())
        .unwrap_or("");
    let conv: Vec<&ChatMessage> = messages.iter().filter(|m| m.role != "system").collect();
    let request_messages = match anthropic_messages_ref(&conv) {
        Ok(m) => m,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    };

    let mut body = json!({
        "model": config.model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "stream": true,
        "messages": request_messages,
    });
    if !system.is_empty() {
        body["system"] = json!(system);
    }

    let resp = reqwest::Client::new()
        .post("https://api.anthropic.com/v1/messages")
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&body)
        .send()
        .await;

    let resp = match resp {
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Claude request failed: {e}")));
            return;
        }
        Ok(r) => r,
    };
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        let _ = tx.send(AppEvent::Err(format!("Claude returned {status}: {body}")));
        return;
    }

    let mut stream = resp.bytes_stream();
    let mut line_buf = String::new();
    let mut parser = ThinkParser::new();

    while let Some(chunk) = stream.next().await {
        let bytes = match chunk {
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Claude stream error: {e}")));
                return;
            }
            Ok(b) => b,
        };
        line_buf.push_str(&String::from_utf8_lossy(&bytes));
        loop {
            match line_buf.find('\n') {
                None => break,
                Some(pos) => {
                    let line = line_buf[..pos].trim().to_string();
                    line_buf = line_buf[pos + 1..].to_string();
                    process_anthropic_sse_line(&line, &mut parser, &tx);
                }
            }
        }
    }
    if !line_buf.trim().is_empty() {
        process_anthropic_sse_line(line_buf.trim(), &mut parser, &tx);
    }
    parser.flush(&tx);
    let _ = tx.send(AppEvent::Done(false));
}

/* ===========================================================
SSE line processors
=========================================================== */

fn sse_data_payload(line: &str) -> Option<&str> {
    line.strip_prefix("data:").map(str::trim_start)
}

/// Render an in-stream error payload into a user-facing message. Handles both
/// an object with a `message` field and a bare string error. (A3)
fn format_stream_error(err: &Value) -> String {
    let detail = err
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| err.as_str().map(str::to_string))
        .unwrap_or_else(|| err.to_string());
    format!("Provider error: {detail}")
}

fn process_openai_sse_line(line: &str, parser: &mut ThinkParser, tx: &UnboundedSender<AppEvent>) {
    let Some(data) = sse_data_payload(line) else {
        return;
    };
    if data == "[DONE]" || data.is_empty() {
        return;
    }
    let Ok(json) = serde_json::from_str::<Value>(data) else {
        return;
    };
    // In-stream provider errors (ollama/openai emit `{"error": ...}` mid-stream,
    // e.g. model-not-found) must surface, not be silently dropped. (A3)
    if let Some(err) = json.get("error") {
        let _ = tx.send(AppEvent::Err(format_stream_error(err)));
        return;
    }
    let Some(choices) = json.get("choices").and_then(Value::as_array) else {
        return;
    };
    for choice in choices {
        let delta = &choice["delta"];
        for key in ["reasoning_content", "reasoning", "thinking"] {
            if let Some(text) = delta.get(key).and_then(Value::as_str) {
                if !text.is_empty() {
                    let _ = tx.send(AppEvent::ThoughtChunk(text.to_string()));
                }
            }
        }
        if let Some(content) = delta.get("content").and_then(Value::as_str) {
            if !content.is_empty() {
                parser.feed(content, tx);
            }
        }
        if let Some(tool_calls) = delta.get("tool_calls").and_then(Value::as_array) {
            for call in tool_calls {
                let name = call
                    .get("function")
                    .and_then(|f| f.get("name"))
                    .and_then(Value::as_str)
                    .or_else(|| call.get("name").and_then(Value::as_str))
                    .unwrap_or_default();
                if !name.is_empty() {
                    let _ = tx.send(AppEvent::ToolEvent {
                        verb: "Tool".to_string(),
                        target: name.to_string(),
                    });
                }
            }
        }
    }
}

fn process_anthropic_sse_line(
    line: &str,
    parser: &mut ThinkParser,
    tx: &UnboundedSender<AppEvent>,
) {
    let Some(data) = sse_data_payload(line) else {
        return;
    };
    if data == "[DONE]" || data.is_empty() {
        return;
    }
    let Ok(json) = serde_json::from_str::<Value>(data) else {
        return;
    };
    match json.get("type").and_then(Value::as_str) {
        Some("content_block_start") => {
            let block = &json["content_block"];
            if block.get("type").and_then(Value::as_str) == Some("tool_use") {
                if let Some(name) = block.get("name").and_then(Value::as_str) {
                    let _ = tx.send(AppEvent::ToolEvent {
                        verb: "Tool".to_string(),
                        target: name.to_string(),
                    });
                }
            }
        }
        Some("content_block_delta") => {
            let delta = &json["delta"];
            match delta.get("type").and_then(Value::as_str) {
                Some("text_delta") => {
                    if let Some(text) = delta.get("text").and_then(Value::as_str) {
                        if !text.is_empty() {
                            parser.feed(text, tx);
                        }
                    }
                }
                Some("thinking_delta") => {
                    if let Some(text) = delta.get("thinking").and_then(Value::as_str) {
                        if !text.is_empty() {
                            let _ = tx.send(AppEvent::ThoughtChunk(text.to_string()));
                        }
                    }
                }
                Some("input_json_delta") => {
                    if let Some(partial) = delta.get("partial_json").and_then(Value::as_str) {
                        if !partial.trim().is_empty() {
                            let _ =
                                tx.send(AppEvent::ThoughtChunk(format!("tool input {partial}")));
                        }
                    }
                }
                _ => {}
            }
        }
        // Anthropic streams `{"type":"error","error":{...}}` events; surface
        // them instead of dropping the stream silently. (A3)
        Some("error") => {
            let err = json.get("error").unwrap_or(&json);
            let _ = tx.send(AppEvent::Err(format_stream_error(err)));
        }
        _ => {}
    }
}

/* ===========================================================
<think> / </think> tag parser
=========================================================== */

/// gpt-oss harmony channel-boundary token fused into delta.content. (A4)
const ASSISTANT_FINAL: &str = "assistantfinal";

enum ThinkState {
    Normal,
    InThink,
}

struct ThinkParser {
    state: ThinkState,
    buf: String,
}

impl ThinkParser {
    fn new() -> Self {
        Self {
            state: ThinkState::Normal,
            buf: String::new(),
        }
    }

    fn feed(&mut self, chunk: &str, tx: &UnboundedSender<AppEvent>) {
        self.buf.push_str(chunk);
        loop {
            match self.state {
                ThinkState::Normal => {
                    let think_pos = self.buf.find("<think>");
                    // gpt-oss "harmony" streams chain-of-thought in delta.content
                    // ending with the fused token `assistantfinal`, then the
                    // answer. Treat it as a channel boundary: text before → thought,
                    // marker swallowed, text after → answer. (A4)
                    let final_pos = self.buf.find(ASSISTANT_FINAL);
                    let use_think = match (think_pos, final_pos) {
                        (Some(tp), Some(fp)) => tp <= fp,
                        (Some(_), None) => true,
                        _ => false,
                    };
                    if use_think {
                        let pos = think_pos.expect("think_pos present");
                        let before = self.buf[..pos].to_string();
                        if !before.is_empty() {
                            let _ = tx.send(AppEvent::TextChunk(before));
                        }
                        self.buf = self.buf[pos + 7..].to_string();
                        self.state = ThinkState::InThink;
                    } else if let Some(pos) = final_pos {
                        let before = self.buf[..pos].to_string();
                        if !before.is_empty() {
                            let _ = tx.send(AppEvent::ThoughtChunk(before));
                        }
                        self.buf = self.buf[pos + ASSISTANT_FINAL.len()..].to_string();
                        // stay in Normal — what follows the marker is the answer.
                    } else {
                        // Hold back enough of the tail (>= marker length) so
                        // `assistantfinal` can't be split across two flushes.
                        let flush_up_to = split_before_tail_chars(&self.buf, 14);
                        if flush_up_to > 0 {
                            let to_flush = self.buf[..flush_up_to].to_string();
                            let _ = tx.send(AppEvent::TextChunk(to_flush));
                            self.buf = self.buf[flush_up_to..].to_string();
                        }
                        break;
                    }
                }
                ThinkState::InThink => {
                    if let Some(pos) = self.buf.find("</think>") {
                        let thought = self.buf[..pos].to_string();
                        if !thought.is_empty() {
                            let _ = tx.send(AppEvent::ThoughtChunk(thought));
                        }
                        self.buf = self.buf[pos + 8..].to_string();
                        self.state = ThinkState::Normal;
                    } else {
                        let flush_up_to = split_before_tail_chars(&self.buf, 7);
                        if flush_up_to > 0 {
                            let to_flush = self.buf[..flush_up_to].to_string();
                            let _ = tx.send(AppEvent::ThoughtChunk(to_flush));
                            self.buf = self.buf[flush_up_to..].to_string();
                        }
                        break;
                    }
                }
            }
        }
    }

    fn flush(&mut self, tx: &UnboundedSender<AppEvent>) {
        if self.buf.is_empty() {
            return;
        }
        let text = std::mem::take(&mut self.buf);
        let _ = match self.state {
            ThinkState::Normal => tx.send(AppEvent::TextChunk(text)),
            ThinkState::InThink => tx.send(AppEvent::ThoughtChunk(text)),
        };
    }
}

fn split_before_tail_chars(text: &str, keep: usize) -> usize {
    text.char_indices()
        .rev()
        .nth(keep.saturating_sub(1))
        .map_or(0, |(i, _)| i)
}

/* ===========================================================
Codex subprocess (Code mode)
=========================================================== */

async fn stream_codex_subprocess(config: &KimConfig, prompt: &str, tx: UnboundedSender<AppEvent>) {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let is_browser = config.provider.to_ascii_lowercase().starts_with("browser");

    // Holds the exclusive temp dir for the codex CODEX_HOME so it outlives the
    // if-else block and stays alive until child.wait() completes (#23).
    let mut _codex_temp_dir: Option<tempfile::TempDir> = None;

    let mut child = if is_browser {
        // Browser provider: launch the Kim codex bridge service.
        // Resolve the Kim source root so `python3 -m orchestrator.codex_bridge_service`
        // works from any user cwd. Mirrors the desktop subprocess environment.
        let kim_root = match kim_root_or_error(crate::sessions::find_kim_repo_root()) {
            Ok(r) => r,
            Err(msg) => {
                let _ = tx.send(AppEvent::Err(msg));
                return;
            }
        };
        let python = match crate::agentic::find_python(&kim_root) {
            Some(p) => p,
            None => {
                let _ = tx.send(AppEvent::Err(
                    "No Python interpreter found (tried venv, python3, python). \
                     Install Python 3 and retry.".to_string(),
                ));
                return;
            }
        };
        match Command::new(&python)
            .args([
                "-m",
                "orchestrator.codex_bridge_service",
                "--task",
                prompt,
                "--cwd",
                &cwd.to_string_lossy(),
                "--provider",
                &config.provider,
            ])
            .current_dir(&kim_root)
            .env("PYTHONPATH", &kim_root)
            .env("PROJECT_ROOT", &kim_root)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Failed to start codex bridge: {e}")));
                return;
            }
        }
    } else {
        // Local provider: start a Responses-API→Chat-Completions proxy so codex
        // can talk to ollama (which only speaks Chat Completions).
        let proxy_port = match start_responses_proxy(config, &tx).await {
            Some(p) => p,
            None => return,
        };
        // Use an exclusive randomized temp dir so concurrent runs don't clobber
        // each other and the path is not pre-creatable by a local attacker (#23).
        let kim_codex_dir = match tempfile::Builder::new()
            .prefix("kim_codex_")
            .tempdir()
        {
            Ok(d) => d,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!(
                    "Failed to create codex temp dir: {e}"
                )));
                return;
            }
        };
        let kim_codex_home = kim_codex_dir.path().to_path_buf();
        // Keep the TempDir alive until child.wait() finishes (#23).
        _codex_temp_dir = Some(kim_codex_dir);
        if let Err(e) = write_codex_config(proxy_port, &config.model, &kim_codex_home) {
            let _ = tx.send(AppEvent::Err(format!("Failed to write codex config: {e}")));
            return;
        }
        // Gate the sandbox-bypass flag behind an explicit opt-in env var (#1).
        // Passing it unconditionally disabled the Codex approval gate for every
        // CLI user, even those who didn't need it.
        let bypass_sandbox =
            std::env::var("KIM_CODEX_BYPASS_SANDBOX").as_deref() == Ok("1");
        let cwd_str = cwd.to_string_lossy().into_owned();
        let mut codex_args: Vec<String> = vec!["exec".into(), "--json".into()];
        if bypass_sandbox {
            codex_args.push("--dangerously-bypass-approvals-and-sandbox".into());
        }
        codex_args.push("-C".into());
        codex_args.push(cwd_str);
        codex_args.push(prompt.to_string());
        match Command::new("codex")
            .args(&codex_args)
            .env("OPENAI_API_KEY", "ollama")
            .env("CODEX_HOME", &kim_codex_home)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!(
                    "Failed to start codex: {e}. Install with: npm install -g @openai/codex"
                )));
                return;
            }
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err("Failed to capture codex stdout.".to_string()));
            return;
        }
    };
    let stderr_pipe = child.stderr.take();
    let mut lines = BufReader::new(stdout).lines();
    let mut had_output = false;

    while let Ok(Some(line)) = lines.next_line().await {
        had_output = true;
        process_codex_line(&line, &tx, is_browser);
    }

    let exit_ok = child.wait().await.map(|s| s.success()).unwrap_or(false);
    if !had_output || !exit_ok {
        let mut stderr_msg = String::new();
        if let Some(pipe) = stderr_pipe {
            let mut err_lines = BufReader::new(pipe).lines();
            while let Ok(Some(line)) = err_lines.next_line().await {
                if !stderr_msg.is_empty() {
                    stderr_msg.push('\n');
                }
                stderr_msg.push_str(line.trim());
            }
        }
        if !stderr_msg.trim().is_empty() {
            let _ = tx.send(AppEvent::Err(format!("codex: {}", stderr_msg.trim())));
        } else if !had_output {
            let _ = tx.send(AppEvent::Err(
                "codex produced no output. Check that ollama is running and the model name is correct.".to_string(),
            ));
        }
        return;
    }
    // used_bridge only when codex ran against a browser provider via the Kim bridge
    // service; local codex (ollama) is not "via Kim desktop".
    let _ = tx.send(AppEvent::Done(is_browser));
}

fn process_codex_line(line: &str, tx: &UnboundedSender<AppEvent>, is_bridge: bool) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    if is_bridge {
        // New typed JSON format from codex_bridge_service.
        if let Ok(json) = serde_json::from_str::<Value>(line) {
            match json.get("type").and_then(Value::as_str) {
                Some("status") => {
                    if let Some(msg) = json.get("message").and_then(Value::as_str) {
                        let _ = tx.send(AppEvent::ThoughtChunk(msg.to_string()));
                    }
                }
                _ => {
                    let _ = tx.send(AppEvent::TextChunk(format!("{line}\n")));
                }
            }
            return;
        }
        // Legacy bracket prefix format.
        if let Some(rest) = line.strip_prefix("[STATUS] ") {
            let _ = tx.send(AppEvent::ThoughtChunk(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("[SUCCESS] ") {
            let _ = tx.send(AppEvent::TextChunk(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("[FAILED] ") {
            let _ = tx.send(AppEvent::Err(rest.to_string()));
        } else {
            let _ = tx.send(AppEvent::TextChunk(format!("{line}\n")));
        }
        return;
    }
    // Codex JSON-stream format.
    let Ok(json) = serde_json::from_str::<Value>(line) else {
        let _ = tx.send(AppEvent::TextChunk(format!("{line}\n")));
        return;
    };
    match json.get("type").and_then(Value::as_str) {
        Some("message") => {
            if let Some(blocks) = json.get("content").and_then(Value::as_array) {
                for block in blocks {
                    if block.get("type").and_then(Value::as_str) == Some("text") {
                        if let Some(text) = block.get("text").and_then(Value::as_str) {
                            if !text.is_empty() {
                                let _ = tx.send(AppEvent::TextChunk(text.to_string()));
                            }
                        }
                    }
                }
            }
        }
        Some("reasoning") => {
            let text = json
                .get("summary")
                .and_then(Value::as_array)
                .and_then(|a| a.first())
                .and_then(|v| v.get("text"))
                .and_then(Value::as_str)
                .or_else(|| json.get("text").and_then(Value::as_str))
                .unwrap_or_default();
            if !text.is_empty() {
                let _ = tx.send(AppEvent::ThoughtChunk(text.to_string()));
            }
        }
        Some("function_call") => {
            let name = json.get("name").and_then(Value::as_str).unwrap_or("tool");
            let _ = tx.send(AppEvent::ToolEvent {
                verb: "Running".to_string(),
                target: name.to_string(),
            });
        }
        Some("function_call_output") => {
            if let Some(output) = json.get("output").and_then(Value::as_str) {
                let trimmed = output.trim();
                if !trimmed.is_empty() {
                    // char-boundary-safe truncation — byte slicing panics mid-UTF-8 (A5)
                    let display = crate::sessions::truncate(trimmed, 300);
                    let _ = tx.send(AppEvent::ThoughtChunk(display));
                }
            }
        }
        Some("item.completed") => {
            if let Some(item) = json.get("item") {
                match item.get("type").and_then(Value::as_str) {
                    Some("agent_message") => {
                        if let Some(text) = item.get("text").and_then(Value::as_str) {
                            if !text.is_empty() {
                                let _ = tx.send(AppEvent::TextChunk(text.to_string()));
                            }
                        }
                    }
                    Some("function_call") => {
                        let name = item.get("name").and_then(Value::as_str).unwrap_or("tool");
                        let _ = tx.send(AppEvent::ToolEvent {
                            verb: "Running".to_string(),
                            target: name.to_string(),
                        });
                    }
                    Some("function_call_output") => {
                        if let Some(output) = item.get("output").and_then(Value::as_str) {
                            let trimmed = output.trim();
                            if !trimmed.is_empty() {
                                // char-boundary-safe truncation (A5)
                                let display = crate::sessions::truncate(trimmed, 300);
                                let _ = tx.send(AppEvent::ThoughtChunk(display));
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        Some("error") => {
            let msg = json
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("codex error");
            if !msg.contains("Reconnecting") && !msg.contains("stream disconnected") {
                let _ = tx.send(AppEvent::Err(msg.to_string()));
            }
        }
        _ => {}
    }
}

/* ===========================================================
Responses-API proxy for Codex + Ollama
=========================================================== */

const RESPONSES_PROXY_PY: &str = include_str!("responses_proxy.py");

async fn start_responses_proxy(config: &KimConfig, tx: &UnboundedSender<AppEvent>) -> Option<u16> {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    let tmp_path = std::env::temp_dir().join("kim_responses_proxy.py");
    if let Err(e) = std::fs::write(&tmp_path, RESPONSES_PROXY_PY) {
        let _ = tx.send(AppEvent::Err(format!("Failed to write proxy script: {e}")));
        return None;
    }

    // Resolve a Python interpreter the same way agentic.rs does: venv first,
    // then system python3/python — avoids hardcoding "python3" which is absent
    // on Windows.
    let python = {
        let root_opt = crate::sessions::find_kim_repo_root();
        let root = root_opt.unwrap_or_else(|| std::path::PathBuf::from("."));
        crate::agentic::find_python(&root).unwrap_or_else(|| std::path::PathBuf::from("python3"))
    };
    let ollama_base = format!("{}/v1", normalize_base_url(&config.ollama_base_url));
    let mut child = match Command::new(&python)
        .args([tmp_path.to_string_lossy().as_ref(), ollama_base.as_str()])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!(
                "Failed to start responses proxy (Python interpreter not found): {e}"
            )));
            return None;
        }
    };

    let stdout = child.stdout.take()?;
    let mut lines = BufReader::new(stdout).lines();
    let port_line =
        match tokio::time::timeout(std::time::Duration::from_secs(5), lines.next_line()).await {
            Ok(Ok(Some(line))) => line,
            _ => {
                let _ = tx.send(AppEvent::Err(
                    "Responses proxy did not start in time.".to_string(),
                ));
                return None;
            }
        };

    let port: u16 = match port_line.trim().parse() {
        Ok(p) => p,
        Err(_) => {
            let _ = tx.send(AppEvent::Err(format!(
                "Responses proxy returned unexpected output: {port_line}"
            )));
            return None;
        }
    };

    tokio::spawn(async move {
        let _ = child.wait().await;
    });
    Some(port)
}

fn write_codex_config(proxy_port: u16, model: &str, codex_home: &std::path::Path) -> Result<(), String> {
    std::fs::create_dir_all(codex_home)
        .map_err(|e| format!("Cannot create kim codex home {}: {e}", codex_home.display()))?;
    let config_path = codex_home.join("config.toml");
    let content = format!(
        "model = \"{model}\"\n\
         model_provider = \"kim-proxy\"\n\
         \n\
         [model_providers.kim-proxy]\n\
         name = \"Kim Proxy\"\n\
         base_url = \"http://127.0.0.1:{proxy_port}/v1\"\n\
         wire_api = \"responses\"\n\
         env_key = \"OPENAI_API_KEY\"\n"
    );
    std::fs::write(&config_path, content).map_err(|e| {
        format!(
            "Cannot write codex config to {}: {e}",
            config_path.display()
        )
    })
}

/* ===========================================================
Message serializers
=========================================================== */

fn openai_compatible_messages(messages: &[ChatMessage]) -> Result<Vec<Value>, String> {
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

fn anthropic_messages_ref(messages: &[&ChatMessage]) -> Result<Vec<Value>, String> {
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

fn image_attachments_from_prompt(prompt: &str) -> Result<Vec<ImageAttachment>, String> {
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
fn kim_root_or_error(found: Option<std::path::PathBuf>) -> Result<std::path::PathBuf, String> {
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
    fn openai_sse_routes_reasoning_tools_and_text() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        process_openai_sse_line(
            r#"data: {"choices":[{"delta":{"reasoning_content":"checking files","content":"hello","tool_calls":[{"function":{"name":"read_file"}}]}}]}"#,
            &mut parser,
            &tx,
        );
        parser.flush(&tx);
        let mut events = Vec::new();
        while let Ok(e) = rx.try_recv() {
            events.push(e);
        }
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t == "checking files")));
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::ToolEvent { target, .. } if target == "read_file")));
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::TextChunk(t) if t == "hello")));
    }

    #[test]
    fn anthropic_sse_routes_thinking_tools_and_text() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        process_anthropic_sse_line(
            r#"data: {"type":"content_block_start","content_block":{"type":"tool_use","name":"bash"}}"#,
            &mut parser,
            &tx,
        );
        process_anthropic_sse_line(
            r#"data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"checking state"}}"#,
            &mut parser,
            &tx,
        );
        process_anthropic_sse_line(
            r#"data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"done"}}"#,
            &mut parser,
            &tx,
        );
        parser.flush(&tx);
        let mut events = Vec::new();
        while let Ok(e) = rx.try_recv() {
            events.push(e);
        }
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::ToolEvent { target, .. } if target == "bash")));
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t == "checking state")));
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::TextChunk(t) if t == "done")));
    }

    fn drain(rx: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>) -> Vec<AppEvent> {
        let mut events = Vec::new();
        while let Ok(e) = rx.try_recv() {
            events.push(e);
        }
        events
    }

    // ── A3: in-stream provider errors surface as AppEvent::Err ──────────────

    #[test]
    fn openai_sse_surfaces_error_object() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        // ollama emits this when the requested model isn't pulled.
        process_openai_sse_line(
            r#"data: {"error":{"message":"model 'gpt-oss:20b-cloud' not found"}}"#,
            &mut parser,
            &tx,
        );
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::Err(m) if m.contains("not found"))),
            "expected an Err event, got {events:?}"
        );
    }

    #[test]
    fn openai_sse_surfaces_bare_string_error() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        process_openai_sse_line(r#"data: {"error":"upstream timed out"}"#, &mut parser, &tx);
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::Err(m) if m.contains("upstream timed out"))),
            "expected an Err event, got {events:?}"
        );
    }

    #[test]
    fn anthropic_sse_surfaces_error_event() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        process_anthropic_sse_line(
            r#"data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}"#,
            &mut parser,
            &tx,
        );
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::Err(m) if m.contains("Overloaded"))),
            "expected an Err event, got {events:?}"
        );
    }

    // ── A5: char-boundary-safe truncation of tool output ────────────────────

    #[test]
    fn function_call_output_truncation_is_char_safe() {
        // Byte 300 lands mid-emoji; byte slicing `&trimmed[..300]` would panic.
        let mut payload = "a".repeat(299);
        payload.push('🦀'); // 4-byte char straddling the 300-byte boundary
        payload.push_str(&"b".repeat(50));
        let line =
            serde_json::json!({"type": "function_call_output", "output": payload}).to_string();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        // Must not panic.
        process_codex_line(&line, &tx, false);
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t.ends_with('…'))),
            "expected a truncated ThoughtChunk, got {events:?}"
        );
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

    // ── A4: assistantfinal harmony boundary ─────────────────────────────────

    #[test]
    fn assistantfinal_splits_thought_and_answer_in_one_feed() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        parser.feed("reasoning here assistantfinalThe answer", &tx);
        parser.flush(&tx);
        let events = drain(&mut rx);
        let thoughts: String = events
            .iter()
            .filter_map(|e| match e {
                AppEvent::ThoughtChunk(t) => Some(t.clone()),
                _ => None,
            })
            .collect();
        let answer: String = events
            .iter()
            .filter_map(|e| match e {
                AppEvent::TextChunk(t) => Some(t.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(thoughts, "reasoning here ");
        assert_eq!(answer, "The answer");
        assert!(!thoughts.contains("assistantfinal") && !answer.contains("assistantfinal"));
    }

    #[test]
    fn assistantfinal_marker_split_across_two_feeds() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        // Each feed is <= the 14-char tail-hold so nothing flushes prematurely.
        parser.feed("think ", &tx);
        parser.feed("assistantfinalDONE", &tx);
        parser.flush(&tx);
        let events = drain(&mut rx);
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t == "think ")));
        assert!(events
            .iter()
            .any(|e| matches!(e, AppEvent::TextChunk(t) if t == "DONE")));
        assert!(events.iter().all(|e| match e {
            AppEvent::TextChunk(t) | AppEvent::ThoughtChunk(t) => !t.contains("assistantfinal"),
            _ => true,
        }));
    }

    #[test]
    fn no_assistantfinal_marker_is_plain_text() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        parser.feed("plain answer text", &tx);
        parser.flush(&tx);
        let events = drain(&mut rx);
        let answer: String = events
            .iter()
            .filter_map(|e| match e {
                AppEvent::TextChunk(t) => Some(t.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(answer, "plain answer text");
        assert!(!events
            .iter()
            .any(|e| matches!(e, AppEvent::ThoughtChunk(_))));
    }

    #[test]
    fn think_parser_keeps_utf8_boundaries_when_flushing_tail() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        parser.feed("for—news", &tx);
        parser.flush(&tx);
        let mut text = String::new();
        while let Ok(e) = rx.try_recv() {
            if let AppEvent::TextChunk(chunk) = e {
                text.push_str(&chunk);
            }
        }
        assert_eq!(text, "for—news");
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
}
