//! Ollama CLOUD-mode detection for `kim tui`/`kimcli`.
//!
//! FINDING 1 (issue #61 follow-up): `--model qwen3-coder:480b-cloud` alone
//! never reached `OllamaProvider` — `--model` only set kimcli's client-side
//! `-c model=` label, and `python -m codex_engine.standalone_proxy` (spawned
//! by `proxy::spawn_standalone_proxy`) inherits this launcher's environment
//! unfiltered (unlike the kimcli child, which goes through `env::
//! build_child_env`'s allowlist), so setting `KIM_OLLAMA_MODE`/
//! `KIM_OLLAMA_CLOUD_MODEL` on the PROXY's `Command` before spawning it is
//! all that's needed for `OllamaProvider.__init__`
//! (orchestrator/providers/ollama.py, via `_env_or_cfg`) to pick cloud mode
//! up — no config.yaml edit required.
//!
//! Two ways to ask for a cloud model, both resolved here:
//!   1. `--provider ollama-cloud --model <cloud-model>` — explicit alias.
//!      Mirrors the same alias already recognized by the REPL's code-mode
//!      gate (`crate::turn::code_mode_denied_reason`) and reserved by
//!      `routing.rs`'s own doc comment ("`ollama-cloud`, which is NOT the
//!      same thing as `ollama`").
//!   2. `--provider ollama --model <name>-cloud` — heuristic: any model tag
//!      ending in `-cloud` is Ollama's own naming convention for its
//!      cloud-hosted models (see `commands::providers::known_ollama_cloud_models`'s
//!      catalog — every entry ends in `-cloud`), so a plain `ollama` request
//!      for one of those is unambiguously a cloud request even without the
//!      alias.
//! `create_provider` (orchestrator/providers/base.py) only recognizes the
//! exact name `"ollama"` — never `"ollama-cloud"` — so both paths resolve to
//! `proxy_provider: "ollama"` for the `--provider` arg actually passed to
//! `python -m codex_engine.standalone_proxy`; the alias is purely a kimcli/
//! `kim tui` surface concept.

/// Provider alias that always means Ollama cloud mode, regardless of the
/// model name (case-insensitive, whitespace-trimmed).
const OLLAMA_CLOUD_PROVIDER_ALIAS: &str = "ollama-cloud";

/// Ollama's own naming convention for cloud-hosted model tags (e.g.
/// `qwen3-coder:480b-cloud`, `gpt-oss:120b-cloud`).
const OLLAMA_CLOUD_MODEL_SUFFIX: &str = "-cloud";

/// Resolved Ollama routing: the provider name to actually pass to
/// `python -m codex_engine.standalone_proxy --provider <name>`, and any
/// extra env vars to set on that child process. `extra_env` is empty for
/// every non-Ollama provider and for a plain local Ollama request — only a
/// detected cloud request carries `KIM_OLLAMA_MODE`/`KIM_OLLAMA_CLOUD_MODEL`.
pub(crate) struct OllamaCloudResolution {
    pub(crate) proxy_provider: String,
    pub(crate) extra_env: Vec<(String, String)>,
}

/// Decide whether `provider`/`model` means "run an Ollama cloud model" (see
/// module docs for the two recognized forms), and if so, what to spawn.
pub(crate) fn resolve_ollama_cloud(provider: &str, model: &str) -> OllamaCloudResolution {
    let trimmed_provider = provider.trim();
    let is_cloud_alias = trimmed_provider.eq_ignore_ascii_case(OLLAMA_CLOUD_PROVIDER_ALIAS);
    let is_plain_ollama = trimmed_provider.eq_ignore_ascii_case("ollama");

    let passthrough = || OllamaCloudResolution {
        proxy_provider: provider.to_string(),
        extra_env: Vec::new(),
    };

    if !is_cloud_alias && !is_plain_ollama {
        return passthrough();
    }

    let model_says_cloud = model
        .trim()
        .to_ascii_lowercase()
        .ends_with(OLLAMA_CLOUD_MODEL_SUFFIX);

    if !is_cloud_alias && !model_says_cloud {
        return passthrough();
    }

    OllamaCloudResolution {
        proxy_provider: "ollama".to_string(),
        extra_env: vec![
            ("KIM_OLLAMA_MODE".to_string(), "cloud".to_string()),
            (
                "KIM_OLLAMA_CLOUD_MODEL".to_string(),
                model.trim().to_string(),
            ),
        ],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plain_local_ollama_request_is_a_passthrough() {
        let r = resolve_ollama_cloud("ollama", "llama3.2:latest");
        assert_eq!(r.proxy_provider, "ollama");
        assert!(r.extra_env.is_empty());
    }

    #[test]
    fn non_ollama_providers_are_always_a_passthrough() {
        for provider in ["claude", "gemini", "browser:chatgpt", "deepseek", ""] {
            let r = resolve_ollama_cloud(provider, "qwen3-coder:480b-cloud");
            assert_eq!(r.proxy_provider, provider);
            assert!(
                r.extra_env.is_empty(),
                "{provider} must not force cloud env"
            );
        }
    }

    #[test]
    fn model_suffix_heuristic_forces_cloud_mode() {
        let r = resolve_ollama_cloud("ollama", "qwen3-coder:480b-cloud");
        assert_eq!(r.proxy_provider, "ollama");
        assert_eq!(
            r.extra_env,
            vec![
                ("KIM_OLLAMA_MODE".to_string(), "cloud".to_string()),
                (
                    "KIM_OLLAMA_CLOUD_MODEL".to_string(),
                    "qwen3-coder:480b-cloud".to_string()
                ),
            ]
        );
    }

    #[test]
    fn model_suffix_heuristic_is_case_insensitive() {
        let r = resolve_ollama_cloud("OLLAMA", "GPT-OSS:120B-CLOUD");
        assert_eq!(r.proxy_provider, "ollama");
        assert_eq!(r.extra_env[0].1, "cloud");
    }

    #[test]
    fn explicit_alias_forces_cloud_even_without_the_model_suffix() {
        let r = resolve_ollama_cloud("ollama-cloud", "my-custom-cloud-model");
        assert_eq!(r.proxy_provider, "ollama");
        assert_eq!(
            r.extra_env[0],
            ("KIM_OLLAMA_MODE".to_string(), "cloud".to_string())
        );
        assert_eq!(
            r.extra_env[1],
            (
                "KIM_OLLAMA_CLOUD_MODEL".to_string(),
                "my-custom-cloud-model".to_string()
            )
        );
    }

    #[test]
    fn explicit_alias_is_case_insensitive_and_trims_whitespace() {
        let r = resolve_ollama_cloud("  OLLAMA-CLOUD  ", "m");
        assert_eq!(r.proxy_provider, "ollama");
        assert!(!r.extra_env.is_empty());
    }
}
