//! Which backend a provider routes through: DIRECT-at-Ollama, or the
//! `codex_engine.standalone_proxy` PROXY route (see `commands::tui` module
//! docs). Split out of the former single-file `commands/tui.rs` — pure
//! relocation, no behavior changes.

/// Opt-in escape hatch back to the legacy `ollama` DIRECT route (see
/// [`route_for_provider`] doc for why it is no longer the default).
const KIM_TUI_OLLAMA_DIRECT_ENV: &str = "KIM_TUI_OLLAMA_DIRECT";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TuiRoute {
    /// `provider == "ollama"` AND `KIM_TUI_OLLAMA_DIRECT=1`: kimcli talks
    /// directly to the local Ollama `/v1/responses` endpoint. No Python proxy
    /// involved. Opt-in only — see [`route_for_provider`].
    OllamaDirect,
    /// Everything else (`browser:*`, `claude`, `gemini`, `deepseek`, and
    /// `ollama` by default: see [`route_for_provider`]): route through
    /// `python -m codex_engine.standalone_proxy`.
    Proxy,
}

/// Routing table: `ollama` (exactly, case-insensitive) is PROXY by default —
/// every other provider name — including `ollama-cloud`, which is NOT the
/// same thing as `ollama` here — also goes through the proxy.
///
/// `ollama` used to be DIRECT unconditionally. Live testing on ollama 0.32.0
/// with qwen3-coder proved the direct route is broken for agentic use: codex
/// 0.144.3's built-in `/v1/responses` client sends tool calls in a shape that
/// Ollama's Responses-API emulation echoes back in a form codex's own tool
/// router then rejects — the child process spins with
/// `ERROR codex_core::tools::router: error=unsupported call` on every turn
/// with a tool call, until the relay budget dies. Plain text-only replies
/// work fine direct; anything agentic does not. The PROXY route sidesteps
/// this entirely: it wraps `OllamaProvider`, which talks Ollama's native
/// `/api/chat` tool-calling wire format and re-emits proper Responses-API
/// `function_call` items that codex's router accepts (see
/// `tests/test_kimcli_binary.py`'s E2E coverage of that path).
///
/// Set `KIM_TUI_OLLAMA_DIRECT=1` to opt back into the old DIRECT route (e.g.
/// to test a future Ollama release that fixes the tool-call shape). That
/// opt-in never applies to an Ollama CLOUD model (a `model` tag ending in
/// `-cloud` — see `ollama_cloud::resolve_ollama_cloud`) regardless of the
/// env var: the DIRECT route talks straight to the local daemon with no
/// Python/`OllamaProvider` involved, so it has no way to run the sign-in
/// flow a cloud model needs — always PROXY for those.
pub(crate) fn route_for_provider(provider: &str, model: &str) -> TuiRoute {
    route_for_provider_with_env(provider, model, |key| std::env::var(key).ok())
}

/// Env-injectable core of [`route_for_provider`] — kept separate so tests
/// can exercise both branches of the `KIM_TUI_OLLAMA_DIRECT` opt-in without
/// mutating real process env (see `crate::turn::provider_is_ready_with_env`
/// for the same pattern elsewhere in this crate).
fn route_for_provider_with_env<F>(provider: &str, model: &str, env_var: F) -> TuiRoute
where
    F: Fn(&str) -> Option<String>,
{
    let is_cloud_tagged_model = model.trim().to_ascii_lowercase().ends_with("-cloud");
    if provider.trim().eq_ignore_ascii_case("ollama")
        && !is_cloud_tagged_model
        && env_var(KIM_TUI_OLLAMA_DIRECT_ENV).as_deref() == Some("1")
    {
        TuiRoute::OllamaDirect
    } else {
        TuiRoute::Proxy
    }
}

#[cfg(test)]
mod tests {
    use super::{route_for_provider, route_for_provider_with_env, TuiRoute};

    #[test]
    fn routing_table_non_ollama_providers_always_use_the_real_entry_point() {
        // `route_for_provider` (the real-env entry point) for anything other
        // than the literal "ollama" must always be Proxy, regardless of
        // whatever KIM_TUI_OLLAMA_DIRECT happens to be in this test process
        // — the opt-in only affects the "ollama" branch.
        for provider in [
            "browser:claude",
            "browser:chatgpt",
            "browser",
            "claude",
            "gemini",
            "deepseek",
            "ollama-cloud",
            "",
        ] {
            assert_eq!(
                route_for_provider(provider, "some-model"),
                TuiRoute::Proxy,
                "{provider} should route through the proxy"
            );
        }
    }

    #[test]
    fn routing_table_ollama_env_opt_in_is_the_only_thing_that_flips_the_route() {
        // Exhaustively cover the opt-in branch via the injected-env core —
        // see the two tests below for the exact truth table.
        for provider in ["browser:claude", "claude", "gemini", "ollama-cloud", ""] {
            assert_eq!(
                route_for_provider_with_env(provider, "some-model", |_| Some("1".to_string())),
                TuiRoute::Proxy,
                "{provider} should route through the proxy even with the opt-in set"
            );
        }
    }

    #[test]
    fn ollama_defaults_to_proxy_without_the_opt_in_env_var() {
        assert_eq!(
            route_for_provider_with_env("ollama", "llama3.2", |_| None),
            TuiRoute::Proxy
        );
        assert_eq!(
            route_for_provider_with_env("OLLAMA", "llama3.2", |_| None),
            TuiRoute::Proxy
        );
        assert_eq!(
            route_for_provider_with_env("  ollama  ", "llama3.2", |_| None),
            TuiRoute::Proxy
        );
        // Any value other than exactly "1" is treated as unset.
        assert_eq!(
            route_for_provider_with_env("ollama", "llama3.2", |_| Some("true".to_string())),
            TuiRoute::Proxy
        );
        assert_eq!(
            route_for_provider_with_env("ollama", "llama3.2", |_| Some(String::new())),
            TuiRoute::Proxy
        );
    }

    #[test]
    fn ollama_is_direct_when_the_opt_in_env_var_is_set_to_1() {
        assert_eq!(
            route_for_provider_with_env("ollama", "llama3.2", |key| {
                (key == "KIM_TUI_OLLAMA_DIRECT").then(|| "1".to_string())
            }),
            TuiRoute::OllamaDirect
        );
        assert_eq!(
            route_for_provider_with_env("  OLLAMA  ", "llama3.2", |_| Some("1".to_string())),
            TuiRoute::OllamaDirect
        );
    }

    #[test]
    fn ollama_cloud_tagged_model_never_uses_direct_even_with_the_opt_in() {
        // The DIRECT route has no Python/OllamaProvider in the loop, so it
        // can't run the sign-in flow a cloud model needs — always Proxy for
        // a `-cloud` tagged model regardless of KIM_TUI_OLLAMA_DIRECT.
        assert_eq!(
            route_for_provider_with_env("ollama", "qwen3-coder:480b-cloud", |_| Some(
                "1".to_string()
            )),
            TuiRoute::Proxy
        );
        assert_eq!(
            route_for_provider_with_env("ollama", "QWEN3-CODER:480B-CLOUD", |_| Some(
                "1".to_string()
            )),
            TuiRoute::Proxy
        );
    }
}
