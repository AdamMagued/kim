//! Turn-level behavior shared by the REPL and one-shot paths: the code-mode
//! provider gate, the non-git-repo confirmation, `stream_repl_turn` itself
//! (spawn the right backend + consume its events), the elapsed-time
//! formatter, provider-readiness checks, and conversation compaction. Split
//! out of the former `main.rs`/`lib.rs` god-file (see `app.rs` for the `App`
//! struct and `repl.rs` for the interactive loop) — pure relocation, no
//! behavior changes.

use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::app::{save_current_session, App, AppMode, MessageRole, UiMessage, ViewState};
use crate::config::KimConfig;
use crate::provider::{self, provider_info, stream_kim_request, AppEvent, CodexTurnControl};

/// F-E-14: which providers may run in code mode. Codex only has two backends
/// here: the local `codex` binary talking to ollama through the responses proxy
/// (`ollama` / `ollama-cloud` / empty→ollama), and the codex browser bridge
/// (`browser` / `browser:<site>`). Every OTHER provider (openai, claude, gemini,
/// deepseek, desktop) used to fall through the `== "openai"`-only gate into the
/// local-codex branch, which unconditionally pointed codex at
/// `config.ollama_base_url` with `config.model` — e.g. `claude-sonnet-4-6` sent
/// to an ollama endpoint, which 404s with a misleading "check that ollama is
/// running" error and no hint that the provider choice was ignored.
///
/// Returns `Some(reason)` for a disallowed provider, `None` when code mode may
/// proceed. Mirrors the scheduled-runner allowlist
/// (`orchestrator/scheduled_runner.py::is_allowed_provider`).
pub(crate) fn code_mode_denied_reason(provider: &str) -> Option<String> {
    let p = provider.trim().to_ascii_lowercase();
    if p.is_empty()
        || p == "ollama"
        || p == "ollama-cloud"
        || crate::provider::is_browser_provider(&p)
    {
        return None;
    }
    Some(format!(
        "Code mode does not support the '{provider}' provider — it runs only on ollama \
         or a browser provider. Switch first: /provider ollama  (or e.g. /provider browser:chatgpt)."
    ))
}

/// Control tasks that never spawn Codex (they compact the browser thread), so
/// they must skip the git-repo confirmation. Mirrors `_COMPACT_CONTROL_TASKS`
/// in orchestrator/codex_bridge_service.py.
fn is_compact_control_task(task: &str) -> bool {
    matches!(
        task.trim().to_ascii_lowercase().as_str(),
        "/compact" | "compact" | "__kim_compact_context__"
    )
}

