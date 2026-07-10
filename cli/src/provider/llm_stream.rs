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

/// #3: request-wide deadline for direct-provider streaming calls (connect +
/// headers + full body read). Mirrors `bridge.rs`'s `stream_via_bridge` 300s
/// timeout — without one, a server that accepts the connection and then never
/// sends more bytes hangs the turn forever.
const STREAM_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(300);

/// #3: heartbeat cadence between SSE chunks. Mirrors `bridge.rs`'s 5s
/// heartbeat so a mid-stream stall shows *something* instead of silence.
const HEARTBEAT_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

/// Finding #1 (HIGH): without a cap, a remote server that never terminates a
/// line with `\n` would let `SseLineBuffer::buf` grow without bound (memory
/// DoS). 512 KiB is generous for any legitimate single SSE line (even a huge
/// streamed code block) while still bounding a pathological/hostile stream.
/// Mirrors the existing bounded-buffer pattern elsewhere in this crate:
/// `drain_stderr_tail`'s `MAX_TAIL_BYTES` (provider.rs) and `commands.rs`'s
/// `MAX_OUTPUT_CHARS`.
const MAX_BUFFER_BYTES: usize = 512 * 1024;

/// F2/F16: byte-level SSE line assembler. Network chunks split multi-byte
/// UTF-8 sequences at arbitrary offsets; decoding each chunk independently
/// (the old `String::from_utf8_lossy` per chunk) corrupted any CJK/emoji/dash
/// character straddling a boundary into `��` — and that corruption was
/// persisted into the session file. Buffer raw bytes, split on `\n` at the
/// byte level, and decode only complete lines. `drain` also removes the old
/// full-buffer reallocation per line (F16).
struct SseLineBuffer {
    buf: Vec<u8>,
}

impl SseLineBuffer {
    fn new() -> Self {
        Self { buf: Vec::new() }
    }

    /// Feed one network chunk; invokes `on_line` for each complete
    /// (trimmed) line. Returns `Err` instead of growing `buf` past
    /// `MAX_BUFFER_BYTES` when no line terminator has been found yet (#1) —
    /// callers should propagate this as a clean stream error, not panic.
    fn push_chunk(&mut self, bytes: &[u8], mut on_line: impl FnMut(&str)) -> Result<(), String> {
        self.buf.extend_from_slice(bytes);
        let mut start = 0usize;
        while let Some(rel) = self.buf[start..].iter().position(|&b| b == b'\n') {
            let end = start + rel;
            let line = String::from_utf8_lossy(&self.buf[start..end]);
            on_line(line.trim());
            start = end + 1;
        }
        if start > 0 {
            self.buf.drain(..start);
        }
        if self.buf.len() > MAX_BUFFER_BYTES {
            return Err(format!(
                "SSE line exceeded {MAX_BUFFER_BYTES} bytes without a line terminator \
                 — aborting the stream to avoid unbounded memory growth"
            ));
        }
        Ok(())
    }

