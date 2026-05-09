//! File Bridge client — replaces the HTTP/SSE API pathway with filesystem IPC.
//!
//! When `CLAW_FILE_BRIDGE=1` is set, claw writes each LLM request to
//! `/tmp/claw_bridge/bridge_request.json` and polls for a matching response at
//! `/tmp/claw_bridge/bridge_response.json`.  An external relay process (Kim)
//! watches for the request, routes it through a browser-based LLM, and writes
//! the response back.
//!
//! This module also defines [`EitherClient`], an enum that wraps both the
//! standard [`super::AnthropicRuntimeClient`] and [`FileBridgeClient`] so the
//! rest of the codebase can remain monomorphic over a single `ApiClient` type.

use std::fs;
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use runtime::{
    ApiClient, ApiRequest, AssistantEvent, ContentBlock, ConversationMessage, MessageRole,
    PromptCacheEvent, RuntimeError, TokenUsage,
};

use serde_json::{json, Value};

// ── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_BRIDGE_DIR: &str = "/tmp/claw_bridge";
const REQUEST_FILE: &str = "bridge_request.json";
const RESPONSE_FILE: &str = "bridge_response.json";
const POLL_INTERVAL: Duration = Duration::from_millis(500);
const TIMEOUT: Duration = Duration::from_secs(600); // 10 minutes — browser scrapes can be slow

// ── FileBridgeClient ─────────────────────────────────────────────────────────

pub struct FileBridgeClient {
    bridge_dir: PathBuf,
}

impl FileBridgeClient {
    pub fn new() -> Self {
        // Honor `CLAW_BRIDGE_DIR` so a relay process can give each Claw run an
        // isolated temp directory (avoids two concurrent runs racing on
        // bridge_request.json). Falls back to /tmp/claw_bridge when unset.
        let bridge_dir = std::env::var("CLAW_BRIDGE_DIR")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(DEFAULT_BRIDGE_DIR));
        fs::create_dir_all(&bridge_dir).expect("Failed to create bridge directory");
        // Clean any stale files from a previous run
        let _ = fs::remove_file(bridge_dir.join(REQUEST_FILE));
        let _ = fs::remove_file(bridge_dir.join(RESPONSE_FILE));
        eprintln!("[file-bridge] Initialized at {}", bridge_dir.display());
        Self { bridge_dir }
    }

    /// Serialize an [`ApiRequest`] into a human-readable JSON object that the
    /// relay process can forward to a browser-based LLM.
    fn serialize_request(&self, request: &ApiRequest) -> String {
        let system = if request.system_prompt.is_empty() {
            Value::Null
        } else {
            Value::String(request.system_prompt.join("\n\n"))
        };

        let messages: Vec<Value> = request
            .messages
            .iter()
            .map(|msg| {
                let role = match msg.role {
                    MessageRole::System => "system",
                    MessageRole::User => "user",
                    MessageRole::Assistant => "assistant",
                    MessageRole::Tool => "tool",
                };
                let blocks: Vec<Value> = msg
                    .blocks
                    .iter()
                    .map(|block| match block {
                        ContentBlock::Text { text } => {
                            json!({"type": "text", "text": text})
                        }
                        ContentBlock::ToolUse { id, name, input } => {
                            json!({"type": "tool_use", "id": id, "name": name, "input": input})
                        }
                        ContentBlock::ToolResult {
                            tool_use_id,
                            tool_name,
                            output,
                            is_error,
                        } => {
                            json!({
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "tool_name": tool_name,
                                "output": output,
                                "is_error": is_error,
                            })
                        }
                    })
                    .collect();
                json!({"role": role, "blocks": blocks})
            })
            .collect();

        let payload = json!({
            "system": system,
            "messages": messages,
        });

        serde_json::to_string_pretty(&payload).unwrap_or_else(|_| "{}".to_string())
    }

    /// Parse a bridge response JSON into a vector of [`AssistantEvent`]s.
    ///
    /// Expected response format:
    /// ```json
    /// {
    ///   "text": "optional thinking / explanation text",
    ///   "tool_calls": [
    ///     {"name": "write_file", "id": "toolu_xxx", "input": {"path": "...", "content": "..."}}
    ///   ]
    /// }
    /// ```
    fn parse_response(raw: &str) -> Result<Vec<AssistantEvent>, RuntimeError> {
        let mut events = Vec::new();

        let parsed: Value = serde_json::from_str(raw).map_err(|e| {
            RuntimeError::new(format!("File bridge: invalid JSON response: {e}"))
        })?;

        // Text
        if let Some(text) = parsed.get("text").and_then(Value::as_str) {
            if !text.is_empty() {
                events.push(AssistantEvent::TextDelta(text.to_string()));
            }
        }

        // Tool calls
        if let Some(tool_calls) = parsed.get("tool_calls").and_then(Value::as_array) {
            for tc in tool_calls {
                let name = tc
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
                    .to_string();

                let id = tc
                    .get("id")
                    .and_then(Value::as_str)
                    .map(String::from)
                    .unwrap_or_else(|| generate_tool_id());

                // input must be a JSON string for the runtime
                let input = match tc.get("input") {
                    Some(v) if v.is_string() => v.as_str().unwrap().to_string(),
                    Some(v) => serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string()),
                    None => "{}".to_string(),
                };

                events.push(AssistantEvent::ToolUse { id, name, input });
            }
        }

        // If no text and no tool calls, treat the entire response as text
        if events.is_empty() {
            events.push(AssistantEvent::TextDelta(raw.to_string()));
        }

        // Usage (zeroed — no billing through the bridge)
        events.push(AssistantEvent::Usage(TokenUsage {
            input_tokens: 0,
            output_tokens: 0,
            cache_creation_input_tokens: 0,
            cache_read_input_tokens: 0,
        }));

        events.push(AssistantEvent::MessageStop);
        Ok(events)
    }
}

