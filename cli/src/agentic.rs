//! P7: run the REAL Kim agent (orchestrator tool loop) from `kim chat` by
//! spawning `python -m orchestrator.agent` and parsing its typed stdout protocol
//! (the same one the desktop consumes). Falls back to plain chat when no Kim
//! source root / Python is available.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::io::{AsyncWriteExt, BufReader};
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
    /// Final answer summary from a SUCCEEDED run (`[SUCCESS] ...`).
    Answer(String),
    /// Final answer summary from a FAILED run (`[FAILED] ...`). Kept distinct
    /// from `Answer` so the CLI renders it with the Error role and one-shot mode
    /// exits non-zero. (F-E-4)
    FailedAnswer(String),
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

fn child_failure_detail(exit_ok: bool, had_output: bool, stderr_tail: String) -> Option<String> {
    if exit_ok && had_output {
        return None;
    }
    Some(if stderr_tail.is_empty() {
        if !exit_ok {
            "process exited unsuccessfully without an error message".to_string()
        } else {
            "process exited without producing output".to_string()
        }
    } else {
        stderr_tail
    })
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
    // F-E-4: `[FAILED] …` is a distinct variant, not another Answer — the run
    // declared failure and must be surfaced as an error, not a normal reply.
    if let Some(rest) = trimmed.strip_prefix("[FAILED] ") {
        return AgentLine::FailedAnswer(rest.to_string());
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
        // #33: the orchestrator now emits tool use as a typed `{"type":"tool",
        // "name":...}` event instead of the legacy `[TOOL] name(...)` text line,
        // so the plain-text branch below never matches. Map it here so the CLI
        // still shows "running <tool>" activity.
        "tool" => {
            let name = v
                .get("name")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .trim()
                .to_string();
            if name.is_empty() {
                AgentLine::Ignore
            } else {
                AgentLine::Tool { name }
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
/// the orchestrator and a Python interpreter. The `desktop` provider always
/// routes through the bridge, so it is excluded. Browser providers are allowed:
/// orchestrator.agent drives ChatGPT/Gemini/Claude over Playwright/CDP
/// (auto-launching Chrome) with no desktop app — the same mechanism code mode
/// uses. Callers that prefer a running desktop bridge should probe it first
/// (see run_turn). Returns (repo_root, python) when usable.
pub fn agentic_available(provider: &str) -> Option<(PathBuf, PathBuf)> {
    let root = crate::sessions::find_kim_repo_root()?;
    agentic_available_at(provider, root)
}

/// Resolve agentic execution against an explicit Kim root. Keeping root
/// validation separate makes the no-desktop fallback testable without relying
/// on the developer machine's environment or a running browser.
pub(crate) fn agentic_available_at(provider: &str, root: PathBuf) -> Option<(PathBuf, PathBuf)> {
    let p = provider.trim().to_lowercase();
    if p == "desktop" {
        return None; // the desktop provider always routes through the bridge
    }
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

pub(crate) fn which(name: &str) -> Option<PathBuf> {
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
#[allow(clippy::too_many_arguments)]
pub async fn stream_agentic_request(
    root: &Path,
    python: &Path,
    prompt: &str,
    provider: &str,
    session_dir: &Path,
    resume_session_id: Option<&str>,
    // F-E-5: when set, the spawned orchestrator's pid is published here so a
    // Ctrl-C mid-run can send it a graceful SIGTERM (letting the agent flush its
    // session/checkpoint and shut down its own children — the MCP server, a
    // Playwright-launched Chrome) BEFORE the hard kill_on_drop SIGKILL. Chat-mode
    // agentic runs previously left this None and went straight to SIGKILL.
    pid_slot: Option<std::sync::Arc<std::sync::Mutex<Option<u32>>>>,
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
    // KIM_TAURI_MODE=1 is required too (#28): the orchestrator only wires
    // StdinApprovalBridge (and starts the stdin pump that routes our
    // hitl_approve replies) when BOTH env vars are set. Without it the
    // approval gate was silently skipped and high-risk tools ran unprompted.
    .env("KIM_HITL_RISK_THRESHOLD", "high")
    .env("KIM_TAURI_MODE", "1")
    .stdin(Stdio::piped())
    .stdout(Stdio::piped())
    // F10: stderr was Stdio::null() — a child that died on startup (missing
    // venv dep, import error) produced only "Kim: (no response)". Pipe it and
    // drain concurrently (bounded tail) so crashes are diagnosable.
    .stderr(Stdio::piped())
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
    // F-E-5: publish the child pid so Ctrl-C can SIGTERM it (graceful) before
    // the kill_on_drop SIGKILL fallback.
    if let Some(slot) = &pid_slot {
        if let Ok(mut s) = slot.lock() {
            *s = child.id();
        }
    }
    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err("Kim agent produced no output stream.".into()));
            return;
        }
    };
    let mut child_stdin = child.stdin.take();
    // F10: bounded concurrent stderr drain (shared helper, see provider.rs).
    let stderr_tail = child.stderr.take().map(crate::provider::drain_stderr_tail);
    // F-E-6: a length-capped line reader — one oversized/newline-less line
    // (base64 screenshot, runaway tool output, corrupt stream) is drained past
    // the cap instead of buffered fully into RAM.
    let mut reader = BufReader::new(stdout);
    // F-E-4: the orchestrator declares run success/failure via
    // run_done{success} and a `[SUCCESS]/[FAILED] …` line, but the python
    // process exits 0 either way — so we must track the declared outcome
    // explicitly rather than trusting the child's exit code.
    let mut run_success: Option<bool> = None;
    let mut answer_failed = false;
    let mut had_output = false;
    // F9: a stdout read error must surface instead of masquerading as EOF.
    let mut read_err: Option<String> = None;
    // The final answer is emitted as `[SUCCESS]/[FAILED] <text>` and is the LAST
    // output; a multi-line answer spills onto following lines that match no marker.
    // Buffer from the first answer line to EOF so multi-line answers aren't truncated
    // to their first line.
    let mut answer_buf: Option<String> = None;

    loop {
        let line = match crate::provider::read_capped_line(
            &mut reader,
            crate::provider::SUBPROCESS_LINE_CAP,
        )
        .await
        {
            Ok(Some(crate::provider::CappedLine::Line(line))) => line,
            Ok(Some(crate::provider::CappedLine::Truncated(_))) => {
                // F-E-6: a line past the cap is pathological (runaway/corrupt
                // stream). Surface it as an error rather than parsing a
                // truncated fragment as an answer.
                read_err = Some(format!(
                    "agent output line exceeded {} bytes and was truncated",
                    crate::provider::SUBPROCESS_LINE_CAP
                ));
                break;
            }
            Ok(None) => break,
            Err(e) => {
                read_err = Some(e.to_string());
                break;
            }
        };
        had_output = true;
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
            AgentLine::FailedAnswer(text) => {
                answer_buf = Some(text);
                answer_failed = true;
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
                    // K1 vocabulary alongside the legacy bool: the orchestrator
                    // broker understands decision=accept/acceptForSession/decline.
                    let payload = serde_json::json!({
                        "type": "hitl_approve",
                        "approved": approved,
                        "decision": if approved { "accept" } else { "decline" },
                    });
                    let _ = stdin.write_all(format!("{payload}\n").as_bytes()).await;
                    let _ = stdin.flush().await;
                }
            }
            AgentLine::Done(success) => {
                // Do NOT emit AppEvent::Done here: the orchestrator prints the
                // `[SUCCESS]/[FAILED] <answer>` line AFTER `run_done`, and the consumer
                // breaks on Done — emitting it now drops the answer ("(no response)").
                // Defer Done to end-of-stream so the answer is delivered first.
                // F-E-4: record the declared success so a failed run exits non-zero.
                run_success = Some(success);
            }
            AgentLine::Ignore => {}
        }
    }
    // F-E-4: a run the agent itself declared FAILED — via `[FAILED] …` or
    // run_done{success:false} — must not be delivered as a normal answer.
    let failed = answer_failed || run_success == Some(false);
    // Flush the (possibly multi-line) final answer now that we've read to EOF.
    // A failed run's summary goes out as an Err (Error role) so one-shot mode
    // exits non-zero; a successful answer as a normal TextChunk.
    let mut sent_failure = false;
    if let Some(buf) = answer_buf {
        let trimmed = buf.trim();
        if !trimmed.is_empty() {
            let rendered = render_markdown(trimmed);
            if failed {
                let _ = tx.send(AppEvent::Err(rendered));
                sent_failure = true;
            } else {
                let _ = tx.send(AppEvent::TextChunk(rendered));
            }
        }
    }
    // On a read error, close our end of the stdout pipe before wait() so a
    // still-writing child can't block forever on a full pipe. (F9)
    drop(reader);
    // Process exited (stdout EOF). End the turn. used_bridge=false: this is the local
    // Python agent, not the desktop HTTP bridge (so we don't print "via Kim desktop").
    let exit_ok = child.wait().await.map(|s| s.success()).unwrap_or(false);
    // F9/F10: surface failures instead of ending like a normal short answer.
    if read_err.is_some() || !exit_ok || !had_output {
        let tail = match stderr_tail {
            Some(handle) => handle.await.unwrap_or_default(),
            None => String::new(),
        };
        if let Some(e) = read_err {
            let extra = if tail.is_empty() {
                String::new()
            } else {
                format!("\n{tail}")
            };
            let _ = tx.send(AppEvent::Err(format!(
                "Kim agent output read error: {e}{extra}"
            )));
            return;
        }
        if let Some(detail) = child_failure_detail(exit_ok, had_output, tail) {
            let _ = tx.send(AppEvent::Err(format!("Kim agent: {detail}")));
            return;
        }
    }
    // F-E-4: a failed run that exited 0 with output but produced no answer text
    // still must not report success.
    if failed && !sent_failure {
        let _ = tx.send(AppEvent::Err(
            "Kim agent run failed (no summary produced).".to_string(),
        ));
        return;
    }
    // The failed answer was already delivered as an Err (which ends the turn on
    // the consumer side); don't also send a normal Done.
    if sent_failure {
        return;
    }
    let _ = tx.send(AppEvent::Done(false));
}

/// Terminal y/N approval prompt. Reads through the process-wide owned stdin
/// reader (F6/T1): a Ctrl-C mid-prompt no longer leaks a blocked reader thread
/// whose late line could be applied to the next prompt or swallow REPL input.
async fn prompt_hitl(tool: &str, risk: &str, reason: &str, preview: &str) -> bool {
    use std::io::{IsTerminal, Write};
    eprintln!("\n\x1b[33mApproval required\x1b[0m: {tool} (risk: {risk}; {reason})");
    if !preview.is_empty() {
        eprintln!("\x1b[2m{preview}\x1b[0m");
    }
    // F5: when stdin is not a terminal, the queued lines are prompts, not
    // approval answers — never consume one; deny by default.
    if !std::io::stdin().is_terminal() {
        eprintln!("Allow this action? [y/N] n  (stdin is not a terminal — auto-denied)");
        return false;
    }
    eprint!("Allow this action? [y/N] ");
    let _ = std::io::stderr().flush();
    match crate::stdin_reader::read_stdin_line().await {
        Some(line) => matches!(line.trim().to_lowercase().as_str(), "y" | "yes"),
        None => false,
    }
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
        // F-E-4: [FAILED] is a distinct variant, not an Answer.
        assert_eq!(
            parse_agent_line("[FAILED] nope"),
            AgentLine::FailedAnswer("nope".into())
        );
    }

    #[test]
    fn parses_typed_tool_event() {
        // #33: the orchestrator emits typed tool events, not [TOOL] text.
        assert_eq!(
            parse_agent_line(r#"{"type":"tool","name":"read_file","args":{}}"#),
            AgentLine::Tool {
                name: "read_file".into()
            }
        );
        // Empty/missing name is ignored rather than shown as a blank tool.
        assert_eq!(
            parse_agent_line(r#"{"type":"tool","name":""}"#),
            AgentLine::Ignore
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
    fn failed_child_without_stderr_is_not_silent() {
        let detail = child_failure_detail(false, true, String::new()).unwrap();
        assert!(detail.contains("exited unsuccessfully"));
        assert!(child_failure_detail(true, true, String::new()).is_none());
    }

    #[test]
    fn agentic_available_treats_browser_like_a_real_provider() {
        // The `desktop` provider always routes through the bridge, never the
        // local agent.
        assert!(agentic_available("desktop").is_none());
        // Browser providers are no longer force-excluded: they resolve to the
        // local Playwright agent exactly like a normal provider (ollama), so
        // both must agree on availability in whatever environment the test runs.
        assert_eq!(
            agentic_available("browser:chatgpt").is_some(),
            agentic_available("ollama").is_some(),
        );
    }

    #[test]
    fn browser_agentic_route_resolves_from_fake_repo_root() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("orchestrator")).unwrap();
        std::fs::write(root.path().join("orchestrator").join("agent.py"), "").unwrap();
        let python = if cfg!(windows) {
            root.path().join("venv").join("Scripts").join("python.exe")
        } else {
            root.path().join("venv").join("bin").join("python")
        };
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, "fake interpreter").unwrap();

        let resolved = agentic_available_at("browser:chatgpt", root.path().to_path_buf())
            .expect("browser provider should resolve through the fake local Kim root");
        assert_eq!(resolved.0, root.path());
        assert_eq!(resolved.1, python);
        assert!(agentic_available_at("desktop", root.path().to_path_buf()).is_none());
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
        // Failed case — a distinct variant (F-E-4) so it renders as an error.
        assert_eq!(
            parse_agent_line("[FAILED] could not read file"),
            AgentLine::FailedAnswer("could not read file".into()),
            "[FAILED] should yield FailedAnswer"
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
            AgentLine::FailedAnswer("timeout".into()),
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

    /// F-E-4: an orchestrator run that ends with run_done{success:false} and a
    /// `[FAILED] …` line (but still exits 0, as the real orchestrator does) must
    /// be delivered to the CLI as an `AppEvent::Err`, NOT a normal TextChunk —
    /// otherwise one-shot `kim chat` renders it like any answer and exits 0.
    ///
    /// Driven with a fake "python" that prints the protocol and exits 0, so the
    /// test is hermetic (no venv / orchestrator required). Unix-only: it relies
    /// on an executable shell script.
    #[cfg(unix)]
    #[tokio::test]
    async fn failed_run_is_delivered_as_error_not_a_normal_answer() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let fake = root.join("fake-python.sh");
        std::fs::write(
            &fake,
            "#!/bin/sh\n\
             echo '{\"type\":\"run_done\",\"success\":false}'\n\
             echo '[FAILED] the task could not be completed'\n",
        )
        .unwrap();
        let mut perm = std::fs::metadata(&fake).unwrap().permissions();
        perm.set_mode(0o755);
        std::fs::set_permissions(&fake, perm).unwrap();

        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        let session_dir = root.join("kim_sessions");
        stream_agentic_request(root, &fake, "do X", "ollama", &session_dir, None, None, tx).await;

        let mut events = Vec::new();
        while let Ok(ev) = rx.try_recv() {
            events.push(ev);
        }
        assert!(
            !events.iter().any(
                |e| matches!(e, AppEvent::TextChunk(t) if t.contains("could not be completed"))
            ),
            "a FAILED run must not be delivered as a normal answer; events: {events:?}"
        );
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::Err(t) if t.contains("could not be completed"))),
            "a FAILED run must be delivered as an Error; events: {events:?}"
        );
    }

    /// F-E-5: the chat-mode agentic child publishes its pid to the shared slot,
    /// so a Ctrl-C mid-run can SIGTERM it (graceful) instead of only SIGKILL.
    /// Before the fix, `stream_agentic_request` had no pid slot and the chat
    /// path passed None, so the pid was never recorded.
    #[cfg(unix)]
    #[tokio::test]
    async fn agentic_child_pid_is_recorded_for_graceful_cancel() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let fake = root.join("fake-python.sh");
        std::fs::write(
            &fake,
            "#!/bin/sh\n\
             echo '{\"type\":\"run_done\",\"success\":true}'\n\
             echo '[SUCCESS] ok'\n",
        )
        .unwrap();
        let mut perm = std::fs::metadata(&fake).unwrap().permissions();
        perm.set_mode(0o755);
        std::fs::set_permissions(&fake, perm).unwrap();

        let pid_slot = std::sync::Arc::new(std::sync::Mutex::new(None::<u32>));
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        let session_dir = root.join("kim_sessions");
        stream_agentic_request(
            root,
            &fake,
            "do X",
            "ollama",
            &session_dir,
            None,
            Some(pid_slot.clone()),
            tx,
        )
        .await;

        assert!(
            pid_slot.lock().unwrap().is_some(),
            "the agentic child's pid must be recorded so Ctrl-C can SIGTERM it before SIGKILL"
        );
    }

    /// F-E-4 (mirror): a SUCCEEDED run is delivered as a normal TextChunk, so
    /// the happy path still exits 0.
    #[cfg(unix)]
    #[tokio::test]
    async fn succeeded_run_is_delivered_as_a_normal_answer() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        let fake = root.join("fake-python.sh");
        std::fs::write(
            &fake,
            "#!/bin/sh\n\
             echo '{\"type\":\"run_done\",\"success\":true}'\n\
             echo '[SUCCESS] all done here'\n",
        )
        .unwrap();
        let mut perm = std::fs::metadata(&fake).unwrap().permissions();
        perm.set_mode(0o755);
        std::fs::set_permissions(&fake, perm).unwrap();

        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();
        let session_dir = root.join("kim_sessions");
        stream_agentic_request(root, &fake, "do X", "ollama", &session_dir, None, None, tx).await;

        let mut events = Vec::new();
        while let Ok(ev) = rx.try_recv() {
            events.push(ev);
        }
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::TextChunk(t) if t.contains("all done here"))),
            "a SUCCESS run must be delivered as a normal answer; events: {events:?}"
        );
        assert!(
            !events.iter().any(|e| matches!(e, AppEvent::Err(_))),
            "a SUCCESS run must not emit an Error; events: {events:?}"
        );
    }
}