    /// Flush any trailing line left after the stream ends.
    fn finish(self, mut on_line: impl FnMut(&str)) {
        let line = String::from_utf8_lossy(&self.buf);
        let trimmed = line.trim();
        if !trimmed.is_empty() {
            on_line(trimmed);
        }
    }
}

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
        // #3: bound the whole request (connect + headers + full body read) so
        // a stalled server can't hang the turn forever. Mirrors bridge.rs's
        // `stream_via_bridge` 300s deadline.
        .timeout(STREAM_TIMEOUT)
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
    let mut line_buf = SseLineBuffer::new();
    let mut parser = ThinkParser::new();
    // #3: heartbeat between chunks so a mid-stream stall doesn't read as a
    // silent hang — mirrors bridge.rs's `stream_via_bridge` heartbeat.
    let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat.tick().await; // consume the immediate first tick

    loop {
        let chunk = tokio::select! {
            biased;
            c = stream.next() => c,
            _ = heartbeat.tick() => {
                let _ = tx.send(AppEvent::ThoughtChunk(".".to_string()));
                continue;
            }
        };
        let Some(chunk) = chunk else { break };
        let bytes = match chunk {
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Stream error: {e}")));
                return;
            }
            Ok(b) => b,
        };
        if let Err(e) = line_buf.push_chunk(&bytes, |line| {
            process_openai_sse_line(line, &mut parser, &tx);
        }) {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    }
    line_buf.finish(|line| {
        process_openai_sse_line(line, &mut parser, &tx);
    });
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
        // #3: same 300s request-wide deadline as the OpenAI-compatible path.
        .timeout(STREAM_TIMEOUT)
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
    let mut line_buf = SseLineBuffer::new();
    let mut parser = ThinkParser::new();
    // #3: same heartbeat-between-chunks approach as the OpenAI-compatible path.
    let mut heartbeat = tokio::time::interval(HEARTBEAT_INTERVAL);
    heartbeat.tick().await; // consume the immediate first tick

    loop {
        let chunk = tokio::select! {
            biased;
            c = stream.next() => c,
            _ = heartbeat.tick() => {
                let _ = tx.send(AppEvent::ThoughtChunk(".".to_string()));
                continue;
            }
        };
        let Some(chunk) = chunk else { break };
        let bytes = match chunk {
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Claude stream error: {e}")));
                return;
            }
            Ok(b) => b,
        };
        if let Err(e) = line_buf.push_chunk(&bytes, |line| {
            process_anthropic_sse_line(line, &mut parser, &tx);
        }) {
            let _ = tx.send(AppEvent::Err(e));
            return;
        }
    }
    line_buf.finish(|line| {
        process_anthropic_sse_line(line, &mut parser, &tx);
    });
    parser.flush(&tx);
    let _ = tx.send(AppEvent::Done(false));
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── SseLineBuffer (F2): UTF-8 chars split across chunks must survive ────

    fn collect_lines(chunks: &[&[u8]]) -> Vec<String> {
        let mut out = Vec::new();
        let mut buf = SseLineBuffer::new();
        for chunk in chunks {
            buf.push_chunk(chunk, |line| out.push(line.to_string()))
                .expect("test chunks stay under the cap");
        }
        buf.finish(|line| out.push(line.to_string()));
        out
    }

    // ── #1: unbounded SSE buffer growth is capped, not silently allowed ─────

    #[test]
    fn sse_line_buffer_errors_instead_of_growing_unbounded_past_cap() {
        let mut buf = SseLineBuffer::new();
        // Feed chunks well past MAX_BUFFER_BYTES with no '\n' anywhere — a
        // hostile/broken server that never terminates a line. Before the fix
        // this grew `buf` without bound; now it must return a clean error
        // once the cap is exceeded, rather than continuing to allocate.
        let chunk = vec![b'x'; 64 * 1024];
        let mut result = Ok(());
        let mut fed = 0usize;
        for _ in 0..(MAX_BUFFER_BYTES / chunk.len() + 4) {
            result = buf.push_chunk(&chunk, |_| {});
            fed += chunk.len();
            if result.is_err() {
                break;
            }
        }
        assert!(
            result.is_err(),
            "expected an error once the buffer exceeded the cap"
        );
        assert!(
            fed <= MAX_BUFFER_BYTES + chunk.len(),
            "buffer must error out at or shortly after the cap, not keep growing silently \
             (fed {fed} bytes before erroring, cap is {MAX_BUFFER_BYTES})"
        );
        assert!(
            buf.buf.len() <= MAX_BUFFER_BYTES + chunk.len(),
            "internal buffer must not keep growing past the cap: len={}",
            buf.buf.len()
        );
    }

    #[test]
    fn sse_line_buffer_survives_utf8_split_across_chunks() {
        // "data: é—🦀\n" split in the middle of each multi-byte sequence.
        let full = "data: é—🦀\n".as_bytes();
        for split in 1..full.len() {
            let lines = collect_lines(&[&full[..split], &full[split..]]);
            assert_eq!(
                lines,
                vec!["data: é—🦀".to_string()],
                "split at byte {split} corrupted the line"
            );
            assert!(
                !lines[0].contains('\u{FFFD}'),
                "split at byte {split} produced a replacement character"
            );
        }
    }

    #[test]
    fn sse_line_buffer_emits_multiple_lines_and_trailing_tail() {
        let lines = collect_lines(&[b"a\nb\nc", b"d"]);
        assert_eq!(lines, vec!["a", "b", "cd"]);
    }

    #[test]
    fn sse_line_buffer_trims_and_skips_blank_tail() {
        let lines = collect_lines(&[b"  data: x  \n", b"   "]);
        assert_eq!(lines, vec!["data: x"]);
    }

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
