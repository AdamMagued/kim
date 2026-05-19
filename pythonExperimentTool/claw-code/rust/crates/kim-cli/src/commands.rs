use std::fs;

use tokio::process::Command;

use crate::config::{config_path, KimConfig, ThemeName};
use crate::provider::provider_info;

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
        "/sessions" => CommandOutcome::Message("__KIM_REFRESH_SESSIONS__".to_string()),
        "/resume" => {
            if args.is_empty() {
                CommandOutcome::Message("__KIM_REFRESH_SESSIONS__".to_string())
            } else {
                CommandOutcome::Message(format!("__KIM_RESUME_SESSION__:{args}"))
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
        return CommandOutcome::OpenProviderPicker;
    }
    let name = args.to_ascii_lowercase();
    let Some(provider) = provider_info(&name) else {
        return CommandOutcome::Message(format!("Unknown provider: {args}"));
    };
    let provider_changed = config.provider != provider.name;
    config.provider = provider.name.to_string();
    if provider_changed || config.model.trim().is_empty() {
        config.model = provider.default_model.to_string();
    }
    config_notice(config, format!("provider → {}  model → {}", provider.name, config.model))
}

async fn set_model(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        return CommandOutcome::OpenModelPicker(model_options(config).await);
    }
    config.model = args.to_string();
    config_notice(config, format!("model → {}", config.model))
}

pub async fn model_options(config: &KimConfig) -> Vec<String> {
    let mut models = match config.provider.as_str() {
        "ollama" => ollama_models().await,
        "claude" => vec![
            "claude-opus-4-7".to_string(),
            "claude-opus-4-6".to_string(),
            "claude-sonnet-4-6".to_string(),
            "claude-haiku-4-5-20251001".to_string(),
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
    config_notice(config, format!("theme → {}", theme.label()))
}

async fn login(args: &str, config: &mut KimConfig) -> CommandOutcome {
    if args.is_empty() {
        return CommandOutcome::Message(
            "Choose a provider to sign in to:\n  /login ollama    — local Ollama server (free)\n  /login claude    — Anthropic API key\n  /login openai    — OpenAI API key\n  /login gemini    — Google Gemini API key\n  /login deepseek  — DeepSeek API key".to_string()
        );
    }
    let provider = args.to_ascii_lowercase();
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
                    "Unknown login provider: {other}\nUse /login ollama, claude, openai, gemini, or deepseek."
                ));
            }
            // Signal the TUI to enter secure input mode — key entry stays inside Kim.
            CommandOutcome::NeedApiKey(other.to_string())
        }
    }
}

/// Called by the TUI after the user finishes typing an API key in secure input mode.
pub async fn login_with_key(provider: &str, key: &str, config: &mut KimConfig) -> CommandOutcome {
    if key.trim().is_empty() {
        return CommandOutcome::Message("No key entered.".to_string());
    }
    let key = key.trim().to_string();
    let validation = validate_api_key(provider, &key).await;
    config.api_keys.insert(provider.to_string(), key);
    config.provider = provider.to_string();
    if let Some(info) = provider_info(provider) {
        config.model = info.default_model.to_string();
    }
    match validation {
        Ok(()) => {
            let save_err = config
                .save()
                .err()
                .map(|e| format!("\nWarning: config not saved: {e}"))
                .unwrap_or_default();
            CommandOutcome::ProviderConnected(format!(
                "Signed in to {provider} · key validated · ready to chat.{save_err}"
            ))
        }
        Err(e) => {
            let _ = config.save();
            CommandOutcome::Message(format!(
                "Key saved for {provider} but validation failed: {e}\nIf the key is correct, Kim will use it anyway."
            ))
        }
    }
}

async fn ollama_login(config: &mut KimConfig) -> CommandOutcome {
    if !ollama_is_available().await {
        #[cfg(windows)]
        return CommandOutcome::Message(
            "Ollama is not installed.\nDownload and install it, then run 'ollama serve' in a terminal, then /login ollama again.\nhttps://ollama.com/download/windows".to_string(),
        );
        #[cfg(not(windows))]
        return CommandOutcome::Message(
            "Ollama is not installed.\nInstall it, run 'ollama serve', then /login ollama again.\nhttps://ollama.com/download".to_string(),
        );
    }
    match ollama_check_server().await {
        Some(n) => {
            config.provider = "ollama".to_string();
            let model_info = if n == 0 {
                "No local models found. Pull one with: ollama pull llama3.2".to_string()
            } else {
                format!("{n} model(s) available locally")
            };
            let save_err = config.save().err().map(|e| format!("\nWarning: config not saved: {e}")).unwrap_or_default();
            CommandOutcome::ProviderConnected(format!(
                "Connected to Ollama · {model_info} · provider set to ollama · ready to chat.{save_err}"
            ))
        }
        None => CommandOutcome::Message(
            "Ollama is installed but the server is not running.\nStart it with: ollama serve\nThen run /login ollama again.".to_string(),
        ),
    }
}

