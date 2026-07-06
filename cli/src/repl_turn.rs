//! One REPL turn's event consumption + rendering (extracted from main.rs).
//!
//! Owns the Part-4 code-mode UX: the native codex approval prompt, live
//! command output, plan checklist, diff summary, and the interrupt-first
//! cancel path. Split out so main.rs stays under the file-size gate.

use std::io::{self, IsTerminal, Write};
use std::time::{Duration, Instant};

use crate::provider::AppEvent;
use crate::{
    format_repl_elapsed, kim_accent_color, paint_bold, paint_dim, paint_text, print_message,
    print_note, stdout, App, MessageRole, UiMessage,
};

/// Consume one turn's streamed events, render them, and persist the result.
/// Extracted from `stream_repl_turn` so it can be driven by a stubbed event
/// channel with an injected `save` sink in tests — no network required. (A1/A2)
/// Erase the current terminal line if the loading spinner was drawn on it.
/// No-op when `active` is false so callers can invoke it unconditionally.
fn clear_spinner_line(active: bool) {
    if active {
        print!("\r\x1b[2K");
        let _ = stdout().flush();
    }
}

/// Render one native codex approval request and read the user's decision from
/// the terminal. Pure formatting/mapping helpers are split out for tests.
fn format_approval_prompt(command: &str, cwd: &str, reason: &str, risk: &str) -> String {
    let mut out = String::from("Codex wants to ");
    if risk == "files" {
        out.push_str(&format!("{command}\n"));
    } else {
        out.push_str("run:\n");
        out.push_str(&format!("  $ {command}"));
        if !cwd.is_empty() {
            out.push_str(&format!("    (in {cwd})"));
        }
        out.push('\n');
    }
    if !reason.is_empty() || risk == "network" {
        out.push_str("  ");
        if !reason.is_empty() {
            out.push_str(&format!("reason: {reason}"));
        }
        if risk == "network" {
            out.push_str("   [network access]");
        }
        out.push('\n');
    }
    out.push_str("Allow? [y]es once / [a]lways this session / [n]o: ");
    out
}

/// Map one terminal input line to a decision. Anything unrecognized denies.
fn map_approval_input(input: &str) -> &'static str {
    match input.trim().to_ascii_lowercase().as_str() {
        "y" | "yes" => "accept",
        "a" | "always" => "acceptForSession",
        _ => "decline",
    }
}

/// The stdin decision line the bridge service expects (Part 3 vocabulary).
fn build_decision_line(id: &str, decision: &str) -> String {
    serde_json::json!({
        "type": "approval_decision",
        "id": id,
        "decision": decision,
    })
    .to_string()
}

/// Best-effort SIGTERM so the bridge service can map it to `turn/interrupt`.
/// (tokio's kill_on_drop is SIGKILL — that skips the graceful path.)
fn send_sigterm(pid: u32) {
    #[cfg(unix)]
    {
        let _ = std::process::Command::new("kill")
            .args(["-TERM", &pid.to_string()])
            .status();
    }
    #[cfg(not(unix))]
    {
        let _ = pid; // Windows: no SIGTERM — the kill_on_drop path handles it.
    }
}

#[cfg(test)]
mod approval_prompt_tests {
    use super::*;

    #[test]
    fn approval_prompt_shows_command_cwd_reason_and_network_badge() {
        let prompt = format_approval_prompt(
            "npx playwright install",
            "/home/u/proj",
            "needs browsers",
            "network",
        );
        assert!(prompt.contains("$ npx playwright install"));
        assert!(prompt.contains("(in /home/u/proj)"));
        assert!(prompt.contains("reason: needs browsers"));
        assert!(prompt.contains("[network access]"));
        assert!(prompt.contains("[y]es once / [a]lways this session / [n]o"));
    }

    #[test]
    fn approval_prompt_file_change_shape() {
        let prompt = format_approval_prompt("apply changes to: a.rs, b.rs", "", "", "files");
        assert!(prompt.starts_with("Codex wants to apply changes to: a.rs, b.rs"));
        assert!(!prompt.contains("$ "));
    }

    #[test]
    fn approval_input_mapping_denies_by_default() {
        assert_eq!(map_approval_input("y"), "accept");
        assert_eq!(map_approval_input("YES\n"), "accept");
        assert_eq!(map_approval_input("a"), "acceptForSession");
        assert_eq!(map_approval_input("always"), "acceptForSession");
        assert_eq!(map_approval_input("n"), "decline");
        assert_eq!(map_approval_input(""), "decline");
        assert_eq!(map_approval_input("whatever"), "decline");
    }