impl ApiClient for FileBridgeClient {
    fn stream(&mut self, request: ApiRequest) -> Result<Vec<AssistantEvent>, RuntimeError> {
        let request_path = self.bridge_dir.join(REQUEST_FILE);
        let response_path = self.bridge_dir.join(RESPONSE_FILE);

        // Clean any previous response
        let _ = fs::remove_file(&response_path);

        // 1. Write request
        let payload = self.serialize_request(&request);
        fs::write(&request_path, &payload).map_err(|e| {
            RuntimeError::new(format!("File bridge: failed to write request: {e}"))
        })?;
        eprintln!(
            "[file-bridge] Wrote request ({} bytes) — waiting for response…",
            payload.len()
        );

        // 2. Poll for response
        let start = Instant::now();
        loop {
            if response_path.exists() {
                // Wait briefly for the write to finish (atomic rename should
                // make this unnecessary, but belt-and-suspenders)
                thread::sleep(Duration::from_millis(100));

                // Double-check: file size should be stable (not mid-write)
                let size1 = fs::metadata(&response_path)
                    .map(|m| m.len())
                    .unwrap_or(0);
                thread::sleep(Duration::from_millis(50));
                let size2 = fs::metadata(&response_path)
                    .map(|m| m.len())
                    .unwrap_or(0);
                if size1 == size2 && size1 > 0 {
                    break;
                }
            }

            if start.elapsed() > TIMEOUT {
                // Clean up the stale request
                let _ = fs::remove_file(&request_path);
                return Err(RuntimeError::new(
                    "File bridge: timeout — no response within 5 minutes".to_string(),
                ));
            }

            thread::sleep(POLL_INTERVAL);
        }

        // 3. Read response
        let raw = fs::read_to_string(&response_path).map_err(|e| {
            RuntimeError::new(format!("File bridge: failed to read response: {e}"))
        })?;
        eprintln!(
            "[file-bridge] Got response ({} bytes)",
            raw.len()
        );

        // 4. Clean up
        let _ = fs::remove_file(&request_path);
        let _ = fs::remove_file(&response_path);

        // 5. Parse
        Self::parse_response(&raw)
    }
}

// ── EitherClient ─────────────────────────────────────────────────────────────
//
// Wraps both client types so `BuiltRuntime` stays monomorphic.

pub enum EitherClient {
    Api(super::AnthropicRuntimeClient),
    Bridge(FileBridgeClient),
}

impl ApiClient for EitherClient {
    fn stream(&mut self, request: ApiRequest) -> Result<Vec<AssistantEvent>, RuntimeError> {
        match self {
            Self::Api(inner) => inner.stream(request),
            Self::Bridge(inner) => inner.stream(request),
        }
    }
}

impl EitherClient {
    /// Forward `set_reasoning_effort` to the inner API client (no-op for bridge).
    pub fn set_reasoning_effort(&mut self, effort: Option<String>) {
        if let Self::Api(inner) = self {
            inner.set_reasoning_effort(effort);
        }
    }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn generate_tool_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!("toolu_{nanos:016x}")
}
