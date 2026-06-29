//! P7: run the REAL Kim agent (orchestrator tool loop) from `kim chat` by
//! spawning `python -m orchestrator.agent` and parsing its typed stdout protocol
//! (the same one the desktop consumes). Falls back to plain chat when no Kim
//! source root / Python is available.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc::UnboundedSender;

use crate::markdown::render_markdown;
use crate::provider::AppEvent;

/// One parsed line of the orchestrator's stdout protocol. Pure mapping target so
/// it can be unit-tested without spawning anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentLine {
    /// Dim status / thinking line.
    Activity(String),
    /// A tool invocation (`[TOOL] name(args)`).
    Tool { name: String },
    /// Final answer summary (`[SUCCESS]/[FAILED] ...`).
    Answer(String),
    /// Human-approval request.
    Hitl {
        tool: String,
        risk: String,
        reason: String,
        preview: String,
    },
    /// Run finished (success flag).
    Done(bool),
    /// Provider error code.
    ProviderError(String),
    /// Anything we intentionally don't surface.
    Ignore,
}

/// Parse a single stdout line from the orchestrator into an `AgentLine`.
pub fn parse_agent_line(line: &str) -> AgentLine {
    let line = line.trim_end_matches(['\r', '\n']);
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return AgentLine::Ignore;
    }
    // Typed JSON lines.
    if trimmed.starts_with('{') {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(trimmed) {
            return parse_typed(&v);
        }
        return AgentLine::Ignore;
    }
    // Plain text markers.
    if let Some(rest) = trimmed.strip_prefix("[TOOL] ") {
        let name = rest.split('(').next().unwrap_or(rest).trim().to_string();
        return AgentLine::Tool { name };
    }
    if let Some(rest) = trimmed.strip_prefix("[SUCCESS] ") {
        return AgentLine::Answer(rest.to_string());
    }
    if let Some(rest) = trimmed.strip_prefix("[FAILED] ") {
        return AgentLine::Answer(rest.to_string());
    }
    AgentLine::Ignore
}

fn parse_typed(v: &serde_json::Value) -> AgentLine {
    let t = v.get("type").and_then(|x| x.as_str()).unwrap_or("");
    match t {
        "status" => {
            let msg = v
                .get("message")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
            if msg.is_empty() {
                AgentLine::Ignore
            } else {
                AgentLine::Activity(msg)
            }
        }
        "run_done" => AgentLine::Done(v.get("success").and_then(|x| x.as_bool()).unwrap_or(false)),
        "provider_error" => AgentLine::ProviderError(
            v.get("code")
                .and_then(|x| x.as_str())
                .unwrap_or("error")
                .to_string(),
        ),
        "hitl_approval_request" => AgentLine::Hitl {
            tool: v
                .get("tool")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            risk: v
                .get("risk")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            reason: v
                .get("reason")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
            preview: v
                .get("preview")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string(),
        },
        // plan/step/done/stats/context/usage/ui_* — not surfaced in the terminal.
        _ => AgentLine::Ignore,
    }
}

/// Decide whether `kim chat` can run agentically: needs a Kim source root with
/// the orchestrator, a Python interpreter, and a real (non-browser/desktop)
/// provider. Returns (repo_root, python) when usable.
pub fn agentic_available(provider: &str) -> Option<(PathBuf, PathBuf)> {
    let p = provider.trim().to_lowercase();
    if p == "desktop" || p.starts_with("browser") {
        return None; // those route through the bridge, not the local agent
    }
    let root = crate::sessions::find_kim_repo_root()?;
    if !root.join("orchestrator").join("agent.py").is_file() {
        return None;
    }
    let python = find_python(&root)?;
    Some((root, python))
}

/// Find a Python interpreter: repo venv first, then system.
pub fn find_python(root: &Path) -> Option<PathBuf> {
    let candidates = [
        root.join("venv").join("bin").join("python"),
        root.join("venv").join("Scripts").join("python.exe"),
        root.join(".venv").join("bin").join("python"),
        root.join(".venv").join("Scripts").join("python.exe"),
    ];
    for c in candidates {
        if c.is_file() {
            return Some(c);
        }
    }
    for name in ["python3", "python"] {
        if which(name).is_some() {
            return Some(PathBuf::from(name));
        }
    }
    None
}

fn which(name: &str) -> Option<PathBuf> {
    let out = std::process::Command::new(if cfg!(windows) { "where" } else { "which" })
        .arg(name)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout);
    s.lines().next().map(|l| PathBuf::from(l.trim()))
}

