use std::path::{Path, PathBuf};

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
];

pub fn provider_info(name: &str) -> Option<ProviderInfo> {
    PROVIDERS
        .iter()
        .copied()
        .find(|provider| provider.name == name.trim().to_ascii_lowercase())
}

/// Streaming entry point. Sends `AppEvent`s via `tx`; always terminates with
/// `Done` or `Err`. Never panics or returns an error — all failures go through `tx`.
pub async fn stream_kim_request(
    config: &KimConfig,
    messages: &[ChatMessage],
    code_mode: bool,
    session_id: &str,
    tx: UnboundedSender<AppEvent>,
) {
    if config.provider == "desktop" {
        if is_bridge_available(&config.desktop_bridge_url).await {
            stream_via_bridge(config, messages, tx).await;
        } else {
            let _ = tx.send(AppEvent::Err(
                "Kim desktop bridge is not running. Start Kim desktop, or switch provider with /provider ollama.".to_string(),
            ));
        }
        return;
    }

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

    if code_mode {
        stream_codex_subprocess(config, prompt, tx).await;
        return;
    }

    // Standalone Chat Mode: spawn the local Python orchestrator agent
    // to run the task locally with all MCP tools and OS control!
    stream_local_agent_subprocess(config, prompt, session_id, tx).await;
}

/// Non-streaming fallback kept for commands that need a blocking reply.
#[allow(dead_code)]
pub async fn send_kim_request(
    config: &KimConfig,
    messages: &[ChatMessage],
    code_mode: bool,
) -> Result<(String, bool), String> {
    if config.provider == "desktop" && is_bridge_available(&config.desktop_bridge_url).await {
        let reply = send_desktop_bridge(config, messages).await?;
        return Ok((reply, true));
    }
    let system_prompt = if code_mode {
        KIM_CODE_SYSTEM_PROMPT
    } else {
        KIM_CHAT_SYSTEM_PROMPT
    };
    let mut full = vec![ChatMessage {
        role: "system".to_string(),
        content: system_prompt.to_string(),
    }];
    full.extend_from_slice(messages);
    let reply = match config.provider.as_str() {
        "desktop" => {
            return Err(
                "Kim desktop bridge is not running. Start Kim desktop, or switch provider with /provider ollama.".to_string(),
            )
        }
        "claude" => send_anthropic(config, &full).await?,
        _ => send_openai_compatible(config, &full).await?,
    };
    Ok((reply, false))
}

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
    std::env::var("KIM_API_KEY")
        .ok()
        .map(|token| token.trim().to_string())
        .filter(|token| !token.is_empty())
}

