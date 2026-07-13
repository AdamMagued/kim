//! Code mode: run the Codex coding agent as a subprocess — either directly
//! against a local provider (via the responses proxy) or through the Kim
//! browser codex bridge service — and translate its output into `AppEvent`s.

use serde_json::Value;
use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

use super::responses_proxy::{start_responses_proxy, ProxyHandle};
use super::typed_events::emit_typed_kim_event;
use super::{kim_root_or_error, AppEvent, CodexTurnControl};

pub(crate) async fn stream_codex_subprocess(
    config: &KimConfig,
    prompt: &str,
    allow_non_git: bool,
    session_id: &str,
    tx: UnboundedSender<AppEvent>,
    control: Option<CodexTurnControl>,
) {
    use tokio::io::{AsyncWriteExt, BufReader};
    use tokio::process::Command;

    let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("."));
    let is_browser = config.provider.to_ascii_lowercase().starts_with("browser");

    // Finding 1: on the direct-API (local provider) path codex is spawned fresh
    // for each message, so there is no persistent model-side context to compact —
    // and forwarding a "/compact" control token to `codex exec` would send it to
    // the model as a literal coding prompt (the UI meanwhile claims "Compacting…").
    // The browser path already intercepts these tokens in the bridge service
    // (_COMPACT_CONTROL_TASKS); here we handle the direct-API path honestly
    // instead of spawning codex on a control token.
    if !is_browser && is_compact_control_task(prompt) {
        let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(
            "Local code mode starts a fresh Codex run for each message, so there is no \
             accumulated model context to compact here. Nothing was sent to the model.",
        )));
        let _ = tx.send(AppEvent::Done(false));
        return;
    }

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
            // Parity Part 4: stdin is the approval decision channel — the
            // REPL answers native codex approvals with approval_decision
            // JSON lines. The env var tells the service someone is listening
            // (stdin is a pipe, not a TTY, so it can't tell on its own).
            .env("KIM_STDIN_APPROVALS", "1")
            .stdin(std::process::Stdio::piped())
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
        // Gate the sandbox-bypass flag behind an explicit opt-in env var (#1).
        // Passing it unconditionally disabled the Codex approval gate for every
        // CLI user, even those who didn't need it.
        let bypass_sandbox = std::env::var("KIM_CODEX_BYPASS_SANDBOX").as_deref() == Ok("1");
        let cwd_str = cwd.to_string_lossy().into_owned();
        // C3 parity with the Python bridge (`_exec_config_overrides`): route codex
        // at Kim's local responses proxy with `-c` overrides layered on top of the
        // user's REAL codex home instead of pointing CODEX_HOME at a throwaway temp
        // dir. The old temp CODEX_HOME wiped the user's ~/.codex/config.toml, MCP
        // servers, and skills for the run; leaving CODEX_HOME unset lets codex use
        // ~/.codex (or an exported CODEX_HOME), so all of that still applies and
        // rollout files persist.
        let codex_args = build_codex_args_with_proxy(
            prompt,
            &cwd_str,
            bypass_sandbox,
            allow_non_git,
            proxy_port,
            &config.model,
        );
        match Command::new("codex")
            .args(&codex_args)
            .env("OPENAI_API_KEY", "ollama")
            // No CODEX_HOME override: inherit the user's real ~/.codex (or an
            // exported CODEX_HOME) so their config, MCP servers, and skills apply.
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
    // Parity Part 4: expose the child's pid (Ctrl-C sends SIGTERM → the
    // service maps it to turn/interrupt) and pump REPL approval decisions
    // into the child's stdin.
    let mut _decision_pump: Option<tokio::task::JoinHandle<()>> = None;
    if let Some(mut control) = control {
        if let Ok(mut slot) = control.pid_slot.lock() {
            *slot = child.id();
        }
        if let Some(mut stdin) = child.stdin.take() {
            _decision_pump = Some(tokio::spawn(async move {
                while let Some(line) = control.decision_rx.recv().await {
                    let framed = if line.ends_with('\n') {
                        line
                    } else {
                        format!("{line}\n")
                    };
                    if stdin.write_all(framed.as_bytes()).await.is_err() {
                        break;
                    }
                    let _ = stdin.flush().await;
                }
            }));
        }
    }
    // F1: drain stderr CONCURRENTLY with the stdout stream. It was previously
    // read only after stdout EOF + child.wait(); a child logging ≥ ~64 KiB to
    // stderr filled the OS pipe, blocked in write(2), stopped emitting stdout,
    // and the turn hung on the spinner forever.
    let stderr_tail = child.stderr.take().map(super::drain_stderr_tail);
    // F-E-6: a length-capped line reader — one oversized/newline-less line
    // (e.g. a typed event carrying a base64 screenshot, or a corrupt stream)
    // is drained past the cap instead of buffered fully into RAM.
    let mut reader = BufReader::new(stdout);
    let mut had_output = false;
    let mut saw_streamed_answer = false;
    // F9: a read error (invalid UTF-8, I/O failure) must surface, not silently
    // truncate the turn like a clean EOF.
    let mut read_err: Option<String> = None;

    loop {
        match super::read_capped_line(&mut reader, super::SUBPROCESS_LINE_CAP).await {
            Ok(Some(super::CappedLine::Line(line))) => {
                had_output = true;
                process_codex_line_stateful(&line, &tx, is_browser, &mut saw_streamed_answer);
            }
            Ok(Some(super::CappedLine::Truncated(_))) => {
                // F-E-6: a line past the cap is pathological — surface it rather
                // than parsing a truncated fragment.
                had_output = true;
                read_err = Some(format!(
                    "codex output line exceeded {} bytes and was truncated",
                    super::SUBPROCESS_LINE_CAP
                ));
                break;
            }
            Ok(None) => break,
            Err(e) => {
                read_err = Some(e.to_string());
                break;
            }
        }
    }
    if let Some(pump) = _decision_pump.take() {
        pump.abort();
    }
    // On a read error, close our end of the stdout pipe before wait() so a
    // still-writing child can't block forever on a full pipe.
    drop(reader);

    let exit_ok = child.wait().await.map(|s| s.success()).unwrap_or(false);
    if read_err.is_some() || !had_output || !exit_ok {
        let stderr_msg = match stderr_tail {
            Some(handle) => handle.await.unwrap_or_default(),
            None => String::new(),
        };
        if let Some(e) = read_err {
            let extra = if stderr_msg.trim().is_empty() {
                String::new()
            } else {
                format!("\n{}", stderr_msg.trim())
            };
            let _ = tx.send(AppEvent::Err(format!(
                "codex output read error: {e}{extra}"
            )));
        } else if !stderr_msg.trim().is_empty() {
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

/// Newline-terminate a discrete status/thought line. `ThoughtChunk` renders
/// raw (it is designed for token streams), so whole-line events from the codex
/// bridge — status messages, narration, tool output — must carry their own
/// trailing newline or consecutive lines concatenate into one unreadable blob
/// ("✓ Using Codex via browser bridge (browser:chatgpt)codex binary: …").
fn line_chunk(text: &str) -> String {
    if text.ends_with('\n') {
        text.to_string()
    } else {
        format!("{text}\n")
    }
}

/// Stateless wrapper kept for tests and one-shot callers; the streaming loop
/// uses the stateful variant so the final answer isn't printed twice.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn process_codex_line(line: &str, tx: &UnboundedSender<AppEvent>, is_bridge: bool) {
    let mut saw_streamed_answer = false;
    process_codex_line_stateful(line, tx, is_bridge, &mut saw_streamed_answer);
}

pub(crate) fn process_codex_line_stateful(
    line: &str,
    tx: &UnboundedSender<AppEvent>,
    is_bridge: bool,
    saw_streamed_answer: &mut bool,
) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    if is_bridge {
        if let Ok(json) = serde_json::from_str::<Value>(line) {
            // Kim's own status events from codex_bridge_service.
            if json.get("type").and_then(Value::as_str) == Some("status") {
                if let Some(msg) = json.get("message").and_then(Value::as_str) {
                    let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(msg)));
                }
                return;
            }
            // Typed Kim events from the app-server transport (parity Part 4).
            if emit_typed_kim_event(&json, tx, saw_streamed_answer) {
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
            let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(rest)));
        } else if let Some(rest) = line.strip_prefix("[SUCCESS] ") {
            let _ = tx.send(AppEvent::TextChunk(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("[FAILED] ") {
            let _ = tx.send(AppEvent::Err(rest.to_string()));
        } else if let Some(rest) = line.strip_prefix("TASK_COMPLETE:") {
            // Final-answer contract line. On the app-server transport the
            // answer already streamed as assistant_delta events — printing
            // it again would duplicate it.
            if !*saw_streamed_answer {
                let _ = tx.send(AppEvent::TextChunk(format!("{}\n", rest.trim())));
            }
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
                let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(text)));
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
                    let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(&display)));
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
                                let _ = tx.send(AppEvent::ThoughtChunk(line_chunk(&display)));
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

