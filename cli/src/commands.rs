use std::fs;

mod doctor;
mod providers;
/// `kim tui` launcher + the standalone `kimcli` binary's shared entry point.
/// Public (unlike `doctor`/`providers`) because `src/bin/kimcli.rs` — a
/// separate crate — calls `kim_cli::commands::tui::run_tui_standalone`
/// directly.
pub mod tui;

use doctor::doctor;
#[allow(unused_imports)]
pub use doctor::{doctor_report, doctor_should_fail, DoctorReport};
use providers::{login, logout, set_model, set_provider};
#[allow(unused_imports)]
pub use providers::{login_with_key, model_options};

#[cfg(test)]
pub(crate) use providers::is_openai_chat_model_for_test;

use tokio::process::Command;

use crate::config::{config_path, KimConfig, ThemeName};
use crate::provider::is_browser_provider;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandOutcome {
    /// Important — pushed to chat as a "note" the user should read.
    Message(String),
    /// Routine confirmation — shown in the status bar only, no chat bubble.
    Info(String),
    /// Login succeeded — push message to chat AND mark provider as ready.
    ProviderConnected(String),
    /// Provider needs an API key — activate secure input mode inside the TUI.
    NeedApiKey(String),
    SendPrompt(String),
    OpenModelPicker(Vec<String>),
    OpenProviderPicker,
    Compact,
    Exit,
    /// Switch unconditionally to chat mode.
    SetChatMode,
    /// Switch unconditionally to code mode (caller must enforce provider guard).
    SetCodeMode,
    /// Toggle between chat and code (caller must enforce provider guard).
    ToggleMode,
    /// Start a brand-new chat session (new session ID, cleared messages).
    NewChat,
    /// Clear the current conversation's messages (A11 — was a sentinel string).
    ClearConversation,
    /// Open / refresh the session picker (A11 — was "__KIM_REFRESH_SESSIONS__").
    OpenSessionPicker,
    /// Resume a specific session by id (A11 — was "__KIM_RESUME_SESSION__:<id>").
    ResumeSession(String),
}

pub const SUPPORTED_COMMANDS: &[&str] = &[
    "/login",
    "/logout",
    "/provider",
    "/model",
    "/status",
    "/doctor",
    "/help",
    "/commands",
    "/clear",
    "/new",
    "/exit",
    "/sessions",
    "/resume",
    "/usage",
    "/compact",
    "/theme",
    "/diff",
    "/run",
    "/git",
    "/search",
    "/files",
    "/init",
    "/mode",
    "/chat",
    "/code",
];

const KEY_PROVIDERS: &[&str] = &["claude", "openai", "gemini", "deepseek"];

/// #5: is `command` (the first whitespace-delimited token of a line starting
/// with `/`) one Kim actually recognizes? Single source of truth shared by
/// `handle_command`'s "is this really a slash-command" gate below and by
/// `SUPPORTED_COMMANDS` (also used for tab completion in main.rs) — no
/// second list to keep in sync. `/` alone is the bare-help alias, matched in
/// `handle_command` but not itself listed in `SUPPORTED_COMMANDS`.
fn is_known_command(command: &str) -> bool {
    command == "/" || SUPPORTED_COMMANDS.contains(&command)
}