/* ===========================================================
streaming implementations
=========================================================== */

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
    let response = request.send().await;
    match response {
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Desktop bridge request failed: {e}")));
        }
        Ok(resp) => {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            if status.is_success() {
                if let Ok(value) = serde_json::from_str::<Value>(&body) {
                    if let Some(response) = value.get("response").and_then(Value::as_str) {
                        let _ = tx.send(AppEvent::TextChunk(response.to_string()));
                    } else if let Some(session_id) = value.get("session_id").and_then(Value::as_str)
                    {
                        let _ = tx.send(AppEvent::ThoughtChunk(format!(
                            "Desktop agent started session {session_id}. Kim desktop will continue the OS task there."
                        )));
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
        trim_base_url(&config.ollama_base_url)
    } else {
        provider.default_base_url.to_string()
    };
    let api_key = provider
        .key_env
        .and_then(|k| std::env::var(k).ok())
        .or_else(|| config.api_keys.get(provider.name).cloned())
        .unwrap_or_else(|| {
            if provider.name == "ollama" {
                "ollama".to_string()
            } else {
                String::new()
            }
        });
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
        // Process complete newline-terminated SSE lines
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

async fn stream_anthropic(
    config: &KimConfig,
    messages: &[ChatMessage],
    tx: UnboundedSender<AppEvent>,
) {
    let api_key = std::env::var("ANTHROPIC_API_KEY")
        .ok()
        .or_else(|| config.api_keys.get("claude").cloned());
    let api_key = match api_key {
        Some(k) => k,
        None => {
            let _ = tx.send(AppEvent::Err("Run /login claude first.".to_string()));
            return;
        }
    };

    // Anthropic separates the system message from the conversation turns.
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
        "max_tokens": 4096,
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

fn sse_data_payload(line: &str) -> Option<&str> {
    line.strip_prefix("data:").map(str::trim_start)
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
                    .and_then(|function| function.get("name"))
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
        _ => {}
    }
}

/* ===========================================================
<think> / </think> tag parser
=========================================================== */

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
                    if let Some(pos) = self.buf.find("<think>") {
                        let before = self.buf[..pos].to_string();
                        if !before.is_empty() {
                            let _ = tx.send(AppEvent::TextChunk(before));
                        }
                        self.buf = self.buf[pos + 7..].to_string();
                        self.state = ThinkState::InThink;
                    } else {
                        // Keep last 6 chars (len("<think>") - 1) to catch split tags
                        let flush_up_to = split_before_tail_chars(&self.buf, 6);
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
                        // Keep last 7 chars (len("</think>") - 1) to catch split tags
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

fn split_before_tail_chars(text: &str, keep_chars: usize) -> usize {
    text.char_indices()
        .rev()
        .nth(keep_chars.saturating_sub(1))
        .map_or(0, |(index, _)| index)
}

/* ===========================================================
non-streaming implementations (kept for /login flows)
=========================================================== */

#[allow(dead_code)]
async fn send_openai_compatible(
    config: &KimConfig,
    messages: &[ChatMessage],
) -> Result<String, String> {
    let provider = provider_info(&config.provider)
        .ok_or_else(|| format!("Unknown provider: {}", config.provider))?;
    let base_url = if provider.name == "ollama" {
        trim_base_url(&config.ollama_base_url)
    } else {
        provider.default_base_url.to_string()
    };
    let api_key = provider
        .key_env
        .and_then(|key| std::env::var(key).ok())
        .or_else(|| config.api_keys.get(provider.name).cloned())
        .unwrap_or_else(|| {
            if provider.name == "ollama" {
                "ollama".to_string()
            } else {
                String::new()
            }
        });
    if api_key.is_empty() {
        return Err(format!("Run /login {} first.", provider.name));
    }

    let client = reqwest::Client::new();
    let request_messages = openai_compatible_messages(messages)?;
    let response = client
        .post(format!(
            "{}/chat/completions",
            base_url.trim_end_matches('/')
        ))
        .bearer_auth(api_key)
        .json(&json!({ "model": config.model, "messages": request_messages, "stream": false }))
        .send()
        .await
        .map_err(|e| format!("Provider request failed: {e}"))?;

    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|e| format!("Provider response failed: {e}"))?;
    if !status.is_success() {
        return Err(format!("Provider returned {status}: {body}"));
    }
    let payload: Value =
        serde_json::from_str(&body).map_err(|e| format!("Bad provider JSON: {e}"))?;
    payload["choices"][0]["message"]["content"]
        .as_str()
        .map(ToOwned::to_owned)
        .filter(|t| !t.trim().is_empty())
        .ok_or_else(|| "Provider returned no message content.".to_string())
}

#[allow(dead_code)]
async fn send_anthropic(config: &KimConfig, messages: &[ChatMessage]) -> Result<String, String> {
    let api_key = std::env::var("ANTHROPIC_API_KEY")
        .ok()
        .or_else(|| config.api_keys.get("claude").cloned())
        .ok_or_else(|| "Run /login claude first.".to_string())?;

    let system = messages
        .iter()
        .find(|m| m.role == "system")
        .map(|m| m.content.as_str())
        .unwrap_or("");
    let conv: Vec<&ChatMessage> = messages.iter().filter(|m| m.role != "system").collect();
    let request_messages = anthropic_messages_ref(&conv)?;

    let mut body = json!({
        "model": config.model,
        "max_tokens": 4096,
        "messages": request_messages,
    });
    if !system.is_empty() {
        body["system"] = json!(system);
    }

    let client = reqwest::Client::new();
    let response = client
        .post("https://api.anthropic.com/v1/messages")
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Claude request failed: {e}"))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|e| format!("Claude response failed: {e}"))?;
    if !status.is_success() {
        return Err(format!("Claude returned {status}: {body}"));
    }
    let payload: Value =
        serde_json::from_str(&body).map_err(|e| format!("Bad Claude JSON: {e}"))?;
    let mut text = String::new();
    if let Some(blocks) = payload["content"].as_array() {
        for block in blocks {
            if let Some(part) = block["text"].as_str() {
                if !text.is_empty() {
                    text.push('\n');
                }
                text.push_str(part);
            }
        }
    }
    if text.trim().is_empty() {
        Err("Claude returned no text content.".to_string())
    } else {
        Ok(text)
    }
}

#[allow(dead_code)]
async fn send_desktop_bridge(
    config: &KimConfig,
    messages: &[ChatMessage],
) -> Result<String, String> {
    let prompt = messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .map(|m| m.content.as_str())
        .unwrap_or_default();
    if prompt.trim().is_empty() {
        return Err("Nothing to send to Kim desktop bridge.".to_string());
    }
    let attachments = image_attachments_from_prompt(prompt)?;
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
    let response = request
        .send()
        .await
        .map_err(|e| format!("Desktop bridge request failed: {e}"))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|e| format!("Desktop bridge response failed: {e}"))?;
    if status.is_success() {
        Ok(format!("Sent to Kim desktop bridge.\n{body}"))
    } else {
        Err(format!("Desktop bridge returned {status}: {body}"))
    }
}

/* ===========================================================
message serializers
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
image helpers
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

fn trim_base_url(base_url: &str) -> String {
    let trimmed = base_url.trim().trim_end_matches('/');
    if trimmed.ends_with("/v1") {
        trimmed.to_string()
    } else {
        format!("{trimmed}/v1")
    }
}

const KIM_CHAT_SYSTEM_PROMPT: &str = "\
You are Kim, a personal AI assistant. Help with any task: questions, research, \
writing, coding, planning, or analysis. Be direct and conversational. \
Remember the conversation context and build on it. When you write code, format it clearly.";

const KIM_CODE_SYSTEM_PROMPT: &str = "\
You are Kim Code, an expert coding agent. Help with programming, debugging, \
code review, refactoring, and architecture.

When working through a problem:
1. Think step by step — show reasoning before giving code.
2. If the user mentions a file path, ask for its content or work with what they share.
3. Write complete, runnable code — no placeholders or TODOs.
4. Format shell commands with a $ prefix so they are easy to copy.
5. Note any bugs or improvements you notice beyond what was asked.

You do not have live screen or file access here — start Kim desktop for that. \
Focus on code quality, clear explanations, and useful output.";

/* ===========================================================
codex subprocess (Code mode)
=========================================================== */

async fn stream_codex_subprocess(config: &KimConfig, prompt: &str, tx: UnboundedSender<AppEvent>) {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    // CRITICAL: never route Code mode through OpenAI to protect gpt-5.5 quota
    if config.provider == "openai" {
        let _ = tx.send(AppEvent::Err(
            "Code mode with OpenAI is disabled. Use /provider ollama or a browser provider."
                .to_string(),
        ));
        return;
    }

    let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let is_browser = config.provider.to_ascii_lowercase().starts_with("browser");

    let mut child = if is_browser {
        match Command::new("python3")
            .args([
                "-m",
                "orchestrator.run_codex_bridge",
                "--task",
                prompt,
                "--cwd",
                &cwd.to_string_lossy(),
                "--provider",
                &config.provider,
            ])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
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
        // Start a local Responses API → Chat Completions proxy so codex can
        // talk to ollama (which only speaks Chat Completions).
        let proxy_port = match start_responses_proxy(config, &tx).await {
            Some(p) => p,
            None => return, // error already sent via tx
        };
        if let Err(e) = write_codex_config(proxy_port, &config.model) {
            let _ = tx.send(AppEvent::Err(format!("Failed to write codex config: {e}")));
            return;
        }
        let kim_codex_home = std::env::temp_dir().join("kim_codex_home");
        match Command::new("codex")
            .args([
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                &cwd.to_string_lossy(),
                prompt,
            ])
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
        // Collect stderr to surface the real error
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
                "codex produced no output. Check that ollama is running and the model name is correct. Switch to Chat mode with /chat for regular AI chat.".to_string(),
            ));
        }
        return;
    }
    let _ = tx.send(AppEvent::Done(true));
}

