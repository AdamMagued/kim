//! Code mode: run the Codex coding agent as a subprocess — either directly
//! against a local provider (via the responses proxy) or through the Kim
//! browser codex bridge service — and translate its output into `AppEvent`s.

use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

use super::responses_proxy::{start_responses_proxy, write_codex_config, ProxyHandle};
use super::{kim_root_or_error, AppEvent};

pub(crate) async fn stream_codex_subprocess(
    config: &KimConfig,
    prompt: &str,
    allow_non_git: bool,
    session_id: &str,
    tx: UnboundedSender<AppEvent>,
) {
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let is_browser = config.provider.to_ascii_lowercase().starts_with("browser");

    // Holds the exclusive temp dir for the codex CODEX_HOME so it outlives the
    // if-else block and stays alive until child.wait() completes (#23).
    let mut _codex_temp_dir: Option<tempfile::TempDir> = None;
    // Holds the responses-proxy (local provider only) for the whole run; drop at
    // function exit kills it and removes its script (#35).
    let mut _proxy_handle: Option<ProxyHandle> = None;

    let mut child = if is_browser {
        // Browser provider: launch the Kim codex bridge service.
        // Resolve the Kim source root so `python3 -m orchestrator.codex_bridge_service`
        // works from any user cwd. Mirrors the desktop subprocess environment.
        let kim_root = match kim_root_or_error(crate::sessions::find_kim_repo_root()) {
            Ok(r) => r,
            Err(msg) => {
                let _ = tx.send(AppEvent::Err(msg));
                return;
            }
        };
        let python = match crate::agentic::find_python(&kim_root) {
            Some(p) => p,
            None => {
                let _ = tx.send(AppEvent::Err(
                    "No Python interpreter found (tried venv, python3, python). \
                     Install Python 3 and retry."
                        .to_string(),
                ));
                return;
            }
        };
        match Command::new(&python)
            .args([
                "-m",
                "orchestrator.codex_bridge_service",
                "--task",
                prompt,
                "--cwd",
                &cwd.to_string_lossy(),
                "--provider",
                &config.provider,
            ])
            .current_dir(&kim_root)
            .env("PYTHONPATH", &kim_root)
            .env("PROJECT_ROOT", &kim_root)
            // Owning CLI session id. Stateful browser threads are keyed on
            // (cwd, provider) on disk so per-message bridge spawns share them;
            // this lets the bridge tell "next message, same session" from "user
            // reopened kim" and start a fresh browser chat for a new session.
            .env("KIM_CLI_SESSION_ID", session_id)
            // Signal the bridge that the user already confirmed running Codex
            // outside a git repo (see the y/N prompt in stream_repl_turn). Only
            // set when approved so the bridge's own gate stays in force otherwise.
            .env(
                "KIM_CODEX_SKIP_GIT_CHECK",
                if allow_non_git { "1" } else { "0" },
            )
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!("Failed to start codex bridge: {e}")));
                return;
            }
        }
    } else {
        // Local provider: start a Responses-API→Chat-Completions proxy so codex
        // can talk to ollama (which only speaks Chat Completions).
        // Keep the handle alive for the whole codex run; it kills the proxy and
        // deletes its temp script on drop at function exit (#35).
        let proxy = match start_responses_proxy(config, &tx).await {
            Some(p) => p,
            None => return,
        };
        let proxy_port = proxy.port;
        _proxy_handle = Some(proxy);
        // Use an exclusive randomized temp dir so concurrent runs don't clobber
        // each other and the path is not pre-creatable by a local attacker (#23).
        let kim_codex_dir = match tempfile::Builder::new().prefix("kim_codex_").tempdir() {
            Ok(d) => d,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!(
                    "Failed to create codex temp dir: {e}"
                )));
                return;
            }
        };
        let kim_codex_home = kim_codex_dir.path().to_path_buf();
        // Keep the TempDir alive until child.wait() finishes (#23).
        _codex_temp_dir = Some(kim_codex_dir);
        if let Err(e) = write_codex_config(proxy_port, &config.model, &kim_codex_home) {
            let _ = tx.send(AppEvent::Err(format!("Failed to write codex config: {e}")));
            return;
        }
        // Gate the sandbox-bypass flag behind an explicit opt-in env var (#1).
        // Passing it unconditionally disabled the Codex approval gate for every
        // CLI user, even those who didn't need it.
        let bypass_sandbox = std::env::var("KIM_CODEX_BYPASS_SANDBOX").as_deref() == Ok("1");
        let cwd_str = cwd.to_string_lossy().into_owned();
        let codex_args = build_codex_args(prompt, &cwd_str, bypass_sandbox, allow_non_git);
        match Command::new("codex")
            .args(&codex_args)
            .env("OPENAI_API_KEY", "ollama")
            .env("CODEX_HOME", &kim_codex_home)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(AppEvent::Err(format!(
                    "Failed to start codex: {e}. Install with: npm install -g @openai/codex"
                )));
                return;
            }
        }
    };

    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err("Failed to capture codex stdout.".to_string()));
            return;
        }
    };
    let stderr_pipe = child.stderr.take();
    let mut lines = BufReader::new(stdout).lines();
    let mut had_output = false;

    while let Ok(Some(line)) = lines.next_line().await {
        had_output = true;
        process_codex_line(&line, &tx, is_browser);
    }

    let exit_ok = child.wait().await.map(|s| s.success()).unwrap_or(false);
    if !had_output || !exit_ok {
        let mut stderr_msg = String::new();
        if let Some(pipe) = stderr_pipe {
            let mut err_lines = BufReader::new(pipe).lines();
            while let Ok(Some(line)) = err_lines.next_line().await {
                if !stderr_msg.is_empty() {
                    stderr_msg.push('\n');
                }
                stderr_msg.push_str(line.trim());
            }
        }
        if !stderr_msg.trim().is_empty() {
            let _ = tx.send(AppEvent::Err(format!("codex: {}", stderr_msg.trim())));
        } else if !had_output {
            let _ = tx.send(AppEvent::Err(
                "codex produced no output. Check that ollama is running and the model name is correct.".to_string(),
            ));
        } else if !exit_ok {
            let _ = tx.send(AppEvent::Err(
                "codex exited with a non-zero status.".to_string(),
            ));
        }
        return;
    }
    // used_bridge only when codex ran against a browser provider via the Kim bridge
    // service; local codex (ollama) is not "via Kim desktop".
    let _ = tx.send(AppEvent::Done(is_browser));
}