pub async fn handle_command(input: &str, config: &mut KimConfig) -> CommandOutcome {
    let trimmed = input.trim();
    if !trimmed.starts_with('/') {
        return CommandOutcome::SendPrompt(trimmed.to_string());
    }
    let mut parts = trimmed.splitn(2, char::is_whitespace);
    let command = parts.next().unwrap_or_default();
    let args = parts.next().unwrap_or_default().trim();

    // #5: a leading '/' is common in ordinary chat text too ("/etc/passwd",
    // "explain 24/7 coverage", "/foo bar" as a literal question). Only treat
    // the line as a slash-command when it matches a KNOWN command name —
    // otherwise it must reach the model like any other prompt, not vanish
    // into a silent "Unknown Kim command" note.
    if !is_known_command(command) {
        return CommandOutcome::SendPrompt(trimmed.to_string());
    }

    match command {
        "/" | "/help" | "/commands" => CommandOutcome::Message(commands_menu(args)),
        "/exit" => CommandOutcome::Exit,
        "/clear" => CommandOutcome::ClearConversation,
        "/new" => CommandOutcome::NewChat,
        "/status" => CommandOutcome::Message(status(config)),
        "/doctor" => doctor(config).await,
        "/provider" => set_provider(args, config),
        "/model" => set_model(args, config).await,
        "/theme" => set_theme(args, config),
        "/login" => login(args, config).await,
        "/logout" => logout(args, config),
        "/sessions" => CommandOutcome::OpenSessionPicker,
        "/resume" => {
            if args.is_empty() {
                CommandOutcome::OpenSessionPicker
            } else {
                CommandOutcome::ResumeSession(args.to_string())
            }
        }
        "/usage" => CommandOutcome::Message("Usage tracking is local-only in this v1 shell; provider billing remains in provider dashboards.".to_string()),
        "/compact" => CommandOutcome::Compact,
        "/diff" => shell("git", &["diff", "--stat"], "git diff --stat").await,
        "/git" => run_project_command("git", args).await,
        "/run" => run_shell(args).await,
        "/search" => search(args).await,
        "/files" => files(args).await,
        "/init" => init_project(),
        "/chat" => CommandOutcome::SetChatMode,
        "/code" => CommandOutcome::SetCodeMode,
        "/mode" => match args.to_ascii_lowercase().as_str() {
            "chat" => CommandOutcome::SetChatMode,
            "code" => CommandOutcome::SetCodeMode,
            "" => CommandOutcome::ToggleMode,
            other => CommandOutcome::Message(format!(
                "Unknown mode: {other}. Use /mode, /mode chat, or /mode code."
            )),
        },
        _ => CommandOutcome::Message(format!(
            "Unknown Kim command: {command}\nRun /help for supported commands."
        )),
    }
}

pub fn commands_menu(filter: &str) -> String {
    let filter = filter.trim().to_ascii_lowercase();
    let mut lines = ["Kim CLI commands", ""].map(str::to_string).to_vec();
    let mut current_group = "";

    for spec in COMMAND_SPECS {
        if !filter.is_empty()
            && !spec.name.contains(&filter)
            && !spec.usage.contains(&filter)
            && !spec.summary.to_ascii_lowercase().contains(&filter)
        {
            continue;
        }
        if current_group != spec.group {
            current_group = spec.group;
            lines.push(current_group.to_string());
        }
        lines.push(format!("  {:<28} {}", spec.usage, spec.summary));
    }

    if lines.len() == 2 {
        lines.push(format!("No commands matched `{filter}`."));
    }

    lines.extend([
        "".to_string(),
        "Tip: type / then press Tab to complete commands.".to_string(),
        "Modes: /chat for general chat, /code for project coding, /mode toggles.".to_string(),
    ]);
    lines.join("\n")
}

pub fn command_summary(command: &str) -> &'static str {
    COMMAND_SPECS
        .iter()
        .find(|spec| spec.name == command)
        .map_or("", |spec| spec.summary)
}

#[derive(Debug, Clone, Copy)]
struct CommandSpec {
    group: &'static str,
    name: &'static str,
    usage: &'static str,
    summary: &'static str,
}