/// Local mirror of main.rs's `is_compact_control_task`: tokens that mean
/// "compact the context" and must never be forwarded to codex as a literal
/// coding prompt. Kept in sync with `_COMPACT_CONTROL_TASKS` in
/// `orchestrator/agent.py` / `codex_bridge_service.py`.
fn is_compact_control_task(task: &str) -> bool {
    matches!(
        task.trim().to_ascii_lowercase().as_str(),
        "/compact" | "compact" | "__kim_compact_context__"
    )
}

/// Strip characters that could break the generated TOML value or inject extra
/// config, matching the Python bridge's `_exec_config_overrides` sanitizer
/// (`[^A-Za-z0-9_\-.:/ ]` removed, capped at 128 chars). Empty → a safe default.
fn sanitize_proxy_model(model: &str) -> String {
    let cleaned: String = model
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | ':' | '/' | ' '))
        .take(128)
        .collect();
    if cleaned.is_empty() {
        "kim-proxy-model".to_string()
    } else {
        cleaned
    }
}

/// `-c key="value"` global overrides that route codex at Kim's local responses
/// proxy (C3). Mirrors the Python bridge's `_exec_config_overrides` and the
/// keys the old `write_codex_config` wrote — but as CLI overrides layered on the
/// user's real codex home, so no throwaway CODEX_HOME is needed.
pub(crate) fn codex_proxy_overrides(proxy_port: u16, model: &str) -> Vec<String> {
    let safe_model = sanitize_proxy_model(model);
    let base_url = format!("http://127.0.0.1:{proxy_port}/v1");
    let pairs: [(&str, &str); 6] = [
        ("model_provider", "kim-proxy"),
        ("model", &safe_model),
        ("model_providers.kim-proxy.name", "Kim Proxy"),
        ("model_providers.kim-proxy.base_url", &base_url),
        ("model_providers.kim-proxy.wire_api", "responses"),
        ("model_providers.kim-proxy.env_key", "OPENAI_API_KEY"),
    ];
    let mut out = Vec::with_capacity(pairs.len() * 2);
    for (key, value) in pairs {
        out.push("-c".to_string());
        // Quoted → parsed as a TOML string (same as the Python bridge).
        out.push(format!("{key}=\"{value}\""));
    }
    out
}