async fn ollama_is_available() -> bool {
    // Try the process PATH first.
    if Command::new("ollama").arg("--version").output().await.is_ok() {
        return true;
    }
    // Fall back to common Windows install locations — handles the case where
    // Ollama was installed after this process started (PATH not yet inherited).
    #[cfg(windows)]
    {
        let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
        let candidates = [
            format!("{local}\\Programs\\Ollama\\ollama.exe"),
            "D:\\Ollama\\ollama.exe".to_string(),
            "C:\\Program Files\\Ollama\\ollama.exe".to_string(),
        ];
        if candidates.iter().any(|p| std::path::Path::new(p).exists()) {
            return true;
        }
    }
    false
}

async fn ollama_check_server() -> Option<usize> {
    let resp = reqwest::Client::new()
        .get("http://127.0.0.1:11434/api/tags")
        .timeout(std::time::Duration::from_secs(2))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let text = resp.text().await.unwrap_or_default();
    let json: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    Some(json["models"].as_array().map_or(0, |a| a.len()))
}

async fn validate_api_key(provider: &str, key: &str) -> Result<(), String> {
    let client = reqwest::Client::new();
    let status = match provider {
        "claude" => client
            .post("https://api.anthropic.com/v1/messages")
            .header("x-api-key", key)
            .header("anthropic-version", "2023-06-01")
            .json(&serde_json::json!({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]
            }))
            .send()
            .await
            .map_err(|e| e.to_string())?
            .status(),
        "openai" => client
            .get("https://api.openai.com/v1/models")
            .bearer_auth(key)
            .send()
            .await
            .map_err(|e| e.to_string())?
            .status(),
        "deepseek" => client
            .get("https://api.deepseek.com/v1/models")
            .bearer_auth(key)
            .send()
            .await
            .map_err(|e| e.to_string())?
            .status(),
        "gemini" => client
            .get(format!(
                "https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            ))
            .send()
            .await
            .map_err(|e| e.to_string())?
            .status(),
        _ => return Ok(()),
    };
    if status.as_u16() == 401 || status.as_u16() == 403 {
        return Err(format!("key rejected (HTTP {status})"));
    }
    Ok(())
}

fn logout(args: &str, config: &mut KimConfig) -> CommandOutcome {
    let provider = if args.is_empty() {
        config.provider.clone()
    } else {
        args.to_ascii_lowercase()
    };
    // Keyless providers (ollama, desktop) have no stored API key.
    let keyless = provider_info(&provider).is_some_and(|p| p.key_env.is_none());
    if keyless {
        return CommandOutcome::Info(format!(
            "{provider} uses a local server — no API key to remove. Use /provider to switch."
        ));
    }
    if config.api_keys.remove(&provider).is_some() {
        if config.provider == provider {
            config.provider = "ollama".to_string();
            config.model = "llama3.2".to_string();
        }
        save_notice(
            config,
            format!("Logged out from {provider} · switched to ollama. Run /login {provider} to reconnect."),
        )
    } else {
        CommandOutcome::Message(format!(
            "No saved key for {provider}. Use /login {provider} to add one."
        ))
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
    let output = Command::new("rg")
        .args(["--line-number", "--hidden", args])
        .output()
        .await;
    match &output {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            #[cfg(windows)]
            {
                let output = Command::new("cmd")
                    .args(["/C", &format!("findstr /r /s /n /p \"{}\" *", args.replace('"', ""))])
                    .output()
                    .await;
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
    let output = Command::new("rg").args(["--files", path]).output().await;
    match &output {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            #[cfg(windows)]
            {
                let output = Command::new("cmd")
                    .args(["/C", &format!("dir /s /b \"{}\"", path.replace('"', ""))])
                    .output()
                    .await;
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
