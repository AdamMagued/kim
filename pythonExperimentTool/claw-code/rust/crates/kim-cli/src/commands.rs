use std::fs;
#[cfg(not(windows))]
use std::time::Duration;

use tokio::process::Command;
#[cfg(not(windows))]
use tokio::time::timeout;

use crate::config::{config_path, KimConfig, ThemeName};
use crate::provider::{provider_info, PROVIDERS};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CommandOutcome {
    Message(String),
    SendPrompt(String),
    OpenModelPicker(Vec<String>),
    Compact,
    Exit,
}

pub const SUPPORTED_COMMANDS: &[&str] = &[
    "/login",
    "/logout",
    "/provider",
    "/model",
    "/status",
    "/help",
    "/clear",
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

pub async fn handle_command(input: &str, config: &mut KimConfig) -> CommandOutcome {
    let trimmed = input.trim();
    if !trimmed.starts_with('/') {
        return CommandOutcome::SendPrompt(trimmed.to_string());
    }
    let mut parts = trimmed.splitn(2, char::is_whitespace);
    let command = parts.next().unwrap_or_default();
    let args = parts.next().unwrap_or_default().trim();

    match command {
        "/help" => CommandOutcome::Message(help()),
        "/exit" => CommandOutcome::Exit,
        "/clear" => CommandOutcome::Message("Conversation cleared.".to_string()),
        "/status" => CommandOutcome::Message(status(config)),
        "/provider" => set_provider(args, config),
        "/model" => set_model(args, config).await,
        "/theme" => set_theme(args, config),
        "/login" => login(args, config).await,
        "/logout" => logout(args, config),
        "/sessions" | "/resume" => CommandOutcome::Message("__KIM_REFRESH_SESSIONS__".to_string()),
        "/usage" => CommandOutcome::Message("Usage tracking is local-only in this v1 shell; provider billing remains in provider dashboards.".to_string()),
        "/compact" => CommandOutcome::Compact,
        "/diff" => shell("git", &["diff", "--stat"], "git diff --stat").await,
        "/git" => run_project_command("git", args).await,
        "/run" => run_shell(args).await,
        "/search" => search(args).await,
        "/files" => files(args).await,
        "/init" => init_project(),
        "/mode" => CommandOutcome::Message("__KIM_TOGGLE_MODE__".to_string()),
        _ => CommandOutcome::Message(format!(
            "Unknown Kim command: {command}\nRun /help for supported commands."
        )),
    }
}

pub fn help() -> String {
    let mut lines = [
        "Kim CLI commands",
        "",
        "Core: /login [ollama|claude|openai|gemini|deepseek], /logout [provider], /provider [name], /model [name], /status, /help, /clear, /exit",
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
    format!(
        "Provider: {}\nModel: {}\nTheme: {}\nConfig: {}",
        config.provider,
        config.model,
        config.theme.label(),
        path
    )
}

fn set_provider(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        let names = PROVIDERS
            .iter()
            .map(|provider| {
                if provider.name == config.provider {
                    format!("* {}", provider.name)
                } else {
                    format!("  {}", provider.name)
                }
            })
            .collect::<Vec<_>>()
            .join("\n");
        return CommandOutcome::Message(format!(
            "Available providers:\n{names}\n\nPick one with /provider ollama, /provider claude, /provider openai, /provider gemini, /provider deepseek, or /provider desktop."
        ));
    }
    let name = args.to_ascii_lowercase();
    let Some(provider) = provider_info(&name) else {
        return CommandOutcome::Message(format!("Unknown provider: {args}"));
    };
    config.provider = provider.name.to_string();
    if config.model.trim().is_empty() || config.model == "llama3.2" {
        config.model = provider.default_model.to_string();
    }
    save_notice(config, format!("Provider set to {}.", provider.name))
}

async fn set_model(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        return CommandOutcome::OpenModelPicker(model_options(config).await);
    }
    config.model = args.to_string();
    save_notice(config, format!("Model set to {}.", config.model))
}

