use crate::app::{App, AppMode, MessageRole};
use crate::config::KimConfig;
use crate::turn::{code_mode_denied_reason, provider_is_ready, stream_repl_turn};

#[derive(Debug)]
pub(crate) enum CliCommand {
    ShowHelp,
    ShowVersion,
    /// `kim doctor` health check. `strict` (from `--strict`) makes optional,
    /// provider-specific check failures gate the exit code too. (F-E-1)
    Doctor {
        strict: bool,
    },
    Oneshot {
        mode: AppMode,
        prompt: Option<String>,
    },
    Repl {
        resume_id: Option<String>,
    },
    /// `kim tui` launcher: spawns the standalone kimcli/codex binary (see
    /// `commands::tui`). Flag parsing (`--provider`/`--model`/`--cwd`/
    /// `--verbose`/`-- <passthrough>`) happens in `commands::tui` itself so the
    /// exact same parser serves both `kim tui` and the standalone `kimcli`
    /// binary entry point.
    Tui {
        args: Vec<String>,
    },
    /// #6: a CLI flag was given but is malformed (currently only a bare
    /// trailing `--resume`). Carries the message to print to stderr before
    /// exiting non-zero — never silently falls through to a fresh session.
    UsageError(String),
}

pub(crate) fn parse_cli_args(args: &[String]) -> CliCommand {
    // Only scan for kim's own global flags BEFORE a literal `--` separator —
    // anything after `--` is passthrough for a downstream program (e.g.
    // `kim tui -- --help` must reach kimcli's own --help, not print kim's).
    let scan_end = args.iter().position(|a| a == "--").unwrap_or(args.len());
    let head = &args[..scan_end];
    if head.iter().any(|a| matches!(a.as_str(), "--help" | "-h")) {
        return CliCommand::ShowHelp;
    }
    if head
        .iter()
        .any(|a| matches!(a.as_str(), "--version" | "-V"))
    {
        return CliCommand::ShowVersion;
    }
    match args.first().map(String::as_str) {
        // Bare `kim` opens the interactive REPL.
        None => CliCommand::Repl { resume_id: None },
        Some("doctor") => match args.get(1).map(String::as_str) {
            None => CliCommand::Doctor { strict: false },
            // F-E-1: `--strict` gates the exit code on provider-specific checks
            // too (for CI / install scripts).
            Some("--strict") => {
                if let Some(extra) = args.get(2) {
                    return CliCommand::UsageError(format!(
                        "kim doctor: unexpected argument '{extra}'. Usage: kim doctor [--strict]"
                    ));
                }
                CliCommand::Doctor { strict: true }
            }
            // doctor takes no other arguments; an extra token is a typo, not a
            // silent no-op. (F-E-2)
            Some(extra) => CliCommand::UsageError(format!(
                "kim doctor: unexpected argument '{extra}'. Usage: kim doctor [--strict]"
            )),
        },
        Some(sub @ "chat") | Some(sub @ "code") => {
            let rest = &args[1..];
            // F-E-2: `kim chat --resume abc` used to treat `--resume abc` as
            // prompt text and send it to the model. A leading option after the
            // subcommand is a mistake — reject it instead of silently turning
            // the user's intent into a prompt.
            if let Some(first) = rest.first() {
                if first.starts_with('-') {
                    return CliCommand::UsageError(format!(
                        "kim {sub}: unexpected option '{first}'. Usage: kim {sub} <prompt...>"
                    ));
                }
            }
            let mode = if sub == "code" {
                AppMode::Code
            } else {
                AppMode::Chat
            };
            let prompt = if rest.is_empty() {
                None
            } else {
                Some(rest.join(" "))
            };
            CliCommand::Oneshot { mode, prompt }
        }
        // `kim tui [--provider <name>] [--model <name>] [--cwd <dir>] [--verbose]
        // [-- <raw args...>]` — launches the standalone kimcli/codex TUI. All
        // flag validation happens in `commands::tui::parse_tui_flags`.
        Some("tui") => CliCommand::Tui {
            args: args[1..].to_vec(),
        },
        // `kim --resume <id>` resumes an existing session in the REPL. The id
        // is required (#6: a bare trailing `--resume` is a usage error, never a
        // silent new session).
        Some("--resume") => match args.get(1) {
            Some(value) => {
                if let Some(extra) = args.get(2) {
                    return CliCommand::UsageError(format!(
                        "kim --resume: unexpected argument '{extra}'. \
                         Usage: kim --resume <id|latest>"
                    ));
                }
                CliCommand::Repl {
                    resume_id: Some(value.clone()),
                }
            }
            None => CliCommand::UsageError(
                "kim --resume: missing session id. Usage: kim --resume <id|latest>".to_string(),
            ),
        },
        // F-E-2: anything else — an unknown flag (`--continue`, a typo'd
        // `--resum`), or an unknown bare subcommand (`resume`, `login`) — used
        // to fall through to a brand-new REPL session, silently discarding the
        // user's intent and scattering a fresh session file. Reject it (exit 2)
        // the same way `--resume`-without-value is rejected.
        Some(other) => CliCommand::UsageError(format!(
            "kim: unknown command '{other}'.\n\
             Usage: kim [chat|code <prompt>] | kim doctor | kim tui | kim --resume <id> | kim --help"
        )),
    }
}

