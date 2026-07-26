//! K2/A4 helper — provider routing for direct Codex/Claw CLI runs.
//!
//! Extracted from `lib.rs` (Q6 size gate): resolves the model/key/base-url
//! for each provider into a pure [`crate::task_spec::ProviderRoute`] that the
//! `task_spec::codex_direct_spec` builder consumes.

use crate::*;
use std::path::Path;

pub(crate) async fn selected_ollama_codex_model(
    mode: Option<&str>,
    base_url: Option<&str>,
    local_model: Option<&str>,
    cloud_model: Option<&str>,
    config: &config::AppConfig,
) -> Result<String, String> {
    let mode = mode.unwrap_or("cloud").trim().to_ascii_lowercase();
    if mode == "local" {
        let model = local_model.unwrap_or("").trim();
        if !model.is_empty() {
            return Ok(model.to_string());
        }
        let base = base_url
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("http://localhost:11434");
        if let Ok(models) = ollama_tags(base).await {
            if let Some(first) = models
                .first()
                .map(|m| m.name.trim())
                .filter(|m| !m.is_empty())
            {
                return Ok(first.to_string());
            }
        }
        return Err(
            "Pick or pull an Ollama local model before running Codex with Ollama Local."
                .to_string(),
        );
    }
    let fallback = config
        .default_model
        .get("ollama")
        .map(|s| s.as_str())
        .unwrap_or("gpt-oss:120b-cloud");
    Ok(cloud_model
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or(fallback)
        .to_string())
}