fn process_codex_line(line: &str, tx: &UnboundedSender<AppEvent>, is_bridge: bool) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    if is_bridge {
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
                    let display = if trimmed.len() > 300 {
                        format!("{}…", &trimmed[..300])
                    } else {
                        trimmed.to_string()
                    };
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
                                let display = if trimmed.len() > 300 {
                                    format!("{}…", &trimmed[..300])
                                } else {
                                    trimmed.to_string()
                                };
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
            // Suppress WebSocket reconnect noise — codex falls back to HTTP automatically
            if !msg.contains("Reconnecting") && !msg.contains("stream disconnected") {
                let _ = tx.send(AppEvent::Err(msg.to_string()));
            }
        }
        _ => {}
    }
}

/// Embeds the proxy script so it's always available at runtime.
const RESPONSES_PROXY_PY: &str = include_str!("responses_proxy.py");

/// Writes the proxy script to a temp file, spawns it, reads back the port it
/// bound to, and returns that port. The proxy process dies with the task because
/// it's spawned with `kill_on_drop(true)`.
async fn start_responses_proxy(config: &KimConfig, tx: &UnboundedSender<AppEvent>) -> Option<u16> {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    // Write proxy script to a temp file
    let tmp_path = std::env::temp_dir().join("kim_responses_proxy.py");
    if let Err(e) = std::fs::write(&tmp_path, RESPONSES_PROXY_PY) {
        let _ = tx.send(AppEvent::Err(format!("Failed to write proxy script: {e}")));
        return None;
    }

    let ollama_base = trim_base_url(&config.ollama_base_url);
    let mut child = match Command::new("python3")
        .args([tmp_path.to_string_lossy().as_ref(), ollama_base.as_str()])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!(
                "Failed to start responses proxy (python3 required): {e}"
            )));
            return None;
        }
    };

    let stdout = child.stdout.take()?;
    let mut lines = BufReader::new(stdout).lines();

    // Proxy prints its port on the first line
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

    // Keep child alive in a background task so kill_on_drop fires when the
    // task is cancelled.  We don't care about its exit status.
    tokio::spawn(async move {
        let _ = child.wait().await;
    });
    Some(port)
}

