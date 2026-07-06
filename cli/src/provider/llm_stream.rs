//! Direct-to-provider chat streaming: OpenAI-compatible endpoints (ollama,
//! openai, gemini, deepseek) and the native Anthropic Messages API.

use futures_util::StreamExt;
use serde_json::json;
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

use super::sse::{process_anthropic_sse_line, process_openai_sse_line, ThinkParser};
use super::{
    anthropic_messages_ref, normalize_base_url, openai_compatible_messages, provider_info,
    resolve_api_key, AppEvent, ChatMessage,
};

/// Maximum output tokens requested from the Anthropic API (#24).
/// 8192 is the published output-token limit for Claude-3 and later models.
/// Formerly hardcoded inline as the slightly-wrong magic number 8096.
const ANTHROPIC_MAX_TOKENS: u32 = 8192;

pub(crate) async fn stream_openai_compatible(
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

pub(crate) async fn stream_anthropic(
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

#[cfg(test)]
mod tests {
    use super::*;

    // ── anthropic_max_tokens_constant (#24) ──────────────────────────────────
    // Regression guard: the constant must be the corrected 8192 value (not the
    // former magic number 8096) and must propagate into the request body JSON.

    #[test]
    fn anthropic_max_tokens_constant() {
        assert_eq!(
            ANTHROPIC_MAX_TOKENS, 8192u32,
            "ANTHROPIC_MAX_TOKENS must be 8192 (was previously the wrong value 8096)"
        );
        // Verify the constant produces the correct numeric value when serialised
        // into a JSON request body (matches the `json!` call in stream_anthropic).
        let body = serde_json::json!({ "max_tokens": ANTHROPIC_MAX_TOKENS });
        assert_eq!(
            body["max_tokens"].as_u64().unwrap(),
            8192u64,
            "max_tokens in request body must equal 8192"
        );
    }
}
