//! SSE line processors and the `<think>` / harmony-marker stream parser shared
//! by the OpenAI-compatible and Anthropic streaming routes.

use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use super::AppEvent;

pub(crate) fn sse_data_payload(line: &str) -> Option<&str> {
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

pub(crate) fn process_openai_sse_line(
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

pub(crate) fn process_anthropic_sse_line(
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

pub(crate) struct ThinkParser {
    state: ThinkState,
    buf: String,
}

impl ThinkParser {
    pub(crate) fn new() -> Self {
        Self {
            state: ThinkState::Normal,
            buf: String::new(),
        }
    }

    pub(crate) fn feed(&mut self, chunk: &str, tx: &UnboundedSender<AppEvent>) {
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

    pub(crate) fn flush(&mut self, tx: &UnboundedSender<AppEvent>) {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn drain(rx: &mut tokio::sync::mpsc::UnboundedReceiver<AppEvent>) -> Vec<AppEvent> {
        let mut events = Vec::new();
        while let Ok(e) = rx.try_recv() {
            events.push(e);
        }
        events
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
        let events = drain(&mut rx);
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
        let events = drain(&mut rx);
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

    // #43: stream a long prefix + the marker one char at a time. No emitted
    // chunk may ever contain a partial marker prefix ("assistantfina", etc.),
    // and the full answer must arrive intact. This exercises the worst case the
    // audit worried about (a boundary landing mid-marker after >14 chars of
    // preceding text). If this passes, the 14-char tail hold-back is sufficient.
    #[test]
    fn assistantfinal_never_leaks_partial_marker_when_streamed_char_by_char() {
        const MARKER: &str = "assistantfinal";
        let prefix = "some long reasoning that exceeds the tail holdback window ";
        let suffix = "The final answer.";
        let full = format!("{prefix}{MARKER}{suffix}");

        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut parser = ThinkParser::new();
        for ch in full.chars() {
            let mut buf = [0u8; 4];
            parser.feed(ch.encode_utf8(&mut buf), &tx);
        }
        parser.flush(&tx);

        let events = drain(&mut rx);
        // #43's actual claim: no individual emitted chunk may contain the marker
        // or any prefix of it of length >= 4 (short overlaps like "as" are
        // unavoidable and harmless). This is the property that would break if the
        // 14-char tail hold-back were too small.
        let mut visible = String::new();
        for e in &events {
            if let AppEvent::TextChunk(t) | AppEvent::ThoughtChunk(t) = e {
                assert!(!t.contains(MARKER), "full marker leaked in a chunk: {t:?}");
                for n in (4..MARKER.len()).rev() {
                    let partial = &MARKER[..n];
                    assert!(
                        !t.contains(partial),
                        "partial marker {partial:?} leaked in chunk {t:?}"
                    );
                }
                visible.push_str(t);
            }
        }
        // Nothing lost or duplicated: the marker is stripped and the surrounding
        // text is delivered exactly once, in order. (Role classification of the
        // pre-marker text is timing-dependent when streamed and is not asserted.)
        assert_eq!(visible, format!("{prefix}{suffix}"));
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
}