pub async fn model_options(config: &KimConfig) -> Vec<String> {
    let mut models = match config.provider.as_str() {
        "ollama" => ollama_models().await,
        "claude" => vec![
            "claude-opus-4-6".to_string(),
            "claude-sonnet-4-5".to_string(),
            "claude-3-5-sonnet-latest".to_string(),
        ],
        "openai" => vec![
            "gpt-4o".to_string(),
            "gpt-4o-mini".to_string(),
            "o1".to_string(),
            "o1-mini".to_string(),
            "o3-mini".to_string(),
        ],
        "gemini" => vec![
            "gemini-2.5-pro".to_string(),
            "gemini-2.5-flash".to_string(),
            "gemini-2.0-flash".to_string(),
        ],
        "deepseek" => vec!["deepseek-chat".to_string(), "deepseek-reasoner".to_string()],
        _ => vec![config.model.clone()],
    };
    if !models.iter().any(|model| model == &config.model) {
        models.insert(0, config.model.clone());
    }
    models
}

async fn ollama_models() -> Vec<String> {
    let mut models = known_ollama_cloud_models()
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    if let Ok(output) = Command::new("ollama").arg("list").output().await {
        if output.status.success() {
            let text = String::from_utf8_lossy(&output.stdout);
            for line in text.lines().skip(1) {
                if let Some(name) = line.split_whitespace().next() {
                    if !name.trim().is_empty() {
                        models.push(name.to_string());
                    }
                }
            }
        }
    }
    models.sort();
    models.dedup();
    models
}

fn known_ollama_cloud_models() -> &'static [&'static str] {
    &[
        "gpt-oss:20b-cloud",
        "gpt-oss:120b-cloud",
        "llama3.3:70b-cloud",
        "llama3.1:405b-cloud",
        "qwen2.5:72b-cloud",
        "qwen2.5-coder:32b-cloud",
        "deepseek-r1:671b-cloud",
        "deepseek-v3:685b-cloud",
        "deepseek-coder-v4:cloud",
        "mistral-large:latest-cloud",
        "gemma3:27b-cloud",
    ]
}

fn set_theme(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        return CommandOutcome::Message(format!("Current theme: {}", config.theme.label()));
    }
    let Some(theme) = ThemeName::from_input(args) else {
        return CommandOutcome::Message("Use /theme dark or /theme light.".to_string());
    };
    config.theme = theme;
    save_notice(config, format!("Theme set to {}.", theme.label()))
}

async fn login(args: &str, config: &mut KimConfig) -> CommandOutcome {
    let provider = if args.is_empty() { "ollama" } else { args }.to_ascii_lowercase();
    match provider.as_str() {
        "ollama" => ollama_login(config).await,
        "desktop" => {
            config.provider = "desktop".to_string();
            save_notice(
                config,
                "Desktop bridge mode selected. Start Kim desktop before sending.".to_string(),
            )
        }
        other => {
            if !KEY_PROVIDERS.contains(&other) {
                return CommandOutcome::Message(format!(
                    "Unknown login provider: {other}\nUse /login for Ollama, or /login claude, /login openai, /login gemini, /login deepseek for API keys."
                ));
            }
            let prompt = format!("Enter {other} API key: ");
            match rpassword::prompt_password(prompt) {
                Ok(key) if !key.trim().is_empty() => {
                    config
                        .api_keys
                        .insert(other.to_string(), key.trim().to_string());
                    config.provider = other.to_string();
                    save_notice(config, format!("{other} API key saved."))
                }
                Ok(_) => CommandOutcome::Message("No key entered.".to_string()),
                Err(error) => CommandOutcome::Message(format!("Could not read API key: {error}")),
            }
        }
    }
}