/// Resolve the provider routing (extra argv + env + route label) for a
/// direct Codex/Claw CLI run. Pure output (`ProviderRoute`) so the spec
/// builders in `task_spec` stay side-effect free (K2).
#[allow(clippy::too_many_arguments)]
pub(crate) async fn configure_codex_direct_provider(
    provider_arg: &str,
    kim_root: &Path,
    ollama_base_url: Option<&str>,
    ollama_mode: Option<&str>,
    ollama_local_model: Option<&str>,
    ollama_cloud_model: Option<&str>,
    config: &config::AppConfig,
) -> Result<task_spec::ProviderRoute, String> {
    use task_spec::ProviderRoute;
    let provider = provider_arg.trim().to_ascii_lowercase();
    match provider.as_str() {
        "ollama" => {
            let model = selected_ollama_codex_model(
                ollama_mode,
                ollama_base_url,
                ollama_local_model,
                ollama_cloud_model,
                config,
            )
            .await?;
            Ok(ProviderRoute {
                args: vec!["--model".into(), model.clone()],
                envs: vec![
                    (
                        "OPENAI_BASE_URL".into(),
                        ollama_openai_base_url(ollama_base_url),
                    ),
                    // Required by OpenAI-compatible clients; ignored by Ollama.
                    ("OPENAI_API_KEY".into(), "ollama".into()),
                ],
                label: format!("Ollama via local daemon ({model})"),
            })
        }
        "openai" => {
            let key = read_env_file_var(kim_root, "OPENAI_API_KEY").ok_or_else(|| {
                "Codex with OpenAI needs OPENAI_API_KEY in the environment or Kim's .env."
                    .to_string()
            })?;
            let fallback = config
                .default_model
                .get("openai")
                .map(|s| s.as_str())
                .unwrap_or("openai/gpt-4o");
            let model = read_first_env_file_var(kim_root, &["CODEX_OPENAI_MODEL", "OPENAI_MODEL"])
                .unwrap_or_else(|| fallback.to_string());
            let mut envs = vec![("OPENAI_API_KEY".to_string(), key)];
            if let Some(base) = read_env_file_var(kim_root, "OPENAI_BASE_URL") {
                envs.push(("OPENAI_BASE_URL".into(), base));
            }
            Ok(ProviderRoute {
                args: vec!["--model".into(), model.clone()],
                envs,
                label: format!("OpenAI-compatible API ({model})"),
            })
        }
        "openai_oauth" => {
            // Local `openai-oauth` proxy: OpenAI-compatible, backed by the Codex
            // OAuth session in ~/.codex/auth.json, so there is no API key. The
            // dummy value satisfies OpenAI-compatible clients (same trick the
            // Ollama arm above uses) and the proxy ignores it.
            let fallback = config
                .default_model
                .get("openai_oauth")
                .map(|s| s.as_str())
                .unwrap_or("gpt-5.6-terra");
            let model = read_first_env_file_var(
                kim_root,
                &["CODEX_OPENAI_OAUTH_MODEL", "OPENAI_OAUTH_MODEL"],
            )
            .unwrap_or_else(|| fallback.to_string());
            let base = read_env_file_var(kim_root, "OPENAI_OAUTH_BASE_URL")
                .unwrap_or_else(|| "http://127.0.0.1:10531/v1".to_string());
            Ok(ProviderRoute {
                args: vec!["--model".into(), model.clone()],
                envs: vec![
                    ("OPENAI_API_KEY".into(), "openai-oauth".into()),
                    ("OPENAI_BASE_URL".into(), base),
                ],
                label: format!("OpenAI via ChatGPT OAuth proxy ({model})"),
            })
        }
        "deepseek" => {
            let key = read_env_file_var(kim_root, "DEEPSEEK_API_KEY").ok_or_else(|| {
                "Codex with DeepSeek needs DEEPSEEK_API_KEY in the environment or Kim's .env."
                    .to_string()
            })?;
            let fallback = config
                .default_model
                .get("deepseek")
                .map(|s| s.as_str())
                .unwrap_or("deepseek-chat");
            let model =
                read_first_env_file_var(kim_root, &["CODEX_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"])
                    .unwrap_or_else(|| fallback.to_string());
            let base = read_env_file_var(kim_root, "DEEPSEEK_BASE_URL")
                .unwrap_or_else(|| "https://api.deepseek.com/v1".to_string());
            Ok(ProviderRoute {
                args: vec!["--model".into(), model.clone()],
                envs: vec![
                    ("OPENAI_API_KEY".into(), key),
                    ("OPENAI_BASE_URL".into(), base),
                ],
                label: format!("DeepSeek API ({model})"),
            })
        }
        "gemini" => {
            let key = read_env_file_var(kim_root, "GOOGLE_API_KEY")
                .ok_or_else(|| "Codex with Gemini direct API needs GOOGLE_API_KEY in the environment or Kim's .env. Kim's Google OAuth token is only wired into the Chat provider path.".to_string())?;
            let fallback = config
                .default_model
                .get("gemini")
                .map(|s| s.as_str())
                .unwrap_or("gemini-2.0-flash");
            let model = read_first_env_file_var(kim_root, &["CODEX_GEMINI_MODEL", "GEMINI_MODEL"])
                .unwrap_or_else(|| fallback.to_string());
            let base = read_env_file_var(kim_root, "GEMINI_OPENAI_BASE_URL").unwrap_or_else(|| {
                "https://generativelanguage.googleapis.com/v1beta/openai".to_string()
            });
            Ok(ProviderRoute {
                args: vec!["--model".into(), model.clone()],
                envs: vec![
                    ("OPENAI_API_KEY".into(), key),
                    ("OPENAI_BASE_URL".into(), base),
                ],
                label: format!("Gemini OpenAI-compatible API ({model})"),
            })
        }
        _ => {
            let key = read_first_env_file_var(kim_root, &["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"])
                .ok_or_else(|| "Codex needs an Anthropic API key for Claude direct mode. Add ANTHROPIC_API_KEY to Kim's .env, or switch the provider dropdown to Ollama/Browser.".to_string())?;
            let mut envs = vec![("ANTHROPIC_API_KEY".to_string(), key)];
            for key in [
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_AUTH_TOKEN",
                "CODEX_MODEL",
                "CLAUDE_MODEL",
                "ANTHROPIC_MODEL",
            ] {
                if let Some(value) = read_env_file_var(kim_root, key) {
                    envs.push((key.to_string(), value));
                }
            }
            Ok(task_spec::ProviderRoute {
                args: vec![],
                envs,
                label: "Claude direct API".to_string(),
            })
        }
    }
}