const COMMAND_SPECS: &[CommandSpec] = &[
    CommandSpec {
        group: "Core",
        name: "/commands",
        usage: "/commands [filter]",
        summary: "Show this command menu.",
    },
    CommandSpec {
        group: "Core",
        name: "/help",
        usage: "/help",
        summary: "Show command help.",
    },
    CommandSpec {
        group: "Core",
        name: "/status",
        usage: "/status",
        summary: "Show provider, model, theme, and config path.",
    },
    CommandSpec {
        group: "Core",
        name: "/doctor",
        usage: "/doctor",
        summary: "Check KimCLI install, providers, bridge, and code mode.",
    },
    CommandSpec {
        group: "Core",
        name: "/clear",
        usage: "/clear",
        summary: "Clear the current conversation.",
    },
    CommandSpec {
        group: "Core",
        name: "/new",
        usage: "/new",
        summary: "Start a fresh chat session with a new session ID.",
    },
    CommandSpec {
        group: "Core",
        name: "/exit",
        usage: "/exit",
        summary: "Quit Kim.",
    },
    CommandSpec {
        group: "Provider",
        name: "/login",
        usage: "/login [provider]",
        summary: "Connect Ollama or add an API key.",
    },
    CommandSpec {
        group: "Provider",
        name: "/logout",
        usage: "/logout [provider]",
        summary: "Remove a saved API key.",
    },
    CommandSpec {
        group: "Provider",
        name: "/provider",
        usage: "/provider [name]",
        summary: "Switch provider or list providers.",
    },
    CommandSpec {
        group: "Provider",
        name: "/model",
        usage: "/model [name]",
        summary: "Switch model or list model options.",
    },
    CommandSpec {
        group: "Sessions",
        name: "/sessions",
        usage: "/sessions",
        summary: "List saved sessions.",
    },
    CommandSpec {
        group: "Sessions",
        name: "/resume",
        usage: "/resume [id|latest]",
        summary: "Resume a saved session.",
    },
    CommandSpec {
        group: "Sessions",
        name: "/usage",
        usage: "/usage",
        summary: "Explain local usage tracking.",
    },
    CommandSpec {
        group: "Sessions",
        name: "/compact",
        usage: "/compact",
        summary: "Compact older conversation context.",
    },
    CommandSpec {
        group: "Mode",
        name: "/mode",
        usage: "/mode",
        summary: "Toggle between chat and code mode.",
    },
    CommandSpec {
        group: "Mode",
        name: "/chat",
        usage: "/chat",
        summary: "General assistant mode.",
    },
    CommandSpec {
        group: "Mode",
        name: "/code",
        usage: "/code",
        summary: "Project coding agent mode.",
    },
    CommandSpec {
        group: "Mode",
        name: "/theme",
        usage: "/theme [dark|light]",
        summary: "Switch the terminal color theme.",
    },
    CommandSpec {
        group: "Coding",
        name: "/diff",
        usage: "/diff",
        summary: "Show git diff stats.",
    },
    CommandSpec {
        group: "Coding",
        name: "/run",
        usage: "/run <command>",
        summary: "Run a shell command.",
    },
    CommandSpec {
        group: "Coding",
        name: "/git",
        usage: "/git <args>",
        summary: "Run git with arguments.",
    },
    CommandSpec {
        group: "Coding",
        name: "/search",
        usage: "/search <query>",
        summary: "Search files with ripgrep.",
    },
    CommandSpec {
        group: "Coding",
        name: "/files",
        usage: "/files [path]",
        summary: "List files with ripgrep.",
    },
    CommandSpec {
        group: "Coding",
        name: "/init",
        usage: "/init",
        summary: "Create a KIM.md project note file.",
    },
];

#[allow(dead_code)]
pub fn legacy_help() -> String {
    let mut lines = [
        "Kim CLI commands",
        "",
        "Core: /login [ollama|claude|openai|gemini|deepseek], /logout [provider], /provider [name], /model [name], /status, /doctor, /help, /clear, /exit",
        "Sessions: /sessions, /resume, /usage, /compact",
        "UI: /theme [dark|light], /chat, /code, /mode",
        "Coding: /diff, /run <cmd>, /git <args>, /search <query>, /files [path], /init",
        "",
        "Modes: /chat shows Kim chat sessions. /code shows sessions for the current project only.",
        "Login: /login defaults to Ollama. For keys, type /login claude, /login openai, /login gemini, or /login deepseek.",
        "Providers: ollama, claude, openai, gemini, deepseek, desktop",
    ]
    .map(str::to_string)
    .to_vec();
    lines.push(String::new());
    lines.push(format!(
        "Supported command set: {}",
        SUPPORTED_COMMANDS.join(", ")
    ));
    lines.join("\n")
}