/// Spawn the orchestrator and stream its events into `tx`. HITL requests prompt
/// the terminal and write the decision back to the child stdin.
pub async fn stream_agentic_request(
    root: &Path,
    python: &Path,
    prompt: &str,
    provider: &str,
    session_dir: &Path,
    resume_session_id: Option<&str>,
    tx: UnboundedSender<AppEvent>,
) {
    let mut cmd = Command::new(python);
    cmd.args([
        "-m",
        "orchestrator.agent",
        "--task",
        prompt,
        // Without this the orchestrator falls back to its default provider
        // (browser), so a CLI configured for ollama would spawn the agent on the
        // browser provider and crash. Forward the CLI's configured provider.
        "--provider",
        provider,
        "--session-dir",
    ])
    .arg(session_dir)
    .current_dir(root)
    .env("PYTHONPATH", root)
    // Terminal HITL: the agent gates risky tools; we answer on stdin.
    .env("KIM_HITL_RISK_THRESHOLD", "high")
    .stdin(Stdio::piped())
    .stdout(Stdio::piped())
    .stderr(Stdio::null())
    .kill_on_drop(true);
    if let Some(id) = resume_session_id {
        cmd.arg("--resume").arg(id);
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Could not start Kim agent: {e}")));
            return;
        }
    };
    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err("Kim agent produced no output stream.".into()));
            return;
        }
    };
    let mut child_stdin = child.stdin.take();
    let mut lines = BufReader::new(stdout).lines();
    let mut saw_done = false;
    // The final answer is emitted as `[SUCCESS]/[FAILED] <text>` and is the LAST
    // output; a multi-line answer spills onto following lines that match no marker.
    // Buffer from the first answer line to EOF so multi-line answers aren't truncated
    // to their first line.
    let mut answer_buf: Option<String> = None;

    while let Ok(Some(line)) = lines.next_line().await {
        if let Some(buf) = answer_buf.as_mut() {
            buf.push('\n');
            buf.push_str(line.trim_end_matches(['\r', '\n']));
            continue;
        }
        match parse_agent_line(&line) {
            AgentLine::Activity(m) => {
                let _ = tx.send(AppEvent::ThoughtChunk(m));
            }
            AgentLine::Tool { name } => {
                let _ = tx.send(AppEvent::ToolEvent {
                    verb: "running".into(),
                    target: name,
                });
            }
            AgentLine::Answer(text) => {
                answer_buf = Some(text);
            }
            AgentLine::ProviderError(code) => {
                let _ = tx.send(AppEvent::Err(format!("provider error: {code}")));
            }
            AgentLine::Hitl {
                tool,
                risk,
                reason,
                preview,
            } => {
                let approved = prompt_hitl(&tool, &risk, &reason, &preview).await;
                if let Some(stdin) = child_stdin.as_mut() {
                    let payload = serde_json::json!({"type": "hitl_approve", "approved": approved});
                    let _ = stdin.write_all(format!("{payload}\n").as_bytes()).await;
                    let _ = stdin.flush().await;
                }
            }
            AgentLine::Done(_success) => {
                // Do NOT emit AppEvent::Done here: the orchestrator prints the
                // `[SUCCESS]/[FAILED] <answer>` line AFTER `run_done`, and the consumer
                // breaks on Done — emitting it now drops the answer ("(no response)").
                // Defer Done to end-of-stream so the answer is delivered first.
                saw_done = true;
            }
            AgentLine::Ignore => {}
        }
    }
    let _ = saw_done;
    // Flush the (possibly multi-line) final answer now that we've read to EOF.
    if let Some(buf) = answer_buf {
        let trimmed = buf.trim();
        if !trimmed.is_empty() {
            let _ = tx.send(AppEvent::TextChunk(render_markdown(trimmed)));
        }
    }
    // Process exited (stdout EOF). End the turn. used_bridge=false: this is the local
    // Python agent, not the desktop HTTP bridge (so we don't print "via Kim desktop").
    let _ = child.wait().await;
    let _ = tx.send(AppEvent::Done(false));
}