fn write_codex_config(proxy_port: u16, model: &str) -> Result<(), String> {
    // Use an isolated temp directory so we never touch the user's real ~/.codex config.
    let codex_home = std::env::temp_dir().join("kim_codex_home");
    std::fs::create_dir_all(&codex_home)
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
    })?;
    Ok(())
}

fn command_exists(cmd: &str) -> bool {
    std::process::Command::new(cmd)
        .arg("--version")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|mut child| {
            let _ = child.kill();
            true
        })
        .unwrap_or(false)
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

async fn stream_local_agent_subprocess(
    config: &KimConfig,
    prompt: &str,
    session_id: &str,
    tx: UnboundedSender<AppEvent>,
) {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    let kim_root = crate::sessions::find_kim_repo_root().unwrap_or_else(|| {
        std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."))
    });
    let python = match find_python_interpreter(&kim_root) {
        Ok(p) => p,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    };

    let home = match dirs::home_dir() {
        Some(h) => h,
        None => {
            let _ = tx.send(AppEvent::Err("Could not find home directory".to_string()));
            return;
        }
    };
    let session_dir = home.join(".kim").join("sessions");
    if let Err(e) = std::fs::create_dir_all(&session_dir) {
        let _ = tx.send(AppEvent::Err(format!("Failed to create sessions directory: {e}")));
        return;
    }

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "orchestrator.agent"])
        .arg("--task")
        .arg(prompt)
        .arg("--session-dir")
        .arg(session_dir.to_string_lossy().to_string())
        .arg("--resume")
        .arg(session_id)
        .arg("--provider")
        .arg(&config.provider)
        .current_dir(&kim_root)
        .env("PROJECT_ROOT", kim_root.to_str().unwrap_or(""))
        .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);

    if let Some(key) = config.api_keys.get("openai") {
        cmd.env("OPENAI_API_KEY", key);
    }
    if let Some(key) = config.api_keys.get("claude") {
        cmd.env("ANTHROPIC_API_KEY", key);
    }
    if let Some(key) = config.api_keys.get("gemini") {
        cmd.env("GEMINI_API_KEY", key);
    }
    if let Some(key) = config.api_keys.get("deepseek") {
        cmd.env("DEEPSEEK_API_KEY", key);
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Failed to start agent: {e}")));
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err("Failed to capture stdout.".to_string()));
            return;
        }
    };
    let stderr_pipe = child.stderr.take();

    let mut lines = BufReader::new(stdout).lines();
    let mut had_output = false;
    while let Ok(Some(line)) = lines.next_line().await {
        had_output = true;
        process_agent_line(&line, &tx);
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
            let _ = tx.send(AppEvent::Err(format!("agent: {}", stderr_msg.trim())));
        } else if !had_output {
            let _ = tx.send(AppEvent::Err("agent produced no output.".to_string()));
        } else {
            let _ = tx.send(AppEvent::Err("agent subprocess failed.".to_string()));
        }
        return;
    }
    let _ = tx.send(AppEvent::Done(true));
}