fn status(config: &KimConfig) -> String {
    let path = config_path().map_or_else(
        || "(no home directory)".to_string(),
        |path| path.display().to_string(),
    );
    let provider_note = if is_browser_provider(&config.provider) {
        "\nNote: browser providers are keyless — requests route through the Kim desktop app."
    } else {
        ""
    };
    format!(
        "Provider: {}\nModel: {}\nTheme: {}\nConfig: {}{}",
        config.provider,
        config.model,
        config.theme.label(),
        path,
        provider_note
    )
}

fn set_theme(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        return CommandOutcome::Message(format!("Current theme: {}", config.theme.label()));
    }
    let Some(theme) = ThemeName::from_input(args) else {
        return CommandOutcome::Message("Use /theme dark or /theme light.".to_string());
    };
    config.theme = theme;
    config_notice(config, format!("theme → {}", theme.label()))
}

async fn run_project_command(program: &str, args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message(format!("Usage: /{program} <args>"));
    }
    // A10: honor quotes so `/git commit -m "two words"` stays one argument.
    let tokens = crate::split_shellish_tokens(args);
    let split = tokens.iter().map(String::as_str).collect::<Vec<_>>();
    shell(program, &split, &format!("{program} {args}")).await
}

/// F18: hard deadline for slash-command subprocesses (`/run`, `/git`,
/// `/search`, `/files`). Without one, `/run sleep 10000` wedged the REPL and
/// Ctrl-C killed the whole CLI.
const SLASH_CMD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

/// Run a slash-command subprocess with a timeout; `kill_on_drop` reaps the
/// child when the deadline fires. Timeouts surface as an `io::Error` so
/// callers' `ErrorKind::NotFound` fallbacks keep working unchanged.
async fn output_with_timeout(mut cmd: Command) -> Result<std::process::Output, std::io::Error> {
    cmd.kill_on_drop(true);
    match tokio::time::timeout(SLASH_CMD_TIMEOUT, cmd.output()).await {
        Ok(result) => result,
        Err(_) => Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            format!(
                "timed out after {}s (process killed)",
                SLASH_CMD_TIMEOUT.as_secs()
            ),
        )),
    }
}

async fn run_shell(args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message("Usage: /run <command>".to_string());
    }
    #[cfg(windows)]
    let mut cmd = Command::new("cmd");
    #[cfg(windows)]
    cmd.args(["/C", args]);
    #[cfg(not(windows))]
    let mut cmd = Command::new("sh");
    #[cfg(not(windows))]
    cmd.args(["-lc", args]);
    let output = output_with_timeout(cmd).await;
    format_output(args, output)
}

async fn search(args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message("Usage: /search <query>".to_string());
    }
    let mut cmd = Command::new("rg");
    cmd.args(["--line-number", "--hidden", args]);
    let output = output_with_timeout(cmd).await;
    match &output {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            #[cfg(windows)]
            {
                let mut cmd = Command::new("cmd");
                cmd.args([
                    "/C",
                    &format!("findstr /r /s /n /p \"{}\" *", args.replace('"', "")),
                ]);
                let output = output_with_timeout(cmd).await;
                return format_output(&format!("findstr {args}"), output);
            }
            #[cfg(not(windows))]
            return CommandOutcome::Message(
                "ripgrep (rg) not found. Install: brew install ripgrep  or  apt install ripgrep"
                    .to_string(),
            );
        }
        _ => format_output(&format!("rg {args}"), output),
    }
}

async fn files(args: &str) -> CommandOutcome {
    let path = if args.trim().is_empty() { "." } else { args };
    let mut cmd = Command::new("rg");
    cmd.args(["--files", path]);
    let output = output_with_timeout(cmd).await;
    match &output {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            #[cfg(windows)]
            {
                let mut cmd = Command::new("cmd");
                cmd.args(["/C", &format!("dir /s /b \"{}\"", path.replace('"', ""))]);
                let output = output_with_timeout(cmd).await;
                return format_output(&format!("dir {path}"), output);
            }
            #[cfg(not(windows))]
            return CommandOutcome::Message(
                "ripgrep (rg) not found. Install: brew install ripgrep  or  apt install ripgrep"
                    .to_string(),
            );
        }
        _ => format_output(&format!("rg --files {path}"), output),
    }
}