/// Blocking-ish terminal y/N approval prompt (off the async runtime).
async fn prompt_hitl(tool: &str, risk: &str, reason: &str, preview: &str) -> bool {
    let tool = tool.to_string();
    let risk = risk.to_string();
    let reason = reason.to_string();
    let preview = preview.to_string();
    tokio::task::spawn_blocking(move || {
        use std::io::{self, Write};
        eprintln!("\n\x1b[33mApproval required\x1b[0m: {tool} (risk: {risk}; {reason})");
        if !preview.is_empty() {
            eprintln!("\x1b[2m{preview}\x1b[0m");
        }
        eprint!("Allow this action? [y/N] ");
        let _ = io::stderr().flush();
        let mut buf = String::new();
        if io::stdin().read_line(&mut buf).is_err() {
            return false;
        }
        matches!(buf.trim().to_lowercase().as_str(), "y" | "yes")
    })
    .await
    .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_tool_line() {
        assert_eq!(
            parse_agent_line("[TOOL] list_dir({\"path\": \".\"})"),
            AgentLine::Tool {
                name: "list_dir".into()
            }
        );
    }

    #[test]
    fn parses_success_and_failed_answers() {
        assert_eq!(
            parse_agent_line("[SUCCESS] all done"),
            AgentLine::Answer("all done".into())
        );
        assert_eq!(
            parse_agent_line("[FAILED] nope"),
            AgentLine::Answer("nope".into())
        );
    }

    #[test]
    fn parses_typed_status_and_run_done() {
        assert_eq!(
            parse_agent_line(r#"{"type":"status","message":"thinking"}"#),
            AgentLine::Activity("thinking".into())
        );
        assert_eq!(
            parse_agent_line(r#"{"type":"run_done","success":true}"#),
            AgentLine::Done(true)
        );
        assert_eq!(
            parse_agent_line(r#"{"type":"run_done","success":false}"#),
            AgentLine::Done(false)
        );
    }

    #[test]
    fn parses_hitl_request_with_preview() {
        let line = r#"{"type":"hitl_approval_request","tool":"run_command","risk":"high","reason":"exec","preview":"rm -rf x"}"#;
        assert_eq!(
            parse_agent_line(line),
            AgentLine::Hitl {
                tool: "run_command".into(),
                risk: "high".into(),
                reason: "exec".into(),
                preview: "rm -rf x".into(),
            }
        );
    }

    #[test]
    fn ignores_other_typed_and_blank() {
        assert_eq!(
            parse_agent_line(r#"{"type":"stats","input":1}"#),
            AgentLine::Ignore
        );
        assert_eq!(parse_agent_line(""), AgentLine::Ignore);
        assert_eq!(parse_agent_line("   "), AgentLine::Ignore);
        assert_eq!(parse_agent_line("random text"), AgentLine::Ignore);
    }

    #[test]
    fn agentic_unavailable_for_browser_providers() {
        assert!(agentic_available("browser:claude").is_none());
        assert!(agentic_available("desktop").is_none());
    }

    // ── new regression guards ────────────────────────────────────────────────

    /// find_python must return the repo venv interpreter when a `venv/bin/python`
    /// (or `venv/Scripts/python.exe` on Windows) file exists inside the root,
    /// without falling through to the system-wide shim.
    #[test]
    fn find_python_prefers_venv() {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path();

        // Create the venv directory tree and a stub python file.
        #[cfg(not(windows))]
        let venv_python = root.join("venv").join("bin").join("python");
        #[cfg(windows)]
        let venv_python = root.join("venv").join("Scripts").join("python.exe");

        std::fs::create_dir_all(venv_python.parent().unwrap()).expect("create venv bin dir");
        std::fs::write(&venv_python, b"#!/bin/sh\nexec python3 \"$@\"\n")
            .expect("write stub python");

        let found = find_python(root).expect("find_python should return a path");
        assert_eq!(
            found, venv_python,
            "find_python must return the venv interpreter, got {found:?}"
        );
    }

    /// [SUCCESS] and [FAILED] prefix lines must both map to AgentLine::Answer
    /// carrying exactly the text that follows the marker (no extra whitespace
    /// stripping beyond what trim_end_matches does on the raw line).
    #[test]
    fn parse_agent_line_answer_marker() {
        // Basic success case.
        assert_eq!(
            parse_agent_line("[SUCCESS] task complete"),
            AgentLine::Answer("task complete".into()),
            "[SUCCESS] should yield Answer"
        );
        // Failed case — same variant, different payload.
        assert_eq!(
            parse_agent_line("[FAILED] could not read file"),
            AgentLine::Answer("could not read file".into()),
            "[FAILED] should yield Answer"
        );
        // Payload with internal spaces preserved.
        assert_eq!(
            parse_agent_line("[SUCCESS] done  with  spaces"),
            AgentLine::Answer("done  with  spaces".into()),
            "internal spaces in payload must be preserved"
        );
        // Trailing CR+LF stripped, marker still recognised.
        assert_eq!(
            parse_agent_line("[FAILED] timeout\r\n"),
            AgentLine::Answer("timeout".into()),
            "trailing CRLF should be stripped before matching"
        );
    }

    /// Typed JSON lines for status / run_done / provider_error must map to their
    /// respective AgentLine variants with the correct payload.
    #[test]
    fn parse_agent_line_activity_and_done() {
        // status → Activity
        assert_eq!(
            parse_agent_line(r#"{"type":"status","message":"reading files"}"#),
            AgentLine::Activity("reading files".into()),
            "status with message should be Activity"
        );
        // Empty status message → Ignore (not Activity with empty string)
        assert_eq!(
            parse_agent_line(r#"{"type":"status","message":""}"#),
            AgentLine::Ignore,
            "status with empty message should be Ignore"
        );
        // run_done success=true → Done(true)
        assert_eq!(
            parse_agent_line(r#"{"type":"run_done","success":true}"#),
            AgentLine::Done(true),
            "run_done success:true should be Done(true)"
        );
        // run_done success=false → Done(false)
        assert_eq!(
            parse_agent_line(r#"{"type":"run_done","success":false}"#),
            AgentLine::Done(false),
            "run_done success:false should be Done(false)"
        );
        // provider_error with code → ProviderError(code)
        assert_eq!(
            parse_agent_line(r#"{"type":"provider_error","code":"rate_limit"}"#),
            AgentLine::ProviderError("rate_limit".into()),
            "provider_error should carry the code string"
        );
        // provider_error missing code → falls back to "error"
        assert_eq!(
            parse_agent_line(r#"{"type":"provider_error"}"#),
            AgentLine::ProviderError("error".into()),
            "provider_error without code field should default to 'error'"
        );
    }
}