fn process_agent_line(line: &str, tx: &UnboundedSender<AppEvent>) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }

    if let Some(success_idx) = line.find("[SUCCESS]") {
        let summary = line[success_idx + "[SUCCESS]".len()..].trim();
        let _ = tx.send(AppEvent::TextChunk(format!("✓ {summary}\n")));
        return;
    }
    if let Some(failed_idx) = line.find("[FAILED]") {
        let err = line[failed_idx + "[FAILED]".len()..].trim();
        let _ = tx.send(AppEvent::Err(err.to_string()));
        return;
    }
    if let Some(err_idx) = line.find("[ERROR]") {
        let err = line[err_idx + "[ERROR]".len()..].trim();
        let _ = tx.send(AppEvent::Err(err.to_string()));
        return;
    }

    if let Some(status_idx) = line.find("[STATUS]") {
        let text = line[status_idx + "[STATUS]".len()..].trim();
        if !text.is_empty() {
            if text.starts_with("[PLAN]") {
                if let Ok(val) = serde_json::from_str::<Value>(&text["[PLAN]".len()..]) {
                    if let Some(steps) = val.get("steps").and_then(Value::as_array) {
                        let mut plan_text = "Plan:\n".to_string();
                        for (i, step) in steps.iter().enumerate() {
                            if let Some(s) = step.as_str() {
                                plan_text.push_str(&format!("  {}. {}\n", i + 1, s));
                            }
                        }
                        let _ = tx.send(AppEvent::ThoughtChunk(plan_text));
                        return;
                    }
                }
            } else if text.starts_with("[STEP]") {
                if let Ok(val) = serde_json::from_str::<Value>(&text["[STEP]".len()..]) {
                    let idx = val.get("index").and_then(Value::as_u64).unwrap_or(0);
                    let name = val.get("name").and_then(Value::as_str).unwrap_or("");
                    let _ = tx.send(AppEvent::ThoughtChunk(format!("Step {idx}: {name}\n")));
                    return;
                }
            } else if text.starts_with("[DONE]") {
                if let Ok(val) = serde_json::from_str::<Value>(&text["[DONE]".len()..]) {
                    let idx = val.get("index").and_then(Value::as_u64).unwrap_or(0);
                    let summary = val.get("summary").and_then(Value::as_str).unwrap_or("");
                    let _ = tx.send(AppEvent::ThoughtChunk(format!("Step {idx} complete: {summary}\n")));
                    return;
                }
            }
            let _ = tx.send(AppEvent::ThoughtChunk(format!("{text}\n")));
        }
        return;
    }

    if let Some(tool_idx) = line.find("[TOOL]") {
        let rest = line[tool_idx + "[TOOL]".len()..].trim();
        let (verb, target) = if let Some(paren_idx) = rest.find('(') {
            let tool_name = rest[..paren_idx].trim().to_string();
            ("Using tool".to_string(), tool_name)
        } else {
            ("Using tool".to_string(), rest.to_string())
        };
        let _ = tx.send(AppEvent::ToolEvent { verb, target });
        return;
    }

    // Generic lines can be forwarded as status/thought info
    let _ = tx.send(AppEvent::ThoughtChunk(format!("{line}\n")));
}

#[cfg(test)]
mod tests {
    use super::*;

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
        while let Ok(event) = rx.try_recv() {
            events.push(event);
        }

        assert!(events.iter().any(
            |event| matches!(event, AppEvent::ThoughtChunk(text) if text == "checking files")
        ));
        assert!(events.iter().any(
            |event| matches!(event, AppEvent::ToolEvent { target, .. } if target == "read_file")
        ));
        assert!(events
            .iter()
            .any(|event| matches!(event, AppEvent::TextChunk(text) if text == "hello")));
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
        while let Ok(event) = rx.try_recv() {
            events.push(event);
        }

        assert!(events
            .iter()
            .any(|event| matches!(event, AppEvent::ToolEvent { target, .. } if target == "bash")));
        assert!(events.iter().any(
            |event| matches!(event, AppEvent::ThoughtChunk(text) if text == "checking state")
        ));
        assert!(events
            .iter()
            .any(|event| matches!(event, AppEvent::TextChunk(text) if text == "done")));
    }

    #[test]
    fn think_parser_keeps_utf8_boundaries_when_flushing_tail() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();

        parser.feed("for—news", &tx);
        parser.flush(&tx);

        let mut text = String::new();
        while let Ok(event) = rx.try_recv() {
            if let AppEvent::TextChunk(chunk) = event {
                text.push_str(&chunk);
            }
        }

        assert_eq!(text, "for—news");
    }
}