pub(crate) fn process_codex_line(line: &str, tx: &UnboundedSender<AppEvent>, is_bridge: bool) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    if is_bridge {
        if let Ok(json) = serde_json::from_str::<Value>(line) {
            // Kim's own status events from codex_bridge_service.
            if json.get("type").and_then(Value::as_str) == Some("status") {
                if let Some(msg) = json.get("message").and_then(Value::as_str) {
                    let _ = tx.send(AppEvent::ThoughtChunk(msg.to_string()));
                }
                return;
            }
            // Otherwise these are Codex's raw --json protocol events, forwarded
            // verbatim by the bridge. Route them through the shared handler so
            // lifecycle noise (thread.started/turn.started/turn.completed) is
            // dropped instead of leaking to the user as raw JSON.
            emit_codex_json_event(&json, tx);
            return;
        }
        // Legacy bracket prefix format / plain assistant text.
        if let Some(rest) = line.strip_prefix("[STATUS] ") {
            let _ = tx.send(AppEvent::ThoughtChunk(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("[SUCCESS] ") {
            let _ = tx.send(AppEvent::TextChunk(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("[FAILED] ") {
            let _ = tx.send(AppEvent::Err(rest.to_string()));
        } else {
            let _ = tx.send(AppEvent::TextChunk(format!("{line}\n")));
        }
        return;
    }
    // Direct Codex JSON-stream format.
    let Ok(json) = serde_json::from_str::<Value>(line) else {
        let _ = tx.send(AppEvent::TextChunk(format!("{line}\n")));
        return;
    };
    emit_codex_json_event(&json, tx);
}

/// Translate a single Codex `--json` event into an `AppEvent`. Shared by the
/// direct-codex path and the browser-bridge path (which forwards Codex's raw
/// events). Unknown events surface only their human-readable `text` (if any) —
/// never the raw JSON envelope — so lifecycle events like `thread.started`,
/// `turn.started`, and `turn.completed` are dropped rather than leaked.
fn emit_codex_json_event(json: &Value, tx: &UnboundedSender<AppEvent>) {
    match json.get("type").and_then(Value::as_str) {
        Some("message") => {
            if let Some(blocks) = json.get("content").and_then(Value::as_array) {
                for block in blocks {
                    if block.get("type").and_then(Value::as_str) == Some("text") {
                        if let Some(text) = block.get("text").and_then(Value::as_str) {
                            if !text.is_empty() {
                                let _ = tx.send(AppEvent::TextChunk(text.to_string()));
                            }
                        }
                    }
                }
            }
        }
        Some("reasoning") => {
            let text = json
                .get("summary")
                .and_then(Value::as_array)
                .and_then(|a| a.first())
                .and_then(|v| v.get("text"))
                .and_then(Value::as_str)
                .or_else(|| json.get("text").and_then(Value::as_str))
                .unwrap_or_default();
            if !text.is_empty() {
                let _ = tx.send(AppEvent::ThoughtChunk(text.to_string()));
            }
        }
        Some("function_call") => {
            let name = json.get("name").and_then(Value::as_str).unwrap_or("tool");
            let _ = tx.send(AppEvent::ToolEvent {
                verb: "Running".to_string(),
                target: name.to_string(),
            });
        }
        Some("function_call_output") => {
            if let Some(output) = json.get("output").and_then(Value::as_str) {
                let trimmed = output.trim();
                if !trimmed.is_empty() {
                    // char-boundary-safe truncation — byte slicing panics mid-UTF-8 (A5)
                    let display = crate::sessions::truncate(trimmed, 300);
                    let _ = tx.send(AppEvent::ThoughtChunk(display));
                }
            }
        }
        Some("item.completed") => {
            if let Some(item) = json.get("item") {
                match item.get("type").and_then(Value::as_str) {
                    Some("agent_message") | Some("assistant_message") => {
                        if let Some(text) = item.get("text").and_then(Value::as_str) {
                            if !text.is_empty() {
                                let _ = tx.send(AppEvent::TextChunk(text.to_string()));
                            }
                        }
                    }
                    Some("function_call") => {
                        let name = item.get("name").and_then(Value::as_str).unwrap_or("tool");
                        let _ = tx.send(AppEvent::ToolEvent {
                            verb: "Running".to_string(),
                            target: name.to_string(),
                        });
                    }
                    Some("function_call_output") => {
                        if let Some(output) = item.get("output").and_then(Value::as_str) {
                            let trimmed = output.trim();
                            if !trimmed.is_empty() {
                                // char-boundary-safe truncation (A5)
                                let display = crate::sessions::truncate(trimmed, 300);
                                let _ = tx.send(AppEvent::ThoughtChunk(display));
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        Some("error") => {
            let msg = json
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("codex error");
            if !msg.contains("Reconnecting") && !msg.contains("stream disconnected") {
                let _ = tx.send(AppEvent::Err(msg.to_string()));
            }
        }
        _ => {
            // Unknown event: surface any human-readable text it carries (some
            // Codex schema versions wrap the answer differently), but never the
            // raw JSON envelope. Text-less lifecycle events fall through and are dropped.
            let text = json.get("text").and_then(Value::as_str).or_else(|| {
                json.get("item")
                    .and_then(|i| i.get("text"))
                    .and_then(Value::as_str)
            });
            if let Some(text) = text {
                if !text.is_empty() {
                    let _ = tx.send(AppEvent::TextChunk(text.to_string()));
                }
            }
        }
    }
}

/// Build the argv list passed to `codex exec`. Extracted for testability (#1).
/// Always produces `["exec", "--json", …, "-C", cwd, prompt]`.
/// The `--dangerously-bypass-approvals-and-sandbox` flag is inserted only when
/// `bypass` is true (opt-in via `KIM_CODEX_BYPASS_SANDBOX=1`).
/// `--skip-git-repo-check` is inserted only when `skip_git_check` is true (the
/// user confirmed running Codex outside a git repo).
pub(crate) fn build_codex_args(
    prompt: &str,
    cwd: &str,
    bypass: bool,
    skip_git_check: bool,
) -> Vec<String> {
    let mut args: Vec<String> = vec!["exec".into(), "--json".into()];
    if bypass {
        args.push("--dangerously-bypass-approvals-and-sandbox".into());
    }
    if skip_git_check {
        args.push("--skip-git-repo-check".into());
    }
    args.push("-C".into());
    args.push(cwd.to_string());
    args.push(prompt.to_string());
    args
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

    // ── A5: char-boundary-safe truncation of tool output ────────────────────

    #[test]
    fn function_call_output_truncation_is_char_safe() {
        // Byte 300 lands mid-emoji; byte slicing `&trimmed[..300]` would panic.
        let mut payload = "a".repeat(299);
        payload.push('🦀'); // 4-byte char straddling the 300-byte boundary
        payload.push_str(&"b".repeat(50));
        let line =
            serde_json::json!({"type": "function_call_output", "output": payload}).to_string();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        // Must not panic.
        process_codex_line(&line, &tx, false);
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t.ends_with('…'))),
            "expected a truncated ThoughtChunk, got {events:?}"
        );
    }

    // ── Codex lifecycle events must not leak as raw JSON (bridge path) ───────

    #[test]
    fn bridge_drops_codex_lifecycle_events() {
        // These are Codex's --json protocol envelopes forwarded by the bridge.
        // None of them should reach the user as text.
        for line in [
            r#"{"type":"thread.started","thread_id":"abc"}"#,
            r#"{"type":"turn.started"}"#,
            r#"{"type":"turn.completed","usage":{"input_tokens":0}}"#,
            r#"{"type":"item.started","item":{"type":"agent_message"}}"#,
        ] {
            let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
            process_codex_line(line, &tx, true);
            let events = drain(&mut rx);
            assert!(
                events.is_empty(),
                "lifecycle event should be dropped, got {events:?} for {line}"
            );
        }
    }

    #[test]
    fn bridge_renders_agent_message_as_text_not_json() {
        let line = r#"{"type":"item.completed","item":{"type":"agent_message","text":"Hi there"}}"#;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(line, &tx, true);
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::TextChunk(t) if t == "Hi there")),
            "agent message should render as clean text, got {events:?}"
        );
        // And nothing should carry a raw JSON brace.
        assert!(
            !events
                .iter()
                .any(|e| matches!(e, AppEvent::TextChunk(t) if t.contains('{'))),
            "no raw JSON should leak, got {events:?}"
        );
    }

    #[test]
    fn bridge_status_event_is_a_thought() {
        let line = r#"{"type":"status","message":"Sending message to gemini…"}"#;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(line, &tx, true);
        let events = drain(&mut rx);
        assert!(
            events
                .iter()
                .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t.contains("Sending"))),
            "status should be a ThoughtChunk, got {events:?}"
        );
    }

    #[test]
    fn bridge_unknown_event_surfaces_text_only() {
        // A future/unknown Codex event that still carries an answer must show the
        // text, never the JSON envelope.
        let line = r#"{"type":"assistant.turn","text":"the answer"}"#;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(line, &tx, true);
        let events = drain(&mut rx);
        assert_eq!(events.len(), 1);
        assert!(
            matches!(&events[0], AppEvent::TextChunk(t) if t == "the answer"),
            "unknown event should surface its text, got {events:?}"
        );
    }

    // ── codex_args_bypass_gated (#1) ─────────────────────────────────────────
    // Regression guard: the sandbox-bypass flag must be gated behind `bypass=true`
    // and must never appear when `bypass=false`. The argv must always terminate
    // with `-C <cwd> <prompt>` regardless of the bypass setting.

    #[test]
    fn codex_args_bypass_gated() {
        let prompt = "fix the bug";
        let cwd = "/home/user/project";

        // bypass=false: the dangerous flag must NOT appear.
        let no_bypass = build_codex_args(prompt, cwd, false, false);
        assert!(
            !no_bypass
                .iter()
                .any(|a| a == "--dangerously-bypass-approvals-and-sandbox"),
            "bypass=false must not include the bypass flag; got {no_bypass:?}"
        );
        // argv must end: -C <cwd> <prompt>
        let n = no_bypass.len();
        assert!(n >= 3, "expected at least 3 args; got {no_bypass:?}");
        assert_eq!(
            &no_bypass[n - 3],
            "-C",
            "second-to-last pair must start with -C"
        );
        assert_eq!(&no_bypass[n - 2], cwd, "cwd must be the penultimate arg");
        assert_eq!(&no_bypass[n - 1], prompt, "prompt must be the last arg");

        // bypass=true: the flag must appear, and argv must still end correctly.
        let with_bypass = build_codex_args(prompt, cwd, true, false);
        assert!(
            with_bypass
                .iter()
                .any(|a| a == "--dangerously-bypass-approvals-and-sandbox"),
            "bypass=true must include the bypass flag; got {with_bypass:?}"
        );
        let n = with_bypass.len();
        assert!(n >= 3);
        assert_eq!(&with_bypass[n - 3], "-C");
        assert_eq!(&with_bypass[n - 2], cwd);
        assert_eq!(&with_bypass[n - 1], prompt);
    }

    #[test]
    fn codex_args_skip_git_check_gated() {
        let prompt = "hi";
        let cwd = "/tmp/not-a-repo";

        // skip_git_check=false: the flag must NOT appear.
        let off = build_codex_args(prompt, cwd, false, false);
        assert!(
            !off.iter().any(|a| a == "--skip-git-repo-check"),
            "skip_git_check=false must not include the flag; got {off:?}"
        );

        // skip_git_check=true: the flag appears, argv still ends -C cwd prompt.
        let on = build_codex_args(prompt, cwd, false, true);
        assert!(
            on.iter().any(|a| a == "--skip-git-repo-check"),
            "skip_git_check=true must include the flag; got {on:?}"
        );
        let n = on.len();
        assert_eq!(&on[n - 3], "-C");
        assert_eq!(&on[n - 2], cwd);
        assert_eq!(&on[n - 1], prompt);
    }
}
