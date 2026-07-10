//! Typed Kim event → AppEvent translation (app-server transport, Part 4).
//! Split from codex_stream.rs to keep both files under the file-size gate.

use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use super::AppEvent;

/// Translate one typed Kim event (app-server transport, Part 3 vocabulary)
/// into an `AppEvent`. Returns false when the type is not a typed Kim event
/// so the caller can fall through to the raw-codex handler.
pub(crate) fn emit_typed_kim_event(
    json: &Value,
    tx: &UnboundedSender<AppEvent>,
    saw_streamed_answer: &mut bool,
) -> bool {
    let text = |key: &str| {
        json.get(key)
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    match json.get("type").and_then(Value::as_str) {
        Some("assistant_delta") => {
            let chunk = text("chunk");
            if !chunk.is_empty() {
                *saw_streamed_answer = true;
                let _ = tx.send(AppEvent::TextChunk(chunk));
            }
            true
        }
        Some("reasoning_delta") => {
            let chunk = text("chunk");
            if !chunk.is_empty() {
                let _ = tx.send(AppEvent::ThoughtChunk(chunk));
            }
            true
        }
        Some("command_output") => {
            let chunk = text("chunk");
            if !chunk.is_empty() {
                let _ = tx.send(AppEvent::CommandOutput(chunk));
            }
            true
        }
        Some("command_approval_request") => {
            let _ = tx.send(AppEvent::ApprovalRequest {
                id: text("id"),
                command: text("command"),
                cwd: text("cwd"),
                reason: text("reason"),
                risk: text("risk"),
            });
            true
        }
        Some("file_change_approval_request") => {
            let files = json
                .get("files")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|f| f.get("path").and_then(Value::as_str))
                        .collect::<Vec<_>>()
                        .join(", ")
                })
                .unwrap_or_default();
            let _ = tx.send(AppEvent::ApprovalRequest {
                id: text("id"),
                command: if files.is_empty() {
                    "apply file changes".to_string()
                } else {
                    format!("apply changes to: {files}")
                },
                cwd: String::new(),
                reason: text("reason"),
                risk: "files".to_string(),
            });
            true
        }
        Some("plan_update") => {
            let steps = json
                .get("steps")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|s| {
                            let step = s
                                .get("step")
                                .or_else(|| s.get("text"))
                                .and_then(Value::as_str)?;
                            let status =
                                s.get("status").and_then(Value::as_str).unwrap_or("pending");
                            Some((step.to_string(), status.to_string()))
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let _ = tx.send(AppEvent::PlanUpdate(steps));
            true
        }
        Some("diff_update") => {
            let _ = tx.send(AppEvent::DiffUpdate(text("unified_diff")));
            true
        }
        Some("token_usage") => {
            // #9: a field that is PRESENT but not a valid u64 (a string, a
            // negative number, a float) must not silently coerce to 0 via
            // `unwrap_or(0)` — that reports a real-looking but false token
            // count. A genuinely absent (or null) field defaults to 0; an
            // invalid-but-present field skips the whole update instead.
            let numeric_field = |key: &str| -> Result<u64, ()> {
                match json.get(key) {
                    None | Some(Value::Null) => Ok(0),
                    Some(v) => v.as_u64().ok_or(()),
                }
            };
            match (numeric_field("input"), numeric_field("output")) {
                (Ok(input), Ok(output)) => {
                    let _ = tx.send(AppEvent::TokenUsage { input, output });
                }
                _ => {
                    #[cfg(debug_assertions)]
                    eprintln!(
                        "kim: dropping token_usage event with a non-numeric input/output field: {json}"
                    );
                }
            }
            true
        }
        Some("user_input_request") => {
            // #2: Codex is asking the user a question via
            // `item/tool/requestUserInput` (kind == "questions") or an MCP
            // elicitation form (kind == "elicitation", which the Python side
            // already auto-declines without waiting — see
            // codex_appserver_transport.py's `_handle_server_request`). Parse
            // the full shape here; repl_turn.rs decides what to do with it.
            let kind = text("kind");
            let item_id = text("item_id");
            let message = text("message");
            let questions = json
                .get("questions")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .enumerate()
                        .map(|(index, q)| {
                            let id = q
                                .get("id")
                                .and_then(Value::as_str)
                                .map(ToString::to_string)
                                .unwrap_or_else(|| index.to_string());
                            let header = q
                                .get("header")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string();
                            let question = q
                                .get("question")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string();
                            let options = q
                                .get("options")
                                .and_then(Value::as_array)
                                .map(|opts| {
                                    opts.iter()
                                        .filter_map(|o| o.get("label").and_then(Value::as_str))
                                        .map(ToString::to_string)
                                        .collect::<Vec<_>>()
                                })
                                .unwrap_or_default();
                            super::UserInputQuestion {
                                id,
                                header,
                                question,
                                options,
                            }
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let _ = tx.send(AppEvent::UserInputRequest {
                id: text("id"),
                kind,
                item_id,
                message,
                questions,
            });
            true
        }
        Some("turn_lifecycle") => {
            let _ = tx.send(AppEvent::TurnPhase(text("phase")));
            true
        }
        Some("item_lifecycle") => {
            // Command items double as activity lines; the rest are internal.
            if json.get("kind").and_then(Value::as_str) == Some("commandExecution")
                && json.get("phase").and_then(Value::as_str) == Some("started")
            {
                let title = text("title");
                if !title.is_empty() {
                    let _ = tx.send(AppEvent::ToolEvent {
                        verb: "Running".to_string(),
                        target: title,
                    });
                }
            }
            true
        }
        Some("answer") => {
            // The final answer duplicates the streamed assistant deltas.
            if !*saw_streamed_answer {
                let answer = text("text");
                if !answer.is_empty() {
                    let _ = tx.send(AppEvent::TextChunk(answer));
                }
                *saw_streamed_answer = true;
            }
            true
        }
        _ => false,
    }
}
