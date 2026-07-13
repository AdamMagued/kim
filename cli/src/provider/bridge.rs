//! Kim desktop bridge route: health probe, token pairing, `/v1/task`
//! forwarding, and session-result polling for the `desktop` and `browser:*`
//! providers in chat mode.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

use super::{image_attachments_from_prompt, AppEvent};

pub(crate) async fn is_bridge_available(base_url: &str) -> bool {
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

pub(crate) async fn stream_via_bridge(
    config: &KimConfig,
    messages: &[super::ChatMessage],
    session_id: &str,
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
    // #36: the desktop /v1/task endpoint does not thread attachments to the
    // agent, so images referenced here used to be base64-encoded, sent, and
    // silently dropped. Fail loudly with a path forward instead of pretending
    // the image was delivered.
    if !attachments.is_empty() {
        let names: Vec<&str> = attachments.iter().map(|a| a.name.as_str()).collect();
        let _ = tx.send(AppEvent::Err(format!(
            "Image attachments ({}) aren't supported over the Kim desktop bridge yet. \
             Use a direct vision provider for images — e.g. `/provider ollama` (a vision \
             model) or `/provider claude` — or drop the image and send text only.",
            names.join(", ")
        )));
        return;
    }
    let client = reqwest::Client::new();
    let mut request = client
        .post(format!(
            "{}/v1/task",
            config.desktop_bridge_url.trim_end_matches('/')
        ))
        // F7: without a deadline a wedged desktop app (accepted TCP, hung
        // webview) left the turn spinning forever on the heartbeat. Match the
        // 300 s poll-path deadline.
        .timeout(std::time::Duration::from_secs(300))
        .json(&json!({
            "task": prompt,
            "provider": config.provider,
            "model": config.model,
            // Preserve the CLI conversation identity across desktop-backed
            // turns. `/new` creates a new id, allowing the desktop
            // orchestrator to reset the browser thread instead of silently
            // continuing the previous ChatGPT conversation.
            "session_id": session_id,
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
                let _ = tx.send(AppEvent::ThoughtChunk("Kim desktop is working…\n".to_string()));
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
                        let prior_run_results = value
                            .get("prior_run_results")
                            .and_then(Value::as_u64)
                            .unwrap_or(0) as usize;
                        match poll_bridge_session_answer(
                            &sessions_dir,
                            session_id,
                            prior_run_results,
                            &tx,
                        )
                        .await
                        {
                            Some((answer, _success)) if !answer.trim().is_empty() => {
                                let _ = tx.send(AppEvent::TextChunk(
                                    crate::markdown::render_markdown(answer.trim()),
                                ));
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
    prior_run_results: usize,
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
            if let Some(res) = read_run_result_after(&path, prior_run_results) {
                return Some(res);
            }
        }
        if Instant::now() >= deadline {
            return None;
        }
        if last_beat.elapsed() >= Duration::from_secs(5) {
            let _ = tx.send(AppEvent::ThoughtChunk(
                "Kim desktop is working…\n".to_string(),
            ));
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
fn read_run_result_after(path: &Path, prior_run_results: usize) -> Option<(String, bool)> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut seen = 0usize;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || !line.contains("\"run_result\"") {
            continue;
        }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            if v.get("type").and_then(Value::as_str) == Some("run_result") {
                seen += 1;
                if seen <= prior_run_results {
                    continue;
                }
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

#[cfg(test)]
mod tests {
    use super::*;

    // ── read_run_result_parses_trailing_line ─────────────────────────────────
    // Regression guard: earlier JSONL lines must be ignored; only the trailing
    // `run_result` line is returned as (summary, success).

    #[test]
    fn read_run_result_parses_trailing_line() {
        use std::io::Write as _;
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        // Earlier lines that do NOT contain run_result must be ignored.
        tmp.write_all(b"{\"type\":\"text\",\"content\":\"earlier line\"}\n")
            .unwrap();
        tmp.write_all(b"{\"type\":\"tool_call\",\"name\":\"read_file\"}\n")
            .unwrap();
        // Trailing run_result line is the one that should be returned.
        tmp.write_all(
            b"{\"type\":\"run_result\",\"summary\":\"task complete\",\"success\":true}\n",
        )
        .unwrap();
        tmp.flush().unwrap();

        let (summary, success) = read_run_result_after(tmp.path(), 0)
            .expect("should parse the trailing run_result line");
        assert_eq!(summary, "task complete");
        assert!(success, "success flag should be true");
    }

    #[test]
    fn read_run_result_after_ignores_stale_completed_turns() {
        use std::io::Write as _;
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(
            concat!(
                "{\"type\":\"run_result\",\"summary\":\"stale\",\"success\":true}\n",
                "{\"type\":\"run_started\"}\n",
                "{\"type\":\"run_result\",\"summary\":\"current\",\"success\":true}\n",
            )
            .as_bytes(),
        )
        .unwrap();
        tmp.flush().unwrap();

        assert_eq!(
            read_run_result_after(tmp.path(), 1),
            Some(("current".to_string(), true))
        );
        assert!(read_run_result_after(tmp.path(), 2).is_none());
    }

    // ── find_session_file_direct_and_dated ───────────────────────────────────
    // Regression guard: find_session_file must locate a session JSONL both
    // directly under sessions_dir AND inside a date-named subdirectory.

    #[test]
    fn find_session_file_direct_and_dated() {
        let sessions_dir = tempfile::tempdir().unwrap();
        let session_id = "test-session-abc123";

        // Case 1: file sitting directly under sessions_dir.
        let direct = sessions_dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(&direct, b"").unwrap();
        let found = find_session_file(sessions_dir.path().to_str().unwrap(), session_id);
        assert_eq!(
            found.as_deref(),
            Some(direct.as_path()),
            "should find file directly under sessions_dir"
        );
        std::fs::remove_file(&direct).unwrap();

        // Case 2: file nested inside a date subdirectory.
        let date_dir = sessions_dir.path().join("2024-01-15");
        std::fs::create_dir_all(&date_dir).unwrap();
        let dated = date_dir.join(format!("{session_id}.jsonl"));
        std::fs::write(&dated, b"").unwrap();
        let found = find_session_file(sessions_dir.path().to_str().unwrap(), session_id);
        assert_eq!(
            found.as_deref(),
            Some(dated.as_path()),
            "should find file inside a date subdir"
        );
    }
}
