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
            let _ = tx.send(AppEvent::TokenUsage {
                input: json.get("input").and_then(Value::as_u64).unwrap_or(0),
                output: json.get("output").and_then(Value::as_u64).unwrap_or(0),
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