async fn ollama_login(config: &mut KimConfig) -> CommandOutcome {
    // On Windows, `ollama signin` is interactive and will hang when piped.
    // Skip straight to the browser flow; it's more reliable on all Windows versions.
    #[cfg(windows)]
    {
        // First probe whether ollama is even present.
        match Command::new("ollama").arg("--version").output().await {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return CommandOutcome::Message(
                    "Ollama is not installed or not on PATH.\nInstall Ollama for Windows, open it once, then run /login again.\nhttps://ollama.com/download/windows".to_string(),
                );
            }
            _ => {}
        }
        return ollama_browser_login(config).await;
    }
    // macOS / Linux: attempt `ollama signin` non-interactively, fall back to browser.
    #[cfg(not(windows))]
    {
        let output = timeout(
            Duration::from_secs(15),
            Command::new("ollama").arg("signin").output(),
        )
        .await;
        match output {
            Ok(Ok(output)) if output.status.success() => {
                config.provider = "ollama".to_string();
                let detail = command_output_text(&output);
                let message = if detail.trim().is_empty() {
                    "Ollama sign-in completed.".to_string()
                } else {
                    format!("Ollama sign-in completed.\n{detail}")
                };
                save_notice(config, message)
            }
            Ok(Ok(output)) if signin_is_missing(&command_output_text(&output)) => {
                ollama_browser_login(config).await
            }
            Ok(Ok(output)) => {
                let detail = command_output_text(&output);
                let browser = open_ollama_signin_page().await;
                config.provider = "ollama".to_string();
                save_notice(
                    config,
                    format!(
                        "`ollama signin` exited with {}.\n{}\n\n{}\nAfter the browser finishes, return to Kim and use /provider ollama or /model to confirm.",
                        output.status,
                        detail.trim(),
                        browser_message(browser)
                    ),
                )
            }
            Ok(Err(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                CommandOutcome::Message(
                    "Ollama is not installed or not on PATH.\nInstall Ollama, open it once, then run /login again.\nhttps://ollama.com/download".to_string(),
                )
            }
            Ok(Err(_)) => ollama_browser_login(config).await,
            Err(_) => ollama_browser_login(config).await,
        }
    }
}

async fn ollama_browser_login(config: &mut KimConfig) -> CommandOutcome {
    let browser = open_ollama_signin_page().await;
    config.provider = "ollama".to_string();
    save_notice(
        config,
        format!(
            "{}\n\nIf cloud models still say unauthorized, update Ollama for Windows and run /login again. Older Ollama builds do not expose terminal sign-in.",
            browser_message(browser)
        ),
    )
}

#[cfg(not(windows))]
fn signin_is_missing(text: &str) -> bool {
    let normalized = text.to_ascii_lowercase();
    normalized.contains("unknown command")
        || normalized.contains("unrecognized command")
        || normalized.contains("invalid command")
        || normalized.contains("unknown shorthand")
        || normalized.contains("signin")
            && (normalized.contains("did you mean")
                || normalized.contains("not found")
                || normalized.contains("is not recognized"))
}

async fn open_ollama_signin_page() -> Result<(), String> {
    let url = "https://ollama.com/signin";
    #[cfg(windows)]
    let result = Command::new("cmd")
        .args(["/C", "start", "", url])
        .status()
        .await;
    #[cfg(target_os = "macos")]
    let result = Command::new("open").arg(url).status().await;
    #[cfg(all(unix, not(target_os = "macos")))]
    let result = Command::new("xdg-open").arg(url).status().await;

    match result {
        Ok(status) if status.success() => Ok(()),
        Ok(status) => Err(format!("browser opener exited with {status}")),
        Err(error) => Err(error.to_string()),
    }
}

fn browser_message(result: Result<(), String>) -> String {
    match result {
        Ok(()) => {
            "Opened Ollama sign-in in your browser. Finish signing in there, then return to Kim."
                .to_string()
        }
        Err(error) => format!(
            "Open this page to sign in to Ollama: https://ollama.com/signin\nCould not open browser automatically: {error}"
        ),
    }
}

fn logout(args: &str, config: &mut KimConfig) -> CommandOutcome {
    let provider = if args.is_empty() {
        config.provider.clone()
    } else {
        args.to_ascii_lowercase()
    };
    if config.api_keys.remove(&provider).is_some() {
        save_notice(config, format!("Removed saved key for {provider}."))
    } else {
        CommandOutcome::Message(format!("No saved API key for {provider}."))
    }
}