/// `build_codex_args` plus the kim-proxy `-c` routing overrides inserted before
/// the positional `-C <cwd> <prompt>` tail. Used by the local (direct-API) path
/// so codex talks to Kim's responses proxy while still using the user's real
/// codex home.
pub(crate) fn build_codex_args_with_proxy(
    prompt: &str,
    cwd: &str,
    bypass: bool,
    skip_git_check: bool,
    proxy_port: u16,
    model: &str,
) -> Vec<String> {
    let mut args = build_codex_args(prompt, cwd, bypass, skip_git_check);
    let overrides = codex_proxy_overrides(proxy_port, model);
    // Insert before `-C` (the positional tail) so the overrides stay global
    // flags regardless of the bypass / skip-git-check flags in front of them.
    let insert_at = args.iter().position(|a| a == "-C").unwrap_or(args.len());
    for (offset, item) in overrides.into_iter().enumerate() {
        args.insert(insert_at + offset, item);
    }
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
                .any(|e| matches!(e, AppEvent::ThoughtChunk(t) if t.trim_end().ends_with('…'))),
            "expected a truncated ThoughtChunk, got {events:?}"
        );
    }

    // ── Discrete bridge lines must be newline-terminated ─────────────────────
    // ThoughtChunk renders raw (token-stream semantics); without a trailing
    // newline every status/narration line concatenates into one blob:
    //   "✓ Using Codex via browser bridge (browser:chatgpt)codex binary: …
    //    Sandbox permissions changed — starting a fresh browser chat…codex …"

    #[test]
    fn bridge_status_lines_are_newline_terminated() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(
            r#"{"type":"status","message":"✓ Using Codex via browser bridge (browser:chatgpt)"}"#,
            &tx,
            true,
        );
        process_codex_line("[STATUS] codex binary: /opt/homebrew/bin/codex", &tx, true);
        process_codex_line(
            r#"{"type":"reasoning","text":"Saying hello before we jump in."}"#,
            &tx,
            true,
        );
        let events = drain(&mut rx);
        assert_eq!(events.len(), 3, "got {events:?}");
        for e in &events {
            match e {
                AppEvent::ThoughtChunk(t) => assert!(
                    t.ends_with('\n'),
                    "discrete bridge line must end with a newline, got {t:?}"
                ),
                other => panic!("expected ThoughtChunk, got {other:?}"),
            }
        }
    }

    #[test]
    fn line_chunk_does_not_double_newline() {
        assert_eq!(line_chunk("already terminated\n"), "already terminated\n");
        assert_eq!(line_chunk("bare line"), "bare line\n");
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

    // ── Typed Kim events (app-server transport, parity Part 4) ──────────────

    #[test]
    fn typed_approval_request_maps_to_app_event() {
        let line = r#"{"type":"command_approval_request","id":"7","command":"npx playwright install","cwd":"/proj","reason":"needs browsers","risk":"network","network":true,"amendment":[]}"#;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(line, &tx, true);
        let events = drain(&mut rx);
        assert_eq!(events.len(), 1);
        match &events[0] {
            AppEvent::ApprovalRequest {
                id,
                command,
                cwd,
                reason,
                risk,
            } => {
                assert_eq!(id, "7");
                assert_eq!(command, "npx playwright install");
                assert_eq!(cwd, "/proj");
                assert_eq!(reason, "needs browsers");
                assert_eq!(risk, "network");
            }
            other => panic!("expected ApprovalRequest, got {other:?}"),
        }
    }

    #[test]
    fn typed_file_change_approval_summarizes_files() {
        let line = r#"{"type":"file_change_approval_request","id":"3","files":[{"path":"a.rs","kind":"edit"},{"path":"b.rs","kind":"add"}],"reason":"patch"}"#;
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        process_codex_line(line, &tx, true);
        let events = drain(&mut rx);
        match &events[0] {
            AppEvent::ApprovalRequest { command, risk, .. } => {
                assert!(command.contains("a.rs") && command.contains("b.rs"));
                assert_eq!(risk, "files");
            }
            other => panic!("expected ApprovalRequest, got {other:?}"),
        }
    }

    #[test]
    fn typed_stream_events_map_and_lifecycle_is_quiet() {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut saw = false;
        for line in [
            r#"{"type":"assistant_delta","chunk":"Hello"}"#,
            r#"{"type":"reasoning_delta","chunk":"thinking"}"#,
            r#"{"type":"command_output","item_id":"i1","chunk":"ls output"}"#,
            r#"{"type":"plan_update","steps":[{"step":"write file","status":"inProgress"}]}"#,
            r#"{"type":"diff_update","unified_diff":"+++ b/a\n+x"}"#,
            r#"{"type":"token_usage","input":10,"output":5,"total":15}"#,
            r#"{"type":"turn_lifecycle","phase":"completed","turn_id":"t1"}"#,
            r#"{"type":"item_lifecycle","item_id":"i1","kind":"commandExecution","phase":"started","title":"touch x"}"#,
            r#"{"type":"item_lifecycle","item_id":"m1","kind":"agentMessage","phase":"completed","title":""}"#,
        ] {
            process_codex_line_stateful(line, &tx, true, &mut saw);
        }
        let events = drain(&mut rx);
        assert!(matches!(&events[0], AppEvent::TextChunk(t) if t == "Hello"));
        assert!(matches!(&events[1], AppEvent::ThoughtChunk(t) if t == "thinking"));
        assert!(matches!(&events[2], AppEvent::CommandOutput(t) if t == "ls output"));
        assert!(
            matches!(&events[3], AppEvent::PlanUpdate(steps) if steps == &[("write file".to_string(), "inProgress".to_string())])
        );
        assert!(matches!(&events[4], AppEvent::DiffUpdate(d) if d.contains("+x")));
        assert!(matches!(
            &events[5],
            AppEvent::TokenUsage {
                input: 10,
                output: 5
            }
        ));
        assert!(matches!(&events[6], AppEvent::TurnPhase(p) if p == "completed"));
        assert!(
            matches!(&events[7], AppEvent::ToolEvent { verb, target } if verb == "Running" && target == "touch x")
        );
        // agentMessage lifecycle is internal — nothing after the ToolEvent.
        assert_eq!(events.len(), 8);
    }

    #[test]
    fn answer_and_task_complete_dedupe_against_streamed_deltas() {
        // With streamed deltas: both the typed answer event and the raw
        // TASK_COMPLETE line are swallowed.
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut saw = false;
        process_codex_line_stateful(
            r#"{"type":"assistant_delta","chunk":"Built pong."}"#,
            &tx,
            true,
            &mut saw,
        );
        process_codex_line_stateful(
            r#"{"type":"answer","text":"Built pong."}"#,
            &tx,
            true,
            &mut saw,
        );
        process_codex_line_stateful("TASK_COMPLETE: Built pong.", &tx, true, &mut saw);
        let events = drain(&mut rx);
        let texts: Vec<_> = events
            .iter()
            .filter(|e| matches!(e, AppEvent::TextChunk(_)))
            .collect();
        assert_eq!(texts.len(), 1, "answer must not repeat: {events:?}");

        // Without deltas (exec path / compact task): the answer text shows once.
        let (tx2, mut rx2) = tokio::sync::mpsc::unbounded_channel();
        let mut saw2 = false;
        process_codex_line_stateful("TASK_COMPLETE: Context compacted.", &tx2, true, &mut saw2);
        let events2 = drain(&mut rx2);
        assert!(
            matches!(&events2[0], AppEvent::TextChunk(t) if t.contains("Context compacted")),
            "got {events2:?}"
        );
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

    // ── Finding 1: compact control tokens never reach codex as a prompt ──────

    #[test]
    fn compact_control_tasks_are_recognized() {
        for t in [
            "/compact",
            "compact",
            "  /COMPACT  ",
            "__kim_compact_context__",
        ] {
            assert!(
                is_compact_control_task(t),
                "{t:?} should be a compact token"
            );
        }
        for t in ["compact the report", "/compactify", "hello", "/comp"] {
            assert!(
                !is_compact_control_task(t),
                "{t:?} must not be a compact token"
            );
        }
    }

    #[tokio::test]
    async fn direct_api_compact_does_not_spawn_codex() {
        // Local (non-browser) provider + a compact token: the turn must report
        // honestly and finish WITHOUT sending "/compact" to the model. Reaching
        // codex/proxy spawn would hang or error here (no codex binary in CI), so
        // a clean Done(false) proves the token was intercepted first.
        let config = crate::config::KimConfig::default(); // provider = "ollama"
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        stream_codex_subprocess(&config, "/compact", false, "sess", tx, None).await;
        let events = drain(&mut rx);
        assert!(
            events.iter().any(
                |e| matches!(e, AppEvent::ThoughtChunk(t) if t.to_lowercase().contains("compact"))
            ),
            "expected an honest compact note, got {events:?}"
        );
        assert!(
            events.iter().any(|e| matches!(e, AppEvent::Done(false))),
            "expected Done(false), got {events:?}"
        );
        assert!(
            !events.iter().any(|e| matches!(e, AppEvent::Err(_))),
            "compact intercept must not error, got {events:?}"
        );
    }

    // ── Finding 3 (C3): local codex routes via -c overrides on the real home ─

    #[test]
    fn proxy_overrides_route_at_kim_proxy_and_keep_positional_tail() {
        let args = build_codex_args_with_proxy(
            "fix bug",
            "/proj",
            false,
            false,
            45999,
            "qwen2.5-coder:7b",
        );
        let joined = args.join(" ");
        assert!(joined.contains(r#"model_provider="kim-proxy""#), "{joined}");
        assert!(
            joined.contains(r#"model_providers.kim-proxy.base_url="http://127.0.0.1:45999/v1""#),
            "{joined}"
        );
        assert!(
            joined.contains(r#"model_providers.kim-proxy.wire_api="responses""#),
            "{joined}"
        );
        assert!(
            joined.contains(r#"model_providers.kim-proxy.env_key="OPENAI_API_KEY""#),
            "{joined}"
        );
        assert!(joined.contains(r#"model="qwen2.5-coder:7b""#), "{joined}");
        // Positional tail preserved: … -C /proj "fix bug".
        let n = args.len();
        assert_eq!(args[n - 3], "-C");
        assert_eq!(args[n - 2], "/proj");
        assert_eq!(args[n - 1], "fix bug");
        // Overrides sit before the -C tail (they are global flags).
        let dash_c = args.iter().position(|a| a == "-C").unwrap();
        assert!(args[..dash_c].iter().any(|a| a == "-c"), "{joined}");
    }

    #[test]
    fn proxy_overrides_sanitize_toml_breaking_model_names() {
        // A quote / backslash / newline / semicolon in the model name must be
        // stripped so it cannot break out of the TOML string or inject config.
        let args = build_codex_args_with_proxy("hi", "/p", false, false, 1, "lla\"ma; x=\ny\\z");
        let joined = args.join(" ");
        assert_eq!(
            joined.matches('"').count() % 2,
            0,
            "unbalanced TOML quotes: {joined}"
        );
        let model_arg = args
            .iter()
            .find(|a| a.starts_with("model="))
            .expect("model override present");
        for bad in ['"', '\\', '\n', ';', '='] {
            // (the leading `model=` `=` is fine — check the value only)
            let value = &model_arg["model=".len()..];
            assert!(
                !value.trim_matches('"').contains(bad),
                "sanitized model still contains {bad:?}: {model_arg}"
            );
        }
    }

    #[test]
    fn proxy_overrides_empty_model_falls_back() {
        let args = build_codex_args_with_proxy("hi", "/p", false, false, 1, "");
        assert!(args.join(" ").contains(r#"model="kim-proxy-model""#));
    }
}