fn init_project() -> CommandOutcome {
    let content = "# KIM.md\n\n- Project notes for Kim CLI.\n- Add build/test commands here.\n";
    let result = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open("KIM.md")
        .and_then(|mut file| {
            use std::io::Write;
            file.write_all(content.as_bytes())
        });
    match result {
        Ok(()) => CommandOutcome::Message("Created KIM.md.".to_string()),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            CommandOutcome::Message("KIM.md already exists.".to_string())
        }
        Err(error) => CommandOutcome::Message(format!("Could not create KIM.md: {error}")),
    }
}

async fn shell(program: &str, args: &[&str], label: &str) -> CommandOutcome {
    let mut cmd = Command::new(program);
    cmd.args(args);
    let output = output_with_timeout(cmd).await;
    format_output(label, output)
}

/// F18: cap the rendered subprocess output so a huge `rg` result doesn't get
/// buffered/printed in full (chars, not bytes — truncate is char-safe).
const MAX_OUTPUT_CHARS: usize = 200_000;

fn format_output(
    label: &str,
    output: Result<std::process::Output, std::io::Error>,
) -> CommandOutcome {
    match output {
        Ok(output) => {
            let mut text = String::new();
            if !output.stdout.is_empty() {
                text.push_str(&String::from_utf8_lossy(&output.stdout));
            }
            if !output.stderr.is_empty() {
                if !text.is_empty() {
                    text.push('\n');
                }
                text.push_str(&String::from_utf8_lossy(&output.stderr));
            }
            if text.trim().is_empty() {
                text = format!("`{label}` completed with no output.");
            } else if text.chars().nth(MAX_OUTPUT_CHARS).is_some() {
                text = format!(
                    "{}\n… (output truncated at {MAX_OUTPUT_CHARS} characters)",
                    crate::sessions::truncate(&text, MAX_OUTPUT_CHARS)
                );
            }
            CommandOutcome::Message(text)
        }
        Err(error) => CommandOutcome::Message(format!("Could not run `{label}`: {error}")),
    }
}

fn save_notice(config: &KimConfig, message: String) -> CommandOutcome {
    match config.save() {
        Ok(()) => CommandOutcome::Message(message),
        Err(error) => {
            CommandOutcome::Message(format!("{message}\nWarning: config was not saved: {error}"))
        }
    }
}

