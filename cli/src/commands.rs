use std::fs;

use tokio::process::Command;

use crate::config::{config_path, KimConfig, ThemeName};
use crate::provider::{is_browser_provider, provider_info};

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

/// F-E-1: the outcome of a `kim doctor` run — the human-readable text plus two
/// health bits so `kim doctor` (and CI / install scripts calling it) can gate
/// on the exit code instead of always seeing 0.
///
/// - `required_ok` covers the universal prerequisites Kim cannot run without
///   (a Python interpreter for the orchestrator/agent; a home directory for
///   config + sessions). A required failure ALWAYS exits non-zero.
/// - `all_ok` additionally covers optional, provider-specific checks (Ollama
///   server/model, desktop bridge, API key, model-in-list). These gate the
///   exit code only under `--strict`.
pub struct DoctorReport {
    pub text: String,
    pub required_ok: bool,
    pub all_ok: bool,
}

/// F-E-1: should `kim doctor` exit non-zero? Required failures always gate;
/// optional/provider-specific failures gate only under `--strict`. Pure so it
/// is unit-testable without probing anything.
#[must_use]
pub fn doctor_should_fail(required_ok: bool, all_ok: bool, strict: bool) -> bool {
    !required_ok || (strict && !all_ok)
}

async fn doctor(config: &KimConfig) -> CommandOutcome {
    // The REPL `/doctor` command only surfaces the text; the process exit-code
    // gating lives in main()'s `kim doctor` arm, which calls doctor_report.
    CommandOutcome::Message(doctor_report(config).await.text)
}