pub(crate) async fn run_oneshot(
    mode: AppMode,
    prompt: String,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut app = App::new(KimConfig::load(), None);
    app.provider_ready = provider_is_ready(&app.config);

    if mode == AppMode::Code {
        if let Some(reason) = code_mode_denied_reason(&app.config.provider) {
            eprintln!("{reason}");
            std::process::exit(2);
        }
    }

    app.set_mode(mode);
    stream_repl_turn(&mut app, prompt).await?;

    if app
        .messages
        .last()
        .is_some_and(|m| m.role == MessageRole::Error)
    {
        std::process::exit(1);
    }

    Ok(())
}

pub(crate) fn help_text() -> &'static str {
    "Kim terminal CLI\n\nUsage:\n  kim                      Launch the interactive chat/code REPL\n  kim chat <prompt...>     Send one prompt in chat mode and exit\n  kim code <prompt...>     Send one prompt in code-agent mode and exit\n  kim tui                  Launch the full Codex-style TUI (kimcli) via Kim's providers\n  kim doctor               Check install, providers, desktop bridge, and code mode\n  kim doctor --strict      Same, but exit non-zero on any failed check (CI)\n  kim --resume <id>        Resume a Kim session in the REPL\n  kim --resume latest      Resume the newest saved session\n  kim --help               Show this help\n  kim --version            Show the version\n\nPipe a prompt via stdin:\n  echo 'explain this' | kim chat\n  echo 'fix the build' | kim code\n\n`kim tui` flags:\n  --provider <name>        Provider to route through (default: config tui_provider, or browser:claude)\n  --model <name>           Model name to pass to kimcli\n  --cwd <dir>              Working directory for the TUI (default: current dir)\n  --verbose                Forward proxy/launcher diagnostics to stderr\n  -- <args...>             Passthrough args appended verbatim to the kimcli binary\n\nInside Kim, type /help for commands and /login to connect a provider."
}

#[cfg(test)]
mod tests {
    use super::{parse_cli_args, CliCommand};
    use crate::app::AppMode;

    // ── parse_cli_args tests ──────────────────────────────────────────────────

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parse_args_empty_launches_repl() {
        let cmd = parse_cli_args(&args(&[]));
        assert!(matches!(cmd, CliCommand::Repl { resume_id: None }));
    }

    #[test]
    fn parse_args_help_flags() {
        for flag in &["--help", "-h"] {
            let cmd = parse_cli_args(&args(&[flag]));
            assert!(
                matches!(cmd, CliCommand::ShowHelp),
                "{flag} should show help"
            );
        }
    }

