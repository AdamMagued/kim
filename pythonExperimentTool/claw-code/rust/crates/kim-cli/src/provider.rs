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

    match config.provider.as_str() {
        "claude" => stream_anthropic(config, &full, tx).await,
        _ => stream_openai_compatible(config, &full, tx).await,
    }
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
                        let keep = self.buf.len().min(6);
                        let flush_up_to = self.buf.len() - keep;
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
                        let keep = self.buf.len().min(7);
                        let flush_up_to = self.buf.len() - keep;
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
}