    #[test]
    fn decision_line_is_the_part3_stdin_vocabulary() {
        let line = build_decision_line("7", "acceptForSession");
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["type"], "approval_decision");
        assert_eq!(v["id"], "7");
        assert_eq!(v["decision"], "acceptForSession");
    }
}

pub(crate) async fn consume_turn_events<S, C>(
    app: &mut App,
    mut rx: tokio::sync::mpsc::UnboundedReceiver<AppEvent>,
    started: Instant,
    mut save: S,
    cancel: C,
    decision_tx: Option<tokio::sync::mpsc::UnboundedSender<String>>,
    child_pid: Option<std::sync::Arc<std::sync::Mutex<Option<u32>>>>,
) -> Result<bool, Box<dyn std::error::Error>>
where
    S: FnMut(&App),
    C: std::future::Future<Output = ()>,
{
    let mut assistant = String::new();
    let mut printed_answer_label = false;
    let mut printed_thinking = false;
    let mut last_tool_line = String::new();
    let mut bridge_used = false;
    let mut command_output_lines = 0usize;
    let mut command_output_capped = false;

    // Loading indicator: while we wait for the first output (browser providers
    // can take many seconds to scrape a reply), animate a spinner in place.
    const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    let show_spinner = stdout().is_terminal();
    let mut spin_i = 0usize;
    let mut spinner_active = false;
    let mut produced_output = false;
    let mut spinner = tokio::time::interval(Duration::from_millis(100));
    spinner.tick().await; // consume the immediate first tick

    tokio::pin!(cancel);
    loop {
        let event = tokio::select! {
            biased;
            maybe = rx.recv() => match maybe {
                Some(event) => event,
                None => break,
            },
            _ = &mut cancel => {
                // A6: cancel mid-stream — keep whatever already streamed, drop
                // back to the prompt instead of killing the whole CLI.
                clear_spinner_line(spinner_active);
                if printed_answer_label && !assistant.ends_with('\n') {
                    println!();
                }
                // Parity Part 4: interrupt, don't kill. SIGTERM lets the
                // bridge service map it to a graceful turn/interrupt; the
                // caller's handle.abort() (SIGKILL via kill_on_drop) stays
                // the hard fallback after a 5s grace drain.
                let pid = child_pid
                    .as_ref()
                    .and_then(|slot| slot.lock().ok().and_then(|p| *p));
                if let Some(pid) = pid {
                    print_note("⏹ interrupting…");
                    send_sigterm(pid);
                    let grace = tokio::time::sleep(Duration::from_secs(5));
                    tokio::pin!(grace);
                    loop {
                        tokio::select! {
                            maybe = rx.recv() => match maybe {
                                Some(AppEvent::TurnPhase(phase)) if phase == "interrupted" => break,
                                Some(_) => continue,   // drain quietly
                                None => break,          // child exited
                            },
                            _ = &mut grace => break,
                        }
                    }
                }
                print_note("(cancelled)");
                if !assistant.trim().is_empty() {
                    app.push(MessageRole::Assistant, std::mem::take(&mut assistant));
                    save(app);
                }
                return Ok(false);
            }
            _ = spinner.tick(), if show_spinner && !produced_output => {
                let frame = SPINNER[spin_i % SPINNER.len()];
                spin_i += 1;
                print!(
                    "\r{}",
                    paint_dim(&format!(
                        "{frame} thinking… ({}s)",
                        started.elapsed().as_secs()
                    ))
                );
                let _ = stdout().flush();
                spinner_active = true;
                continue;
            }
        };
        // A real event arrived — wipe the spinner line before rendering it.
        if spinner_active {
            clear_spinner_line(true);
            spinner_active = false;
        }
        match event {
            AppEvent::TextChunk(chunk) => {
                produced_output = true;
                if !printed_answer_label {
                    if printed_thinking {
                        println!();
                    }
                    print!("{}", paint_bold("Kim: ", kim_accent_color()));
                    stdout().flush()?;
                    printed_answer_label = true;
                }
                print!("{}", paint_text(&chunk));
                stdout().flush()?;
                assistant.push_str(&chunk);
            }
            AppEvent::ThoughtChunk(chunk) => {
                produced_output = true;
                if !printed_thinking && !printed_answer_label {
                    printed_thinking = true;
                }
                print!("{}", paint_dim(&chunk));
                stdout().flush()?;
            }
            AppEvent::ToolEvent { verb, target } => {
                produced_output = true;
                let line = format!("{verb}: {target}");
                if line != last_tool_line {
                    if printed_answer_label && !assistant.ends_with('\n') {
                        println!();
                    }
                    print_note(&line);
                    last_tool_line = line;
                }
            }
            AppEvent::ApprovalRequest {
                id,
                command,
                cwd,
                reason,
                risk,
            } => {
                produced_output = true;
                if printed_answer_label && !assistant.ends_with('\n') {
                    println!();
                }
                print!("{}", format_approval_prompt(&command, &cwd, &reason, &risk));
                stdout().flush()?;
                // Between turns rustyline owns stdin; mid-turn (here) nothing
                // else reads it — same mechanism as confirm_run_outside_git_repo.
                let input = tokio::task::spawn_blocking(|| {
                    let mut line = String::new();
                    match io::stdin().read_line(&mut line) {
                        Ok(_) => line,
                        Err(_) => String::new(),
                    }
                })
                .await
                .unwrap_or_default();
                let decision = map_approval_input(&input);
                if let Some(dtx) = decision_tx.as_ref() {
                    let _ = dtx.send(build_decision_line(&id, decision));
                }
                let verdict = match decision {
                    "accept" => "approved once",
                    "acceptForSession" => "approved for this session",
                    _ => "denied",
                };
                print_note(&format!("({verdict})"));
            }
            AppEvent::CommandOutput(chunk) => {
                produced_output = true;
                command_output_lines += chunk.lines().count().max(1);
                if command_output_lines <= 40 {
                    for line in chunk.lines() {
                        println!("{}", paint_dim(&format!("  │ {line}")));
                    }
                } else if !command_output_capped {
                    command_output_capped = true;
                    print_note("  │ … (further command output hidden)");
                }
            }
            AppEvent::PlanUpdate(steps) => {
                produced_output = true;
                if !steps.is_empty() {
                    if printed_answer_label && !assistant.ends_with('\n') {
                        println!();
                    }
                    for (step, status) in &steps {
                        let mark = match status.as_str() {
                            "completed" => "✓",
                            "inProgress" | "in_progress" => "▸",
                            _ => "○",
                        };
                        println!("{}", paint_dim(&format!("  {mark} {step}")));
                    }
                }
            }
            AppEvent::DiffUpdate(diff) => {
                produced_output = true;
                let files = diff.lines().filter(|l| l.starts_with("+++ ")).count();
                let added = diff
                    .lines()
                    .filter(|l| l.starts_with('+') && !l.starts_with("+++"))
                    .count();
                let removed = diff
                    .lines()
                    .filter(|l| l.starts_with('-') && !l.starts_with("---"))
                    .count();
                if files > 0 || added > 0 || removed > 0 {
                    print_note(&format!("diff: {files} file(s), +{added} −{removed}"));
                }
            }
            AppEvent::TokenUsage { .. } => {
                // Tracked codex-side; too chatty to render per update.
            }
            AppEvent::TurnPhase(phase) => {
                if phase == "interrupted" {
                    print_note("⏹ turn interrupted");
                }
            }
            AppEvent::Done(used_bridge) => {
                bridge_used = used_bridge;
                break;
            }
            AppEvent::Err(error) => {
                if printed_answer_label && !assistant.ends_with('\n') {
                    println!();
                }
                app.push(MessageRole::Error, error.clone());
                print_message(&UiMessage {
                    role: MessageRole::Error,
                    content: error,
                });
                save(app);
                return Ok(false);
            }
        }
    }
    // The stream closed (or ended) while the spinner was on-screen — wipe it.
    if spinner_active {
        clear_spinner_line(true);
    }

    if printed_answer_label {
        if !assistant.ends_with('\n') {
            println!();
        }
    } else {
        println!("Kim: (no response)");
    }

    // Push the streamed assistant reply into the session and persist again, so
    // the next turn's `chat_history()` actually includes Kim's response. (A1)
    if !assistant.trim().is_empty() {
        app.push(MessageRole::Assistant, assistant);
        save(app);
    }

    let via = if bridge_used { " via Kim desktop" } else { "" };
    print_note(&format!(
        "done in {}{}",
        format_repl_elapsed(started.elapsed()),
        via
    ));
    Ok(false)
}
