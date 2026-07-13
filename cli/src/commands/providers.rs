use tokio::process::Command;

use crate::config::KimConfig;
use crate::provider::{is_browser_provider, provider_info};

use super::{config_notice, save_notice, CommandOutcome, KEY_PROVIDERS};

pub(super) fn set_provider(args: &str, config: &mut KimConfig) -> CommandOutcome {
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

pub(super) async fn set_model(args: &str, config: &mut KimConfig) -> CommandOutcome {
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
pub(super) fn known_ollama_cloud_models() -> &'static [&'static str] {
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

pub(super) async fn login(args: &str, config: &mut KimConfig) -> CommandOutcome {
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
pub(super) async fn ollama_models_at(base: &str) -> Option<Vec<String>> {
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

pub(super) fn choose_ollama_model(current: &str, local_models: &[String]) -> String {
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
pub(super) struct ValidationHosts<'a> {
    pub(super) anthropic: &'a str,
    pub(super) openai: &'a str,
    pub(super) deepseek: &'a str,
    pub(super) gemini: &'a str,
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

pub(super) async fn validate_api_key_at(
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

pub(super) fn logout(args: &str, config: &mut KimConfig) -> CommandOutcome {
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

#[cfg(test)]
mod tests {
    use crate::commands::{handle_command, CommandOutcome};
    use crate::config::KimConfig;

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

    #[tokio::test]
    async fn validate_api_key_times_out_and_skips_rather_than_rejecting() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind blackhole");
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            // Hold every accepted connection open forever, never responding.
            let mut held = Vec::new();
            for s in listener.incoming().flatten() {
                held.push(s);
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
}