async fn run_project_command(program: &str, args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message(format!("Usage: /{program} <args>"));
    }
    let split = args.split_whitespace().collect::<Vec<_>>();
    shell(program, &split, &format!("{program} {args}")).await
}

async fn run_shell(args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message("Usage: /run <command>".to_string());
    }
    #[cfg(windows)]
    let output = Command::new("cmd").args(["/C", args]).output().await;
    #[cfg(not(windows))]
    let output = Command::new("sh").args(["-lc", args]).output().await;
    format_output(args, output)
}

async fn search(args: &str) -> CommandOutcome {
    if args.trim().is_empty() {
        return CommandOutcome::Message("Usage: /search <query>".to_string());
    }
    shell(
        "rg",
        &["--line-number", "--hidden", args],
        &format!("rg {args}"),
    )
    .await
}

async fn files(args: &str) -> CommandOutcome {
    let path = if args.trim().is_empty() { "." } else { args };
    shell("rg", &["--files", path], &format!("rg --files {path}")).await
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
    let output = Command::new(program).args(args).output().await;
    format_output(label, output)
}

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
            }
            CommandOutcome::Message(text)
        }
        Err(error) => CommandOutcome::Message(format!("Could not run `{label}`: {error}")),
    }
}

#[cfg(not(windows))]
fn command_output_text(output: &std::process::Output) -> String {
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
    text
}

fn save_notice(config: &KimConfig, message: String) -> CommandOutcome {
    match config.save() {
        Ok(()) => CommandOutcome::Message(message),
        Err(error) => {
            CommandOutcome::Message(format!("{message}\nWarning: config was not saved: {error}"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{handle_command, CommandOutcome, SUPPORTED_COMMANDS};
    use crate::config::{KimConfig, ThemeName};

    #[tokio::test]
    async fn parses_theme_command() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/theme light", &mut config).await;
        assert!(matches!(outcome, CommandOutcome::Message(_)));
        assert_eq!(config.theme, ThemeName::QuietLight);
    }

    #[test]
    fn default_theme_is_dark_neovim() {
        assert_eq!(KimConfig::default().theme, ThemeName::DarkNeovim);
    }

    #[tokio::test]
    async fn parses_provider_command() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider openai", &mut config).await;
        assert!(matches!(outcome, CommandOutcome::Message(_)));
        assert_eq!(config.provider, "openai");
    }

    #[tokio::test]
    async fn compact_returns_real_compaction_outcome() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/compact", &mut config).await;
        assert_eq!(outcome, CommandOutcome::Compact);
    }

    #[tokio::test]
    async fn openai_models_are_current_picker_options() {
        let config = KimConfig {
            provider: "openai".to_string(),
            ..KimConfig::default()
        };
        let models = super::model_options(&config).await;
        assert!(models.iter().any(|model| model == "gpt-4o"));
        assert!(models.iter().any(|model| model == "o3-mini"));
        assert!(!models.iter().any(|model| model == "gpt-5.4"));
    }

    #[tokio::test]
    async fn ollama_models_include_kim_cloud_catalog() {
        let config = KimConfig {
            provider: "ollama".to_string(),
            ..KimConfig::default()
        };
        let models = super::model_options(&config).await;
        assert!(models.iter().any(|model| model == "deepseek-v3:685b-cloud"));
        assert!(models.iter().any(|model| model == "llama3.1:405b-cloud"));
        assert!(models
            .iter()
            .any(|model| model == "qwen2.5-coder:32b-cloud"));
    }

    #[test]
    fn command_surface_is_kim_ready_subset() {
        assert!(SUPPORTED_COMMANDS.contains(&"/login"));
        assert!(SUPPORTED_COMMANDS.contains(&"/run"));
        assert!(!SUPPORTED_COMMANDS.contains(&"/stickers"));
    }
}