pub async fn doctor_report(config: &KimConfig) -> DoctorReport {
    let root =
        crate::sessions::find_kim_repo_root().unwrap_or_else(|| std::path::PathBuf::from("."));
    // Required: the interpreter chat/code mode actually run (find_python: repo
    // venv first, then python3/python). This is the check CI must gate on.
    let python_found = crate::agentic::find_python(&root).is_some();
    let config_ok = config_path().is_some();

    let mut lines = vec![
        "KimCLI doctor".to_string(),
        "Mode support: chat + code".to_string(),
        format!("Provider: {}", config.provider),
        format!("Model: {}", config.model),
        format!(
            "Config: {}",
            config_path().map_or_else(
                || "(no home directory)".to_string(),
                |path| path.display().to_string()
            )
        ),
        format!("Source root: {}", source_root_status()),
        format!("Bridge token: {}", crate::provider::bridge_token_source()),
        // F12: report the interpreter the CLI actually runs (find_python:
        // repo venv first, then system), not a bare PATH probe of `python3` —
        // the two could disagree in both directions.
        format!("python: {}", python_status().await),
        format!("codex: {}", command_status("codex", &["--version"]).await),
        format!("git: {}", command_status("git", &["--version"]).await),
        format!("cargo: {}", command_status("cargo", &["--version"]).await),
    ];

    // required_ok gates the exit code unconditionally; all_ok also folds in the
    // optional provider-specific checks below (only consulted under --strict).
    let mut required_ok = python_found && config_ok;
    let mut all_ok = required_ok;

    if config.provider == "ollama" {
        let base = crate::provider::normalize_base_url(&config.ollama_base_url);
        let server = ollama_models_at(&base).await;
        lines.push(format!(
            "Ollama server: {}",
            http_status(&base, "/api/tags").await
        ));
        // A8: doctor must verify the configured model is actually servable —
        // it previously reported "ok" while the selected model was missing.
        lines.push(format!(
            "Ollama model: {}",
            ollama_model_status(&base, &config.model).await
        ));
        let ollama_ok = known_ollama_cloud_models().contains(&config.model.as_str())
            || matches!(&server, Some(models) if models.iter().any(|m| m == &config.model));
        if !ollama_ok {
            all_ok = false;
        }
    }
    if config.provider == "desktop" || is_browser_provider(&config.provider) {
        let status = desktop_bridge_status(&config.desktop_bridge_url).await;
        // The bridge is optional (browser code mode can run via local
        // Playwright), so it only affects --strict.
        if !status.starts_with("ok") {
            all_ok = false;
        }
        lines.push(format!("Kim desktop bridge: {status}"));
    }
    if is_browser_provider(&config.provider) {
        lines.push(
            "Browser provider: keyless; chat needs Kim desktop open, code mode uses Kim's Codex browser bridge."
                .to_string(),
        );
    }
    if let Some(p) = provider_info(&config.provider).filter(|p| p.key_env.is_some()) {
        let key_env = p.key_env.unwrap();
        let env_val = std::env::var(key_env).ok();
        let stored = config.api_keys.get(&config.provider).map(String::as_str);
        let key_present = env_val
            .as_deref()
            .map(str::trim)
            .is_some_and(|v| !v.is_empty())
            || stored.map(str::trim).is_some_and(|v| !v.is_empty());
        lines.push(format!(
            "API key: {}",
            api_key_status(key_env, env_val, stored, &config.provider)
        ));
        if !key_present {
            all_ok = false;
        }
        // A8: note whether the configured model appears in the known list.
        let opts = model_options(config).await;
        if !opts.is_empty() {
            let model_known = opts.iter().any(|m| m == &config.model);
            lines.push(format!(
                "Model '{}': {}",
                config.model,
                if model_known {
                    "in the known model list".to_string()
                } else {
                    format!("not in the known list (known: {})", opts.join(", "))
                }
            ));
            if !model_known {
                all_ok = false;
            }
        }
    }

    if !python_found {
        // Belt-and-braces: python_status() already prints "not found", but make
        // the gating reason explicit at the end of the report.
        lines.push(
            "FAIL: no Python interpreter found — install Python 3.11+ (Kim's orchestrator runtime)."
                .to_string(),
        );
        required_ok = false;
        all_ok = false;
    }

    DoctorReport {
        text: lines.join("\n"),
        required_ok,
        all_ok,
    }
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
    config_notice(
        config,
        format!("provider → {}  model → {}", provider.name, config.model),
    )
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
        "ollama" => ollama_models(config).await,
        // A18: current Claude model ids (claude-api).
        "claude" => vec![
            "claude-opus-4-8".to_string(),
            "claude-sonnet-4-6".to_string(),
            "claude-haiku-4-5-20251001".to_string(),
            "claude-opus-4-7".to_string(),
        ],
        // A18: fetch live where cheap (/v1/models), static fallback otherwise.
        "openai" => openai_models(config).await,
        "gemini" => vec![
            "gemini-2.5-pro".to_string(),
            "gemini-2.5-flash".to_string(),
            "gemini-2.0-flash".to_string(),
        ],
        "deepseek" => vec!["deepseek-chat".to_string(), "deepseek-reasoner".to_string()],
        p if is_browser_provider(p) => {
            // Model is determined by the browser session; these are informational only.
            vec!["browser-default".to_string()]
        }
        _ => vec![config.model.clone()],
    };
    if !models.iter().any(|model| model == &config.model) {
        models.insert(0, config.model.clone());
    }
    models
}

/// A18: OpenAI model list — live from /v1/models when a key is available,
/// otherwise a static fallback. Filters to chat-capable gpt*/o* ids.
async fn openai_models(config: &KimConfig) -> Vec<String> {
    let fallback: Vec<String> = ["gpt-4o", "gpt-4o-mini", "o3", "o3-mini", "o1"]
        .iter()
        .map(ToString::to_string)
        .collect();
    let key = std::env::var("OPENAI_API_KEY")
        .ok()
        .filter(|k| !k.trim().is_empty())
        .or_else(|| config.api_keys.get("openai").cloned())
        .unwrap_or_default();
    if key.trim().is_empty() {
        return fallback;
    }
    let resp = reqwest::Client::new()
        .get("https://api.openai.com/v1/models")
        .header("Authorization", format!("Bearer {}", key.trim()))
        .timeout(std::time::Duration::from_secs(3))
        .send()
        .await;
    let Ok(r) = resp else { return fallback };
    if !r.status().is_success() {
        return fallback;
    }
    let text = r.text().await.unwrap_or_default();
    let json: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    let mut ids: Vec<String> = json["data"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|m| m["id"].as_str())
                .filter(|id| is_openai_chat_model(id))
                .map(ToString::to_string)
                .collect()
        })
        .unwrap_or_default();
    if ids.is_empty() {
        return fallback;
    }
    ids.sort();
    ids
}

