mod agentic;
mod app;
pub mod commands;
mod config;
mod file_refs;
mod markdown;
mod oneshot;
mod paint;
mod pickers;
mod provider;
mod repl;
mod repl_turn;
mod sessions;
mod stdin_reader;
mod turn;

pub(crate) use file_refs::split_shellish_tokens;
use oneshot::{help_text, parse_cli_args, CliCommand};
use repl::run_repl;

use std::io::{self, IsTerminal, Read};

use config::KimConfig;
use oneshot::run_oneshot;

/* ===========================================================
entry point
=========================================================== */

pub async fn cli_main() -> Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match parse_cli_args(&args) {
        CliCommand::ShowHelp => println!("{}", help_text()),
        CliCommand::ShowVersion => println!("kim {}", env!("CARGO_PKG_VERSION")),
        CliCommand::UsageError(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
        CliCommand::Doctor { strict } => {
            // F-E-1: `kim doctor` must exit non-zero when a required check fails
            // (and, under --strict, when any provider-specific check fails) so
            // install scripts and CI can gate on it. The old path built the
            // report via handle_command and always fell through to Ok(()) → 0.
            let config = KimConfig::load();
            let report = commands::doctor_report(&config).await;
            println!("{}", report.text);
            if commands::doctor_should_fail(report.required_ok, report.all_ok, strict) {
                std::process::exit(1);
            }
        }
        CliCommand::Oneshot { mode, prompt } => {
            let prompt = match prompt {
                Some(p) => p,
                None => {
                    if !io::stdin().is_terminal() {
                        let mut buf = String::new();
                        io::stdin().read_to_string(&mut buf)?;
                        let trimmed = buf.trim().to_string();
                        if trimmed.is_empty() {
                            eprintln!("kim {}: no prompt provided", mode.label());
                            std::process::exit(1);
                        }
                        trimmed
                    } else {
                        eprintln!("Usage: kim {} <prompt...>", mode.label());
                        std::process::exit(1);
                    }
                }
            };
            if let Err(error) = run_oneshot(mode, prompt).await {
                eprintln!("kim error: {error}");
                std::process::exit(1);
            }
        }
        CliCommand::Repl { resume_id } => match run_repl(resume_id.as_deref()).await {
            Ok(session_id) => {
                // Only advertise --resume when a session file was actually
                // written (empty REPLs and skipped saves leave none). (A7)
                let session_saved = crate::config::kim_home()
                    .map(|h| {
                        h.join(".kim")
                            .join("sessions")
                            .join(format!("{session_id}.jsonl"))
                    })
                    .map(|p| p.exists())
                    .unwrap_or(false);
                if session_saved {
                    println!("Resume this Kim session with: kim --resume {session_id}");
                }
            }
            Err(error) => {
                eprintln!("kim error: {error}");
                std::process::exit(1);
            }
        },
        CliCommand::Tui { args } => {
            let code = commands::tui::run_tui_standalone(args).await;
            std::process::exit(code);
        }
    }
    Ok(())
}