/// Interactive y/N: confirm running Codex in a directory that is not a git repo.
/// Returns true only on an explicit yes; a non-tty / EOF / read error is "no".
fn confirm_run_outside_git_repo(cwd: &Path) -> bool {
    use std::io::{self, IsTerminal, Write};
    // F5: when stdin is not a terminal (piped/one-shot), reading here would
    // steal the next queued prompt line as the y/N answer (or hit EOF).
    // Auto-decline instead of consuming input that isn't an answer.
    if !io::stdin().is_terminal() {
        eprintln!(
            "⚠  {} is not a git repository and stdin is not a terminal — \
             declining the code-mode run. Run from a git repo, `git init` first, \
             or use an interactive terminal to confirm.",
            cwd.display()
        );
        return false;
    }
    print!(
        "⚠  {} is not a git repository.\n   \
         Codex can't track or undo its edits here. Run anyway? [y/N] ",
        cwd.display()
    );
    let _ = io::stdout().flush();
    let mut input = String::new();
    if io::stdin().read_line(&mut input).is_err() {
        return false;
    }
    matches!(input.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

pub(crate) async fn stream_repl_turn(
    app: &mut App,
    prompt: String,
) -> Result<bool, Box<dyn std::error::Error>> {
    // Code mode runs Codex, which refuses to operate outside a git repository
    // (so its edits stay trackable/undoable). Rather than fail hard, confirm
    // once per session before running in a non-git directory. Compact control
    // tasks never reach Codex, so they skip this.
    if app.mode == AppMode::Code && !is_compact_control_task(&prompt) && !app.allow_non_git_codex {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        if !crate::provider::is_git_repo(&cwd) {
            if confirm_run_outside_git_repo(&cwd) {
                app.allow_non_git_codex = true;
            } else {
                println!(
                    "Cancelled. Codex needs a git repo — run Kim from inside one, \
                     or `git init` here first."
                );
                // F5: record the cancellation so one-shot mode exits non-zero.
                // Previously nothing was pushed, `run_oneshot`'s last-message-is-
                // Error check never fired, and scripts saw a bogus exit 0.
                app.push(
                    MessageRole::Error,
                    format!(
                        "Cancelled: {} is not a git repository; the code-mode run was declined.",
                        cwd.display()
                    ),
                );
                return Ok(false);
            }
        }
    }

    app.view = ViewState::InChat;
    app.push(MessageRole::User, prompt.clone());
    // Persist the user turn up front so the session file exists even if the
    // request errors or is interrupted, and so resumed chats keep their history.
    // (A1/A2 — the old `is_local_agent` reload-from-file branch was vestigial:
    // nothing writes that file mid-stream, so it wiped state every turn.)
    save_current_session(app);

    let history = app.chat_history();
    let config = app.config.clone();
    let code_mode = app.mode == AppMode::Code;
    let allow_non_git = app.allow_non_git_codex;
    let session_id = app.current_session_id.clone();
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<AppEvent>();

    // P7: in chat mode, run the REAL Kim agent (tool loop) when a Kim source root
    // + Python are available; otherwise fall back to plain LLM chat with a note.
    let agentic = if code_mode {
        None
    } else if provider::is_browser_provider(&config.provider) {
        // Chat-mode browser provider: if the Kim desktop bridge is already
        // running, reuse it (keeps the in-app webview experience and avoids a
        // second Chrome). Otherwise drive ChatGPT/Gemini/Claude directly via the
        // local Playwright agent — same mechanism code mode uses, so a bare
        // `kim` works without launching the desktop app.
        if provider::bridge_is_available(&config.desktop_bridge_url).await {
            None
        } else {
            crate::agentic::agentic_available(&config.provider)
        }
    } else {
        crate::agentic::agentic_available(&config.provider)
    };
    // Ctrl-C → SIGTERM (graceful) before the hard kill. The pid slot is shared
    // by anything that spawns a killable child this turn:
    //   - code mode's codex child (via CodexTurnControl.pid_slot), and
    //   - F-E-5: the chat-mode agentic child (orchestrator.agent), directly and
    //     via the browser-provider TOCTOU fallback inside stream_kim_request.
    // Populated iff a child could be spawned; the HTTP bridge path leaves it
    // None (there is no local child to signal).
    let child_pid: Option<std::sync::Arc<std::sync::Mutex<Option<u32>>>> =
        if code_mode || agentic.is_some() || provider::is_browser_provider(&config.provider) {
            Some(std::sync::Arc::new(std::sync::Mutex::new(None::<u32>)))
        } else {
            None
        };
    // Parity Part 4: code-mode turns also get a decision channel (REPL → child
    // stdin, for native codex approvals).
    let (codex_control, decision_tx) = if code_mode {
        let (dtx, drx) = tokio::sync::mpsc::unbounded_channel::<String>();
        (
            Some(CodexTurnControl {
                decision_rx: drx,
                pid_slot: child_pid
                    .clone()
                    .expect("code_mode implies a pid slot was created above"),
            }),
            Some(dtx),
        )
    } else {
        (None, None)
    };

    let handle = if let Some((root, python)) = agentic {
        let prompt2 = prompt.clone();
        let sid = session_id.clone();
        let provider = config.provider.clone();
        let pid_slot = child_pid.clone();
        tokio::spawn(async move {
            let session_dir = root.join("kim_sessions");
            crate::agentic::stream_agentic_request(
                &root,
                &python,
                &prompt2,
                &provider,
                &session_dir,
                Some(&sid),
                pid_slot,
                tx,
            )
            .await;
        })
    } else {
        crate::repl::maybe_note_plain_chat(code_mode, &config.provider);
        let pid_slot = child_pid.clone();
        tokio::spawn(async move {
            stream_kim_request(
                &config,
                &history,
                code_mode,
                &session_id,
                allow_non_git,
                tx,
                codex_control,
                pid_slot,
            )
            .await;
        })
    };

    // A6: Ctrl-C cancels the current generation instead of killing the CLI.
    // tokio::signal::ctrl_c fires only while we await here (between turns,
    // rustyline owns the prompt and its own Ctrl-C handling resumes).
    let cancel = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    let result = crate::repl_turn::consume_turn_events(
        app,
        rx,
        std::time::Instant::now(),
        save_current_session,
        cancel,
        decision_tx,
        child_pid,
    )
    .await;
    // Reap the request task (kill_on_drop reaps any child subprocess). No-op if
    // it already finished.
    handle.abort();
    result
}

pub(crate) fn format_repl_elapsed(duration: Duration) -> String {
    let secs = duration.as_secs();
    if secs < 60 {
        format!("{secs}s")
    } else {
        format!("{}m {:02}s", secs / 60, secs % 60)
    }
}

pub(crate) fn provider_is_ready(config: &KimConfig) -> bool {
    provider_is_ready_with_env(config, |key| std::env::var(key).ok())
}

fn provider_is_ready_with_env<F>(config: &KimConfig, env_var: F) -> bool
where
    F: Fn(&str) -> Option<String>,
{
    let p = config.provider.as_str();
    if p == "ollama" || p == "desktop" || provider::is_browser_provider(p) {
        return true;
    }
    if config
        .api_keys
        .get(p)
        .map(|key| !key.trim().is_empty())
        .unwrap_or(false)
    {
        return true;
    }
    provider_info(p)
        .and_then(|info| info.key_env)
        .and_then(env_var)
        .map(|key| !key.trim().is_empty())
        .unwrap_or(false)
}

pub(crate) fn compact_app_messages(app: &mut App) {
    let preserve = 6usize;
    let exchange_indices: Vec<usize> = app
        .messages
        .iter()
        .enumerate()
        .filter(|(_, m)| matches!(m.role, MessageRole::User | MessageRole::Assistant))
        .map(|(i, _)| i)
        .collect();

    if exchange_indices.len() <= preserve {
        app.push(
            MessageRole::System,
            "Nothing to compact yet; keeping the current conversation as-is.",
        );
        return;
    }

    let cut_at = exchange_indices[exchange_indices.len() - preserve];
    let removed = app
        .messages
        .drain(..cut_at)
        .filter(|m| matches!(m.role, MessageRole::User | MessageRole::Assistant))
        .count();
    app.messages.insert(
        0,
        UiMessage {
            role: MessageRole::System,
            content: format!("Earlier context compacted — {removed} message(s) removed."),
            timestamp_ms: Some(UiMessage::now_ms()),
        },
    );
    app.push(
        MessageRole::System,
        format!("Compacted {removed} older message(s)."),
    );
}

#[cfg(test)]
mod tests {
    use super::code_mode_denied_reason;
    use crate::config::KimConfig;
    use crate::turn::{provider_is_ready, provider_is_ready_with_env};

    // ── F-E-14: code-mode provider gate ──────────────────────────────────────

    #[test]
    fn code_mode_allows_only_ollama_and_browser() {
        // Allowed: the two real codex backends (+ empty → ollama).
        for ok in [
            "ollama",
            "ollama-cloud",
            "",
            "browser",
            "browser:chatgpt",
            "BROWSER:Gemini",
        ] {
            assert!(
                code_mode_denied_reason(ok).is_none(),
                "code mode must allow {ok:?}"
            );
        }
        // Rejected: everything the old `== \"openai\"` gate let silently route to
        // ollama with a non-ollama model name.
        for bad in ["openai", "claude", "gemini", "deepseek", "desktop"] {
            let reason = code_mode_denied_reason(bad)
                .unwrap_or_else(|| panic!("code mode must reject {bad:?}"));
            assert!(
                reason.contains(bad) && reason.contains("ollama"),
                "rejection for {bad:?} should name the provider and the fix; got: {reason}"
            );
        }
    }

    #[test]
    fn browser_providers_are_ready_without_api_key() {
        for name in &[
            "browser",
            "browser:chatgpt",
            "browser:gemini",
            "browser:deepseek",
        ] {
            let config = KimConfig {
                provider: name.to_string(),
                ..KimConfig::default()
            };
            assert!(
                provider_is_ready(&config),
                "{name} should be ready without a key"
            );
        }
    }

    #[test]
    fn api_key_providers_require_key_to_be_ready() {
        for name in &["claude", "openai", "gemini", "deepseek"] {
            let config = KimConfig {
                provider: name.to_string(),
                ..KimConfig::default()
            };
            assert!(
                !provider_is_ready(&config),
                "{name} should not be ready without a key"
            );
        }
    }

    #[test]
    fn api_key_provider_is_ready_with_non_empty_env_key() {
        let config = KimConfig {
            provider: "claude".to_string(),
            ..KimConfig::default()
        };
        assert!(provider_is_ready_with_env(&config, |name| {
            (name == "ANTHROPIC_API_KEY").then(|| "sk-env".to_string())
        }));
    }

    #[test]
    fn api_key_provider_is_not_ready_with_blank_env_key() {
        let config = KimConfig {
            provider: "openai".to_string(),
            ..KimConfig::default()
        };
        assert!(!provider_is_ready_with_env(&config, |name| {
            (name == "OPENAI_API_KEY").then(|| "   ".to_string())
        }));
    }

    #[test]
    fn api_key_provider_is_not_ready_with_blank_saved_key() {
        let mut config = KimConfig {
            provider: "gemini".to_string(),
            ..KimConfig::default()
        };
        config
            .api_keys
            .insert("gemini".to_string(), " \n ".to_string());
        assert!(!provider_is_ready_with_env(&config, |_| None));
    }
}