/// Like save_notice but for routine UI config changes — shows in status bar only.
fn config_notice(config: &KimConfig, message: String) -> CommandOutcome {
    match config.save() {
        Ok(()) => CommandOutcome::Info(message),
        Err(error) => {
            CommandOutcome::Message(format!("{message}\nWarning: config was not saved: {error}"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{handle_command, is_known_command, CommandOutcome, SUPPORTED_COMMANDS};
    use crate::config::{KimConfig, ThemeName};

    // ── #5: unrecognized leading-slash text falls through to chat ───────────

    #[tokio::test]
    async fn unknown_slash_command_falls_through_to_send_prompt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/etc/passwd is a file", &mut config).await;
        assert_eq!(
            outcome,
            CommandOutcome::SendPrompt("/etc/passwd is a file".to_string()),
            "an unrecognized leading-slash line must reach the model as chat text, \
             not vanish into an 'Unknown Kim command' note"
        );
    }

    #[tokio::test]
    async fn unknown_bare_slash_word_falls_through_to_send_prompt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/nonexistent", &mut config).await;
        assert_eq!(
            outcome,
            CommandOutcome::SendPrompt("/nonexistent".to_string())
        );
    }

    #[tokio::test]
    async fn known_command_is_still_dispatched_not_sent_as_prompt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/exit", &mut config).await;
        assert_eq!(outcome, CommandOutcome::Exit);
    }

    #[test]
    fn is_known_command_covers_bare_slash_and_supported_list() {
        assert!(is_known_command("/"));
        for command in SUPPORTED_COMMANDS {
            assert!(
                is_known_command(command),
                "{command} is in SUPPORTED_COMMANDS but is_known_command rejected it"
            );
        }
        assert!(!is_known_command("/nonexistent"));
        assert!(!is_known_command("/etc"));
    }

    #[tokio::test]
    async fn parses_theme_command() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/theme light", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
        assert_eq!(config.theme, ThemeName::QuietLight);
    }

    #[test]
    fn default_theme_is_dark_neovim() {
        assert_eq!(KimConfig::default().theme, ThemeName::DarkNeovim);
    }

    #[tokio::test]
    async fn compact_returns_real_compaction_outcome() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/compact", &mut config).await;
        assert_eq!(outcome, CommandOutcome::Compact);
    }

    #[tokio::test]
    async fn new_command_returns_new_chat() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/new", &mut config).await;
        assert_eq!(outcome, CommandOutcome::NewChat);
    }

    #[test]
    fn new_command_is_in_supported_commands() {
        assert!(SUPPORTED_COMMANDS.contains(&"/new"));
    }

    #[test]
    fn command_surface_is_kim_ready_subset() {
        assert!(SUPPORTED_COMMANDS.contains(&"/login"));
        assert!(SUPPORTED_COMMANDS.contains(&"/run"));
        assert!(!SUPPORTED_COMMANDS.contains(&"/stickers"));
    }

    // ── Mode command tests ────────────────────────────────────────────────────

    #[tokio::test]
    async fn chat_command_returns_set_chat_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/chat", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetChatMode);
    }

    #[tokio::test]
    async fn chat_command_with_trailing_text_still_sets_chat_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/chat extra text", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetChatMode);
    }

    #[tokio::test]
    async fn code_command_returns_set_code_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/code", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetCodeMode);
    }

    #[tokio::test]
    async fn mode_no_args_returns_toggle_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode", &mut config).await;
        assert_eq!(outcome, CommandOutcome::ToggleMode);
    }

    #[tokio::test]
    async fn mode_chat_returns_set_chat_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode chat", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetChatMode);
    }

    #[tokio::test]
    async fn mode_code_returns_set_code_mode() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode code", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetCodeMode);
    }

    #[tokio::test]
    async fn mode_chat_is_case_insensitive() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode CHAT", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetChatMode);
    }

    #[tokio::test]
    async fn mode_code_is_case_insensitive() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode CODE", &mut config).await;
        assert_eq!(outcome, CommandOutcome::SetCodeMode);
    }

    #[tokio::test]
    async fn mode_unknown_subcommand_returns_error_message() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/mode unknown", &mut config).await;
        match outcome {
            CommandOutcome::Message(msg) => assert!(
                msg.contains("Unknown mode"),
                "expected 'Unknown mode' in message, got: {msg}"
            ),
            other => panic!("expected Message outcome, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn chat_and_code_are_in_supported_commands() {
        assert!(SUPPORTED_COMMANDS.contains(&"/chat"));
        assert!(SUPPORTED_COMMANDS.contains(&"/code"));
        assert!(SUPPORTED_COMMANDS.contains(&"/mode"));
    }

    // ── Browser provider tests ────────────────────────────────────────────────

    // ── source_root formatting ────────────────────────────────────────────────

    #[tokio::test]
    async fn status_includes_bridge_note_for_browser_provider() {
        let mut config = KimConfig {
            provider: "browser:gemini".to_string(),
            ..KimConfig::default()
        };
        let outcome = handle_command("/status", &mut config).await;
        if let CommandOutcome::Message(text) = outcome {
            assert!(text.contains("browser:gemini"));
            assert!(
                text.contains("desktop app") || text.contains("keyless") || text.contains("bridge")
            );
        } else {
            panic!("expected Message outcome");
        }
    }
}