/// A45: keep only chat-capable OpenAI model ids in the `/model` picker.
/// The raw `/v1/models` list also contains embeddings, audio/realtime, image,
/// moderation, and instruct (completions) models whose ids start with `gpt`/`o`
/// but can't be used for chat — selecting one just fails at request time.
fn is_openai_chat_model(id: &str) -> bool {
    if !(id.starts_with("gpt") || id.starts_with('o')) {
        return false;
    }
    const NON_CHAT_MARKERS: &[&str] = &[
        "instruct",
        "embedding",
        "audio",
        "realtime",
        "transcribe",
        "tts",
        "whisper",
        "image",
        "moderation",
        "dall-e",
        "search",
        "-tts",
    ];
    let lowered = id.to_ascii_lowercase();
    !NON_CHAT_MARKERS.iter().any(|m| lowered.contains(m))
}

#[cfg(test)]
pub(crate) fn is_openai_chat_model_for_test(id: &str) -> bool {
    is_openai_chat_model(id)
}

async fn ollama_models(config: &KimConfig) -> Vec<String> {
    let mut models = known_ollama_cloud_models()
        .iter()
        .map(ToString::to_string)
        .collect::<Vec<_>>();
    // F-E-10: query the CONFIGURED Ollama endpoint (respects ollama_base_url /
    // a remote or nonstandard-port host) via its HTTP API, instead of shelling
    // out to the LOCAL `ollama list` daemon — which returned an empty/wrong list
    // for anyone pointing Kim at a remote Ollama while doctor and actual chat
    // requests worked.
    let base = crate::provider::normalize_base_url(&config.ollama_base_url);
    if let Some(server) = ollama_models_at(&base).await {
        models.extend(server);
    }
    models.sort();
    models.dedup();
    models
}

