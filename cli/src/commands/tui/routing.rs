//! Which backend a provider routes through: DIRECT-at-Ollama, or the
//! `codex_engine.standalone_proxy` PROXY route (see `commands::tui` module
//! docs). Split out of the former single-file `commands/tui.rs` — pure
//! relocation, no behavior changes.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TuiRoute {
    /// `provider == "ollama"`: kimcli talks directly to the local Ollama
    /// `/v1/responses` endpoint. No Python proxy involved.
    OllamaDirect,
    /// Everything else (`browser:*`, `claude`, `gemini`, `deepseek`, …):
    /// route through `python -m codex_engine.standalone_proxy`.
    Proxy,
}

/// Routing table: `ollama` (exactly, case-insensitive) is DIRECT; every other
/// provider name — including `ollama-cloud`, which is NOT the same thing as
/// `ollama` here — goes through the proxy.
pub(crate) fn route_for_provider(provider: &str) -> TuiRoute {
    if provider.trim().eq_ignore_ascii_case("ollama") {
        TuiRoute::OllamaDirect
    } else {
        TuiRoute::Proxy
    }
}

#[cfg(test)]
mod tests {
    use super::{route_for_provider, TuiRoute};

    #[test]
    fn routing_table_ollama_is_direct_everything_else_is_proxy() {
        assert_eq!(route_for_provider("ollama"), TuiRoute::OllamaDirect);
        assert_eq!(route_for_provider("OLLAMA"), TuiRoute::OllamaDirect);
        assert_eq!(route_for_provider("  ollama  "), TuiRoute::OllamaDirect);
        // NOT the same as "ollama" — must route through the proxy.
        assert_eq!(route_for_provider("ollama-cloud"), TuiRoute::Proxy);
        for provider in [
            "browser:claude",
            "browser:chatgpt",
            "browser",
            "claude",
            "gemini",
            "deepseek",
            "",
        ] {
            assert_eq!(
                route_for_provider(provider),
                TuiRoute::Proxy,
                "{provider} should route through the proxy"
            );
        }
    }
}