    #[test]
    fn parse_args_version_flags() {
        for flag in &["--version", "-V"] {
            let cmd = parse_cli_args(&args(&[flag]));
            assert!(
                matches!(cmd, CliCommand::ShowVersion),
                "{flag} should show version"
            );
        }
    }

    #[test]
    fn parse_args_doctor() {
        let cmd = parse_cli_args(&args(&["doctor"]));
        assert!(matches!(cmd, CliCommand::Doctor { strict: false }));
    }

    // F-E-1: `kim doctor --strict` parses to the strict health check.
    #[test]
    fn parse_args_doctor_strict() {
        let cmd = parse_cli_args(&args(&["doctor", "--strict"]));
        assert!(matches!(cmd, CliCommand::Doctor { strict: true }));
    }

    #[test]
    fn parse_args_chat_with_prompt() {
        let cmd = parse_cli_args(&args(&["chat", "hello", "world"]));
        match cmd {
            CliCommand::Oneshot {
                mode: AppMode::Chat,
                prompt: Some(p),
            } => assert_eq!(p, "hello world"),
            other => panic!("expected Oneshot Chat, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_code_with_prompt() {
        let cmd = parse_cli_args(&args(&["code", "fix", "this", "bug"]));
        match cmd {
            CliCommand::Oneshot {
                mode: AppMode::Code,
                prompt: Some(p),
            } => assert_eq!(p, "fix this bug"),
            other => panic!("expected Oneshot Code, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_chat_no_prompt_is_none() {
        let cmd = parse_cli_args(&args(&["chat"]));
        assert!(matches!(
            cmd,
            CliCommand::Oneshot {
                mode: AppMode::Chat,
                prompt: None
            }
        ));
    }

    #[test]
    fn parse_args_code_no_prompt_is_none() {
        let cmd = parse_cli_args(&args(&["code"]));
        assert!(matches!(
            cmd,
            CliCommand::Oneshot {
                mode: AppMode::Code,
                prompt: None
            }
        ));
    }

    #[test]
    fn parse_args_resume_with_id() {
        let cmd = parse_cli_args(&args(&["--resume", "session-1234"]));
        match cmd {
            CliCommand::Repl {
                resume_id: Some(id),
            } => assert_eq!(id, "session-1234"),
            other => panic!("expected Repl with resume_id, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_resume_latest() {
        let cmd = parse_cli_args(&args(&["--resume", "latest"]));
        match cmd {
            CliCommand::Repl {
                resume_id: Some(id),
            } => assert_eq!(id, "latest"),
            other => panic!("expected Repl with resume_id=latest, got {other:?}"),
        }
    }

    // F-E-2: an unknown flag no longer silently opens a fresh REPL — it is a
    // usage error (exit 2), so a typo can't scatter a new session file.
    #[test]
    fn parse_args_unknown_flag_is_usage_error() {
        let cmd = parse_cli_args(&args(&["--unknown-flag"]));
        match cmd {
            CliCommand::UsageError(message) => assert!(
                message.contains("--unknown-flag"),
                "usage error should name the offending flag, got: {message}"
            ),
            other => panic!("expected UsageError for an unknown flag, got {other:?}"),
        }
    }

    // F-E-2: an unknown bare subcommand (e.g. `kim login`, or `kim resume
    // latest` with the dashes dropped) is a usage error, not a silent new REPL.
    #[test]
    fn parse_args_unknown_subcommand_is_usage_error() {
        for argv in [
            vec!["login"],
            vec!["resume", "latest"],
            vec!["--continue"],
            vec!["--resum", "latest"],
        ] {
            let cmd = parse_cli_args(&args(&argv));
            assert!(
                matches!(cmd, CliCommand::UsageError(_)),
                "{argv:?} should be a UsageError, got {cmd:?}"
            );
        }
    }

    // F-E-2: `kim chat --resume abc` must not be sent to the model as the
    // literal prompt "--resume abc"; a leading option is a usage error.
    #[test]
    fn parse_args_chat_leading_option_is_usage_error() {
        let cmd = parse_cli_args(&args(&["chat", "--resume", "abc"]));
        match cmd {
            CliCommand::UsageError(message) => assert!(
                message.contains("--resume"),
                "usage error should name the offending option, got: {message}"
            ),
            other => panic!("expected UsageError, got {other:?}"),
        }
    }

    // F-E-2: extra tokens after `kim doctor` / `kim --resume <id>` are rejected
    // rather than ignored.
    #[test]
    fn parse_args_trailing_garbage_is_usage_error() {
        assert!(matches!(
            parse_cli_args(&args(&["doctor", "--json"])),
            CliCommand::UsageError(_)
        ));
        assert!(matches!(
            parse_cli_args(&args(&["--resume", "abc", "def"])),
            CliCommand::UsageError(_)
        ));
    }

    // ── #6: trailing `--resume` with no value is a usage error, not a silent
    // new session ──────────────────────────────────────────────────────────

    #[test]
    fn parse_args_resume_with_no_value_is_usage_error() {
        let cmd = parse_cli_args(&args(&["--resume"]));
        match cmd {
            CliCommand::UsageError(message) => {
                assert!(
                    message.contains("--resume"),
                    "usage error should mention --resume, got: {message}"
                );
            }
            other => panic!("expected UsageError for trailing --resume, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_no_resume_flag_is_still_a_plain_repl() {
        let cmd = parse_cli_args(&args(&[]));
        assert!(matches!(cmd, CliCommand::Repl { resume_id: None }));
    }

    #[test]
    fn parse_args_chat_prompt_is_joined_with_spaces() {
        let cmd = parse_cli_args(&args(&["chat", "one", "two", "three"]));
        match cmd {
            CliCommand::Oneshot {
                prompt: Some(p), ..
            } => assert_eq!(p, "one two three"),
            other => panic!("unexpected {other:?}"),
        }
    }

    // ── `kim tui` parsing ────────────────────────────────────────────────────

    #[test]
    fn parse_args_tui_bare_forwards_no_args() {
        let cmd = parse_cli_args(&args(&["tui"]));
        match cmd {
            CliCommand::Tui { args } => assert!(args.is_empty()),
            other => panic!("expected Tui, got {other:?}"),
        }
    }

    #[test]
    fn parse_args_tui_forwards_flags_and_passthrough_verbatim() {
        let cmd = parse_cli_args(&args(&[
            "tui",
            "--provider",
            "browser:claude",
            "--verbose",
            "--",
            "--help",
        ]));
        match cmd {
            CliCommand::Tui { args } => assert_eq!(
                args,
                vec!["--provider", "browser:claude", "--verbose", "--", "--help"]
            ),
            other => panic!("expected Tui, got {other:?}"),
        }
    }

    // A `--` separator hides everything after it from kim's own global
    // --help/--version scan, so `kim tui -- --help` reaches kimcli's --help
    // instead of printing kim's own help text.
    #[test]
    fn parse_args_help_after_double_dash_is_not_intercepted() {
        let cmd = parse_cli_args(&args(&["tui", "--", "--help"]));
        assert!(
            matches!(cmd, CliCommand::Tui { .. }),
            "passthrough --help after -- must not trigger kim's own ShowHelp, got {cmd:?}"
        );
    }

    // Existing behavior is unchanged when there is no `--` separator at all.
    #[test]
    fn parse_args_help_before_any_double_dash_still_shows_help() {
        assert!(matches!(
            parse_cli_args(&args(&["--help"])),
            CliCommand::ShowHelp
        ));
        assert!(matches!(
            parse_cli_args(&args(&["chat", "hello", "--help"])),
            CliCommand::ShowHelp
        ));
    }
}