/// Suggested Ollama cloud models shown in the picker (#32).
///
/// These are display suggestions only — the daemon is authoritative, and the
/// selected model is validated at request time (a wrong name fails there, not
/// here). The list was trimmed to real `-cloud` tags; the previously-listed
/// `deepseek-coder-v4:cloud` and `mistral-large:latest-cloud` were fabricated
/// (non-existent / malformed tags) and are removed. Keep in sync with the
/// desktop copy in `desktop/src-tauri/src/ollama.rs`.
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
            "Choose a provider to sign in to:\n  /login ollama           — local Ollama server (free, no key)\n  /login claude           — Anthropic API key\n  /login openai           — OpenAI API key\n  /login gemini           — Google Gemini API key\n  /login deepseek         — DeepSeek API key\n  /login browser          — Kim desktop browser bridge (no key needed)\n  /login browser:claude   — Claude in browser via Kim desktop\n  /login browser:chatgpt  — ChatGPT in browser via Kim desktop\n  /login browser:gemini   — Gemini in browser via Kim desktop".to_string()
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
        p if is_browser_provider(p) => {
            config.provider = p.to_string();
            if let Some(info) = provider_info(p) {
                config.model = info.default_model.to_string();
            }
            save_notice(
                config,
                format!(
                    "Browser provider set to {p}.\nNo API key needed — requests route through the Kim desktop app.\nStart Kim desktop before chatting. Use /code to run code agent tasks."
                ),
            )
        }
        other => {
            if !KEY_PROVIDERS.contains(&other) {
                return CommandOutcome::Message(format!(
                    "Unknown login provider: {other}\nUse /login ollama, claude, openai, gemini, deepseek, or browser[:claude|chatgpt|gemini]."
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
    // F-E-10: probe the CONFIGURED Ollama endpoint, not a hardcoded
    // 127.0.0.1:11434 — a user pointing Kim at a remote / nonstandard-port
    // Ollama previously got "server is not running" from /login while doctor
    // and chat requests (which already respect ollama_base_url) worked.
    let base = crate::provider::normalize_base_url(&config.ollama_base_url);
    match ollama_models_at(&base).await {
        Some(local_models) => {
            config.provider = "ollama".to_string();
            config.model = choose_ollama_model(&config.model, &local_models);
            let model_info = if local_models.is_empty() {
                "No local models found. Pull one with: ollama pull llama3.2".to_string()
            } else {
                format!(
                    "{} model(s) available locally · model set to {}",
                    local_models.len(),
                    config.model
                )
            };
            let save_err = config
                .save()
                .err()
                .map(|e| format!("\nWarning: config not saved: {e}"))
                .unwrap_or_default();
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
    if Command::new("ollama")
        .arg("--version")
        .output()
        .await
        .is_ok()
    {
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

/// Fetch the model list from a specific ollama base URL (respects config, not a
/// hardcoded host). Returns None if unreachable / non-200. (A8)
async fn ollama_models_at(base: &str) -> Option<Vec<String>> {
    let url = format!("{}/api/tags", base.trim_end_matches('/'));
    let resp = reqwest::Client::new()
        .get(&url)
        .timeout(std::time::Duration::from_secs(2))
        .send()
        .await
        .ok()?;
    if !resp.status().is_success() {
        return None;
    }
    let text = resp.text().await.unwrap_or_default();
    let json: serde_json::Value = serde_json::from_str(&text).unwrap_or_default();
    Some(
        json["models"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| item["name"].as_str())
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default(),
    )
}

/// Doctor line: is `model` actually servable by this ollama endpoint? (A8)
async fn ollama_model_status(base: &str, model: &str) -> String {
    if known_ollama_cloud_models().contains(&model) {
        return format!("'{model}' is a known Ollama cloud model");
    }
    match ollama_models_at(base).await {
        Some(models) if models.iter().any(|m| m == model) => {
            format!("'{model}' is installed")
        }
        Some(models) => format!(
            "⚠ '{model}' is not installed and not a known cloud model — pull it or pick another (installed: {})",
            if models.is_empty() { "none".to_string() } else { models.join(", ") }
        ),
        None => format!("⚠ could not reach /api/tags to verify '{model}'"),
    }
}

/// Returns a human-readable API-key status for /doctor.  Precedence mirrors
/// resolve_api_key in provider.rs: a non-empty, non-whitespace env var wins
/// over a saved config key; a blank env var or blank stored key is treated as
/// absent.  env_val and stored are passed by the caller so this function is
/// pure and testable without touching the process environment.
fn api_key_status(
    key_env: &str,
    env_val: Option<String>,
    stored: Option<&str>,
    provider_name: &str,
) -> String {
    let env_present = env_val
        .as_deref()
        .map(str::trim)
        .is_some_and(|v| !v.is_empty());
    let stored_present = stored.map(str::trim).is_some_and(|v| !v.is_empty());

    match (env_present, stored_present) {
        (true, true) => format!("set via {key_env} env var (overrides saved key)"),
        (true, false) => format!("set via {key_env} env var"),
        (false, true) => "saved in Kim config".to_string(),
        (false, false) => format!("missing; run /login {provider_name}"),
    }
}

fn source_root_status() -> String {
    format_source_root(crate::sessions::find_kim_repo_root().as_deref())
}

/// Pure helper — formats the source-root status line for `kim doctor`.
/// Accepts the resolved root (or None) so it can be tested without the filesystem.
fn format_source_root(found: Option<&std::path::Path>) -> String {
    match found {
        Some(path) => format!("ok ({})", path.display()),
        None => {
            let marker = dirs::home_dir()
                .map(|h| h.join(".kim_root").display().to_string())
                .unwrap_or_else(|| "~/.kim_root".to_string());
            format!(
                "not set — required for browser-backed code mode\n  \
                 Fix: run cli/install.sh (writes {marker}), set KIM_PROJECT_ROOT, \
                 or run kim from inside the Kim repo"
            )
        }
    }
}

/// F12: doctor's python line must reflect what chat/code mode actually run —
/// `find_python` (repo venv first, then python3/python) — not a PATH probe.
async fn python_status() -> String {
    let root =
        crate::sessions::find_kim_repo_root().unwrap_or_else(|| std::path::PathBuf::from("."));
    match crate::agentic::find_python(&root) {
        Some(python) => {
            let shown = python.display().to_string();
            format!(
                "{} — {}",
                shown,
                command_status(&shown, &["--version"]).await
            )
        }
        None => "not found (tried repo venv, python3, python)".to_string(),
    }
}

async fn command_status(program: &str, args: &[&str]) -> String {
    match Command::new(program).args(args).output().await {
        Ok(output) if output.status.success() => {
            let first_line = String::from_utf8_lossy(if output.stdout.is_empty() {
                &output.stderr
            } else {
                &output.stdout
            })
            .lines()
            .next()
            .unwrap_or("ok")
            .trim()
            .to_string();
            if first_line.is_empty() {
                "ok".to_string()
            } else {
                first_line
            }
        }
        Ok(output) => format!("found but exited {}", output.status),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => "not found".to_string(),
        Err(error) => format!("error: {error}"),
    }
}

async fn desktop_bridge_status(base_url: &str) -> String {
    let base = base_url.trim_end_matches('/');
    for path in ["/health", "/v1/health"] {
        let status = http_status(base, path).await;
        if status.starts_with("ok") {
            return status;
        }
    }
    format!("offline ({base})")
}

async fn http_status(base_url: &str, path: &str) -> String {
    let url = format!("{}{}", base_url.trim_end_matches('/'), path);
    match reqwest::Client::new()
        .get(&url)
        .timeout(std::time::Duration::from_millis(800))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => format!("ok ({url})"),
        Ok(resp) => format!("HTTP {} ({url})", resp.status()),
        Err(_) => format!("offline ({url})"),
    }
}

fn choose_ollama_model(current: &str, local_models: &[String]) -> String {
    if local_models.iter().any(|model| model == current)
        || known_ollama_cloud_models().contains(&current)
    {
        return current.to_string();
    }
    local_models.first().cloned().unwrap_or_else(|| {
        provider_info("ollama")
            .map_or("llama3.2", |info| info.default_model)
            .to_string()
    })
}

/// F-E-11: per-provider validation host bases (overridable in tests). Real
/// hosts by default.
struct ValidationHosts<'a> {
    anthropic: &'a str,
    openai: &'a str,
    deepseek: &'a str,
    gemini: &'a str,
}

const DEFAULT_VALIDATION_HOSTS: ValidationHosts<'static> = ValidationHosts {
    anthropic: "https://api.anthropic.com",
    openai: "https://api.openai.com",
    deepseek: "https://api.deepseek.com",
    gemini: "https://generativelanguage.googleapis.com",
};

/// F-E-11: every validation request gets a hard total timeout. The default
/// reqwest client has NONE, so a blackholed connection (captive portal,
/// firewalled egress) left the REPL stuck forever after the user typed their
/// key, with no spinner and no Ctrl-C-friendly path.
const VALIDATION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

async fn validate_api_key(provider: &str, key: &str) -> Result<(), String> {
    validate_api_key_at(provider, key, &DEFAULT_VALIDATION_HOSTS, VALIDATION_TIMEOUT).await
}

async fn validate_api_key_at(
    provider: &str,
    key: &str,
    hosts: &ValidationHosts<'_>,
    timeout: std::time::Duration,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let result = match provider {
        "claude" => {
            client
                .post(format!("{}/v1/messages", hosts.anthropic))
                .header("x-api-key", key)
                .header("anthropic-version", "2023-06-01")
                .json(&serde_json::json!({
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}]
                }))
                .timeout(timeout)
                .send()
                .await
        }
        "openai" => {
            client
                .get(format!("{}/v1/models", hosts.openai))
                .bearer_auth(key)
                .timeout(timeout)
                .send()
                .await
        }
        "deepseek" => {
            client
                .get(format!("{}/v1/models", hosts.deepseek))
                .bearer_auth(key)
                .timeout(timeout)
                .send()
                .await
        }
        // F15: pass the key in a header, not the URL query string — URLs leak
        // into proxy logs and reqwest error messages include the full URL.
        "gemini" => {
            client
                .get(format!("{}/v1beta/models", hosts.gemini))
                .header("x-goog-api-key", key)
                .timeout(timeout)
                .send()
                .await
        }
        _ => return Ok(()),
    };
    match result {
        Ok(resp) => {
            let status = resp.status();
            if status.as_u16() == 401 || status.as_u16() == 403 {
                Err(format!("key rejected (HTTP {status})"))
            } else {
                Ok(())
            }
        }
        // F-E-11: a timeout or connect failure is NOT a rejection — the key may
        // be perfectly valid; we just couldn't reach the provider. Treat it as
        // "validation skipped" so the key is still saved and usable, rather than
        // hanging or telling the user their key failed.
        Err(e) if e.is_timeout() || e.is_connect() => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

fn logout(args: &str, config: &mut KimConfig) -> CommandOutcome {
    let provider = if args.is_empty() {
        config.provider.clone()
    } else {
        args.to_ascii_lowercase()
    };
    // Keyless providers (ollama, desktop, browser*) have no stored API key.
    let keyless = is_browser_provider(&provider)
        || provider_info(&provider).is_some_and(|p| p.key_env.is_none());
    if keyless {
        let hint = if is_browser_provider(&provider) {
            format!("{provider} uses the Kim desktop bridge — no API key to remove. Use /provider to switch.")
        } else {
            format!(
                "{provider} uses a local server — no API key to remove. Use /provider to switch."
            )
        };
        return CommandOutcome::Info(hint);
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
    async fn parses_provider_command() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider openai", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
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

    // F-E-10: the /model picker must query the CONFIGURED Ollama endpoint
    // (ollama_base_url), not the local `ollama list` daemon / hardcoded
    // 127.0.0.1:11434 — so a remote/nonstandard-port Ollama shows the right
    // models. A fake /api/tags server serving a model that exists NOWHERE else
    // proves the configured base URL was actually queried.
    #[tokio::test]
    async fn model_picker_queries_configured_ollama_base_url() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake ollama");
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut buf = [0u8; 2048];
                let _ = s.read(&mut buf);
                let body = r#"{"models":[{"name":"remote-only-model:latest"}]}"#;
                let resp = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                     Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = s.write_all(resp.as_bytes());
                let _ = s.flush();
            }
        });

        let config = KimConfig {
            provider: "ollama".to_string(),
            ollama_base_url: format!("http://127.0.0.1:{port}"),
            ..KimConfig::default()
        };
        let models = super::model_options(&config).await;
        assert!(
            models.iter().any(|m| m == "remote-only-model:latest"),
            "the picker must list models from the configured Ollama endpoint; got: {models:?}"
        );
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
    fn ollama_login_replaces_non_ollama_model_with_local_model() {
        let local_models = vec!["llama3.2:latest".to_string()];
        assert_eq!(
            super::choose_ollama_model("gpt-4o-mini", &local_models),
            "llama3.2:latest"
        );
        assert_eq!(
            super::choose_ollama_model("gpt-oss:20b-cloud", &local_models),
            "gpt-oss:20b-cloud"
        );
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

    #[test]
    fn format_source_root_ok_when_path_supplied() {
        let p = std::path::Path::new("/some/kim");
        let s = super::format_source_root(Some(p));
        assert!(s.starts_with("ok ("), "expected 'ok (' prefix, got: {s}");
        assert!(s.contains("/some/kim"), "expected path in output, got: {s}");
    }

    #[test]
    fn format_source_root_not_set_mentions_fix_options() {
        let s = super::format_source_root(None);
        assert!(
            s.contains("not set"),
            "expected 'not set' in output, got: {s}"
        );
        assert!(
            s.contains("browser-backed code mode"),
            "expected capability note, got: {s}"
        );
        assert!(
            s.contains("install.sh") || s.contains("KIM_PROJECT_ROOT"),
            "expected fix hint, got: {s}"
        );
    }

    // ── Browser provider tests ────────────────────────────────────────────────

    #[test]
    fn is_browser_provider_recognises_all_variants() {
        use crate::provider::is_browser_provider;
        assert!(is_browser_provider("browser"));
        assert!(is_browser_provider("browser:claude"));
        assert!(is_browser_provider("browser:chatgpt"));
        assert!(is_browser_provider("browser:gemini"));
        assert!(is_browser_provider("BROWSER:CLAUDE")); // case-insensitive
        assert!(!is_browser_provider("claude"));
        assert!(!is_browser_provider("openai"));
        assert!(!is_browser_provider("desktop"));
        assert!(!is_browser_provider("ollama"));
    }

    #[tokio::test]
    async fn provider_command_accepts_browser() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider browser", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
        assert_eq!(config.provider, "browser");
    }

    #[tokio::test]
    async fn provider_command_accepts_browser_claude() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider browser:claude", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
        assert_eq!(config.provider, "browser:claude");
    }

    #[tokio::test]
    async fn provider_command_accepts_browser_chatgpt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider browser:chatgpt", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
        assert_eq!(config.provider, "browser:chatgpt");
    }

    #[tokio::test]
    async fn provider_command_accepts_browser_gemini() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/provider browser:gemini", &mut config).await;
        assert!(matches!(
            outcome,
            CommandOutcome::Info(_) | CommandOutcome::Message(_)
        ));
        assert_eq!(config.provider, "browser:gemini");
    }

    #[tokio::test]
    async fn login_browser_sets_provider_without_key_prompt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/login browser:claude", &mut config).await;
        // Must NOT return NeedApiKey — browser providers are keyless.
        assert!(!matches!(outcome, CommandOutcome::NeedApiKey(_)));
        assert!(matches!(outcome, CommandOutcome::Message(_)));
        assert_eq!(config.provider, "browser:claude");
    }

    #[tokio::test]
    async fn login_bare_browser_sets_provider_without_key_prompt() {
        let mut config = KimConfig::default();
        let outcome = handle_command("/login browser", &mut config).await;
        assert!(!matches!(outcome, CommandOutcome::NeedApiKey(_)));
        assert_eq!(config.provider, "browser");
    }

    #[tokio::test]
    async fn logout_browser_provider_reports_keyless() {
        let mut config = KimConfig {
            provider: "browser:claude".to_string(),
            ..KimConfig::default()
        };
        let outcome = handle_command("/logout", &mut config).await;
        // Keyless — should return Info, not remove anything.
        assert!(matches!(outcome, CommandOutcome::Info(_)));
    }

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

    #[tokio::test]
    async fn browser_provider_readiness_does_not_require_api_key() {
        use crate::provider::is_browser_provider;
        // Readiness logic: browser providers are always ready (keyless).
        // Verify provider_info exists for all browser variants.
        use crate::provider::provider_info;
        for name in &[
            "browser",
            "browser:claude",
            "browser:chatgpt",
            "browser:gemini",
        ] {
            assert!(
                provider_info(name).is_some(),
                "provider_info missing for {name}"
            );
            assert!(
                is_browser_provider(name),
                "is_browser_provider false for {name}"
            );
            let info = provider_info(name).unwrap();
            assert!(info.key_env.is_none(), "{name} should be keyless");
        }
    }

    // ── api_key_status ────────────────────────────────────────────────────────

    #[test]
    fn api_key_status_env_var_only_reports_env_name() {
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some("sk-real".to_string()),
            None,
            "claude",
        );
        assert!(s.contains("ANTHROPIC_API_KEY"), "{s}");
        assert!(!s.contains("missing"), "{s}");
    }

    #[test]
    fn api_key_status_both_present_mentions_override() {
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some("sk-env".to_string()),
            Some("sk-saved"),
            "claude",
        );
        assert!(s.contains("overrides"), "{s}");
    }

    #[test]
    fn api_key_status_stored_only_reports_config() {
        let s = super::api_key_status("ANTHROPIC_API_KEY", None, Some("sk-saved"), "claude");
        assert_eq!(s, "saved in Kim config");
    }

    #[test]
    fn api_key_status_blank_env_falls_through_to_stored() {
        // ANTHROPIC_API_KEY="" must not shadow a saved key (matches resolve_api_key).
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some(String::new()),
            Some("sk-saved"),
            "claude",
        );
        assert_eq!(s, "saved in Kim config");
    }

    #[test]
    fn api_key_status_whitespace_stored_key_treated_as_absent() {
        // A stored key of "   " is effectively blank — login_with_key may trim on write,
        // but a hand-edited config could contain whitespace.  Doctor must not say
        // "saved in Kim config" when the actual request would fail with "Run /login".
        let s = super::api_key_status("ANTHROPIC_API_KEY", None, Some("   "), "claude");
        assert!(s.contains("missing"), "{s}");
        assert!(s.contains("/login"), "{s}");
    }

    #[test]
    fn api_key_status_both_absent_names_the_provider() {
        let s = super::api_key_status("OPENAI_API_KEY", None, None, "openai");
        assert!(s.contains("missing"), "{s}");
        assert!(s.contains("openai"), "{s}");
    }

    // ── F-E-11: /login key validation must time out, and treat a timeout as
    // "skipped", not "key rejected" ─────────────────────────────────────────

    /// A blackholed connection (server accepts but never responds) must NOT
    /// hang the REPL: the request times out and, because a timeout is not a
    /// rejection, validation is skipped (Ok) so the key is still saved.
    #[tokio::test]
    async fn validate_api_key_times_out_and_skips_rather_than_rejecting() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind blackhole");
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            // Hold every accepted connection open forever, never responding.
            let mut held = Vec::new();
            for stream in listener.incoming() {
                if let Ok(s) = stream {
                    held.push(s);
                }
            }
        });
        let base = format!("http://127.0.0.1:{port}");
        let hosts = super::ValidationHosts {
            anthropic: &base,
            openai: &base,
            deepseek: &base,
            gemini: &base,
        };

        let start = std::time::Instant::now();
        let result = super::validate_api_key_at(
            "openai",
            "sk-whatever",
            &hosts,
            std::time::Duration::from_millis(400),
        )
        .await;
        let elapsed = start.elapsed();

        assert!(
            result.is_ok(),
            "a timeout must be treated as skipped, not a rejection"
        );
        assert!(
            elapsed < std::time::Duration::from_secs(5),
            "validation must not hang; it returned after {elapsed:?}"
        );
    }

    /// A 401 response is a real rejection and must surface as an Err.
    #[tokio::test]
    async fn validate_api_key_reports_401_as_rejected() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind 401 server");
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut s) = stream else { continue };
                let mut buf = [0u8; 1024];
                let _ = s.read(&mut buf);
                let resp =
                    "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
                let _ = s.write_all(resp.as_bytes());
                let _ = s.flush();
            }
        });
        let base = format!("http://127.0.0.1:{port}");
        let hosts = super::ValidationHosts {
            anthropic: &base,
            openai: &base,
            deepseek: &base,
            gemini: &base,
        };

        let result = super::validate_api_key_at(
            "openai",
            "sk-bad",
            &hosts,
            std::time::Duration::from_secs(5),
        )
        .await;
        assert!(
            result.is_err_and(|e| e.contains("rejected")),
            "a 401 must be reported as a rejected key"
        );
    }

    // ── F-E-1: doctor exit-code gating ──────────────────────────────────────

    /// Required failures always gate the exit code; optional/provider-specific
    /// failures gate only under --strict.
    #[test]
    fn doctor_should_fail_gating_matrix() {
        use super::doctor_should_fail;
        // All green.
        assert!(!doctor_should_fail(true, true, false));
        assert!(!doctor_should_fail(true, true, true));
        // Optional failure: gated only by --strict.
        assert!(!doctor_should_fail(true, false, false));
        assert!(doctor_should_fail(true, false, true));
        // Required failure: always gates, regardless of --strict.
        assert!(doctor_should_fail(false, false, false));
        assert!(doctor_should_fail(false, false, true));
    }

    /// A provider-specific failure (Ollama server unreachable + configured model
    /// not installed / not a known cloud tag) must clear `all_ok` — so `kim
    /// doctor --strict` exits non-zero — while the required Python interpreter
    /// present on the test host keeps `required_ok` true (plain `kim doctor`
    /// stays 0). 127.0.0.1:1 refuses instantly, so the probe is deterministic.
    #[tokio::test]
    async fn doctor_report_flags_unreachable_ollama_without_gating_required() {
        let config = KimConfig {
            provider: "ollama".to_string(),
            model: "definitely-not-a-real-model-xyz".to_string(),
            ollama_base_url: "http://127.0.0.1:1".to_string(),
            ..KimConfig::default()
        };
        let report = super::doctor_report(&config).await;
        assert!(
            !report.all_ok,
            "unreachable Ollama + unknown model must clear all_ok; text:\n{}",
            report.text
        );
        assert!(
            report.text.contains("Ollama"),
            "the human-readable report must still surface the Ollama status"
        );
    }
}
