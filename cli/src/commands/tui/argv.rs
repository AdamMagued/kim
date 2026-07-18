//! TOML `-c` override argv construction for the spawned kimcli/codex process.
//! Split out of the former single-file `commands/tui.rs` — pure relocation,
//! no behavior changes.

use super::routing::TuiRoute;

/// KIM_ENABLED_TOOL_TIERS value passed to the spawned kimcli's MCP `kim`
/// server. `ui` expands to `screen,mouse,keyboard,windows` and `browser`
/// expands to `web` (see `mcp_server/tool_tiers.py::TIER_ALIASES`). `shell`,
/// `file_write`, and `git` are intentionally excluded — `kim tui` grants
/// Kim's OS/browser-control tools, not arbitrary shell/file/git access.
const KIM_TUI_ENABLED_TOOL_TIERS: &str = "ui,browser";

/// Escape a string for embedding as a TOML basic string (the same quoting
/// `-c key="value"` codex/kimcli's config-override parser expects). Unlike
/// `codex_stream::sanitize_proxy_model` (which strips characters that would
/// break the generated TOML), this ESCAPES them — required here because the
/// values passed through this path include filesystem paths (which must
/// round-trip byte-for-byte, e.g. a `cwd` with a backslash on Windows or a
/// literal quote) rather than a cosmetic model name that can tolerate lossy
/// sanitization.
///
/// FIX 8 (#61 review): a TOML basic string forbids every raw control
/// character except tab (U+0000–U+0008, U+000A–U+001F, and U+007F must all
/// be escaped — TOML v1.0 spec, "Any Unicode character may be used except
/// those that must be escaped: quotation mark, backslash, and the control
/// characters other than tab"). The previous version only escaped
/// `\n`/`\r`/`\t`, so e.g. a path containing an ESC (U+001B) byte — unusual
/// but not impossible, and not under this program's control since paths
/// come from the filesystem/user input — produced an invalid `-c` override
/// that kimcli's TOML parser would reject. Named escapes are used where
/// TOML defines one (`\b`, `\t`, `\n`, `\f`, `\r`, `\"`, `\\`); every other
/// forbidden control character falls back to the generic `\u00XX` form.
fn toml_quote(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\u{08}' => out.push_str("\\b"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\u{0C}' => out.push_str("\\f"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 || c as u32 == 0x7F => {
                out.push_str(&format!("\\u{:04X}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// One `-c key="value"` pair as two argv elements.
fn c_override(key: &str, value: &str) -> [String; 2] {
    ["-c".to_string(), format!("{key}={}", toml_quote(value))]
}

/// One `-c key=<raw TOML literal>` pair (numbers, arrays, inline tables —
/// anything that must NOT be wrapped in quotes).
fn c_override_raw(key: &str, raw_value: &str) -> [String; 2] {
    ["-c".to_string(), format!("{key}={raw_value}")]
}

/// `-c` overrides that route kimcli at Kim's local codex_engine proxy
/// (non-ollama providers). `port`/`model` come from the proxy handshake and
/// the resolved TUI model respectively.
fn proxy_route_overrides(port: u16, model: &str) -> Vec<String> {
    let base_url = format!("http://127.0.0.1:{port}/v1");
    let mut out = Vec::with_capacity(12);
    out.extend(c_override("model_provider", "kim-proxy"));
    out.extend(c_override("model", model));
    out.extend(c_override("model_providers.kim-proxy.name", "Kim Proxy"));
    out.extend(c_override("model_providers.kim-proxy.base_url", &base_url));
    out.extend(c_override(
        "model_providers.kim-proxy.wire_api",
        "responses",
    ));
    out.extend(c_override(
        "model_providers.kim-proxy.env_key",
        "CODEX_API_KEY",
    ));
    out
}

/// `-c` overrides that route kimcli directly at the local Ollama
/// `/v1/responses` endpoint (`provider == "ollama"` AND
/// `KIM_TUI_OLLAMA_DIRECT=1` — see `routing.rs`; NOT the default route as of
/// this override set's current form, kept opt-in for testing/parity because
/// ollama 0.32.0's tool-call shape on this endpoint is rejected by codex
/// 0.144.3's tool router). Ollama gained an OpenAI-Responses-compatible
/// endpoint in codex's built-in provider
/// (`create_oss_provider(11434, WireApi::Responses)`); an Ollama older than
/// ~0.13.4 without `/v1/responses` will surface a generic HTTP error here
/// rather than a Kim-specific one — a known limitation, not a bug in this
/// override set.
fn ollama_direct_overrides(model: &str) -> Vec<String> {
    let mut out = Vec::with_capacity(12);
    out.extend(c_override("model_provider", "kim-ollama"));
    out.extend(c_override("model", model));
    out.extend(c_override("model_providers.kim-ollama.name", "Kim Ollama"));
    out.extend(c_override(
        "model_providers.kim-ollama.base_url",
        "http://127.0.0.1:11434/v1",
    ));
    out.extend(c_override(
        "model_providers.kim-ollama.wire_api",
        "responses",
    ));
    out.extend(c_override(
        "model_providers.kim-ollama.env_key",
        "CODEX_API_KEY",
    ));
    out
}

/// `-c mcp_servers.kim.*` overrides shared by both routes: wires kimcli's MCP
/// client at Kim's own `mcp_server.server`, scoped to the `ui,browser` tool
/// tiers and the target working directory.
fn mcp_kim_overrides(python: &str, kim_root: &str, target_cwd: &str) -> Vec<String> {
    let mut out = Vec::with_capacity(12);
    out.extend(c_override("mcp_servers.kim.command", python));
    out.extend(c_override_raw(
        "mcp_servers.kim.args",
        "[\"-m\",\"mcp_server.server\"]",
    ));
    out.extend(c_override("mcp_servers.kim.cwd", kim_root));
    out.extend(c_override_raw(
        "mcp_servers.kim.env",
        &format!(
            "{{PROJECT_ROOT={},KIM_ENABLED_TOOL_TIERS={}}}",
            toml_quote(target_cwd),
            toml_quote(KIM_TUI_ENABLED_TOOL_TIERS)
        ),
    ));
    out.extend(c_override_raw("mcp_servers.kim.startup_timeout_sec", "30"));
    out.extend(c_override_raw("mcp_servers.kim.tool_timeout_sec", "120"));
    out
}

/// Full argv (minus the binary itself) for the spawned kimcli/codex process:
/// route-specific model-provider overrides, then the shared `mcp_servers.kim`
/// block, then the user's raw `-- <passthrough>` args appended verbatim.
pub(crate) fn build_kimcli_argv(
    route: TuiRoute,
    model: &str,
    proxy_port: Option<u16>,
    python: &str,
    kim_root: &str,
    target_cwd: &str,
    passthrough: &[String],
) -> Vec<String> {
    let mut args = match route {
        TuiRoute::OllamaDirect => ollama_direct_overrides(model),
        TuiRoute::Proxy => {
            let port = proxy_port.expect("TuiRoute::Proxy requires a handshake port");
            proxy_route_overrides(port, model)
        }
    };
    args.extend(mcp_kim_overrides(python, kim_root, target_cwd));
    args.extend(passthrough.iter().cloned());
    args
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proxy_route_argv_matches_the_trusted_design_exactly() {
        let argv = build_kimcli_argv(
            TuiRoute::Proxy,
            "gpt-5-codex",
            Some(45999),
            "/usr/bin/python3",
            "/home/kim/kim-pro",
            "/home/kim/my-project",
            &[],
        );
        let joined = argv.join(" ");
        for expected in [
            r#"model_provider="kim-proxy""#,
            r#"model="gpt-5-codex""#,
            r#"model_providers.kim-proxy.name="Kim Proxy""#,
            r#"model_providers.kim-proxy.base_url="http://127.0.0.1:45999/v1""#,
            r#"model_providers.kim-proxy.wire_api="responses""#,
            r#"model_providers.kim-proxy.env_key="CODEX_API_KEY""#,
            r#"mcp_servers.kim.command="/usr/bin/python3""#,
            r#"mcp_servers.kim.args=["-m","mcp_server.server"]"#,
            r#"mcp_servers.kim.cwd="/home/kim/kim-pro""#,
            r#"mcp_servers.kim.env={PROJECT_ROOT="/home/kim/my-project",KIM_ENABLED_TOOL_TIERS="ui,browser"}"#,
            r#"mcp_servers.kim.startup_timeout_sec=30"#,
            r#"mcp_servers.kim.tool_timeout_sec=120"#,
        ] {
            assert!(
                joined.contains(expected),
                "missing {expected:?} in: {joined}"
            );
        }
        // Every override is a `-c <key=value>` pair — count -c occurrences.
        assert_eq!(argv.iter().filter(|a| a.as_str() == "-c").count(), 12);
    }

    #[test]
    fn ollama_direct_argv_matches_the_trusted_design_exactly() {
        let argv = build_kimcli_argv(
            TuiRoute::OllamaDirect,
            "qwen2.5-coder:7b",
            None,
            "/usr/bin/python3",
            "/home/kim/kim-pro",
            "/home/kim/my-project",
            &[],
        );
        let joined = argv.join(" ");
        for expected in [
            r#"model_provider="kim-ollama""#,
            r#"model="qwen2.5-coder:7b""#,
            r#"model_providers.kim-ollama.name="Kim Ollama""#,
            r#"model_providers.kim-ollama.base_url="http://127.0.0.1:11434/v1""#,
            r#"model_providers.kim-ollama.wire_api="responses""#,
            r#"model_providers.kim-ollama.env_key="CODEX_API_KEY""#,
            r#"mcp_servers.kim.command="/usr/bin/python3""#,
        ] {
            assert!(
                joined.contains(expected),
                "missing {expected:?} in: {joined}"
            );
        }
        // No kim-proxy provider keys must appear on the direct route.
        assert!(!joined.contains("kim-proxy"));
    }

    #[test]
    #[should_panic(expected = "TuiRoute::Proxy requires a handshake port")]
    fn proxy_route_without_a_port_panics_defensively() {
        let _ = build_kimcli_argv(
            TuiRoute::Proxy,
            "model",
            None,
            "/usr/bin/python3",
            "/root",
            "/cwd",
            &[],
        );
    }

    #[test]
    fn argv_handles_a_path_with_spaces_exactly() {
        let argv = build_kimcli_argv(
            TuiRoute::OllamaDirect,
            "model",
            None,
            "/usr/bin/python3",
            "/Users/me/My Kim Repo",
            "/Users/me/My Projects/app",
            &[],
        );
        let joined = argv.join(" ");
        assert!(
            joined.contains(r#"mcp_servers.kim.cwd="/Users/me/My Kim Repo""#),
            "got: {joined}"
        );
        assert!(
            joined.contains(r#"PROJECT_ROOT="/Users/me/My Projects/app""#),
            "got: {joined}"
        );
    }

    #[test]
    fn argv_escapes_quotes_and_backslashes_in_paths() {
        // A Windows-style path (backslashes) and a path containing a literal
        // quote must round-trip as valid TOML strings, not break out of them.
        let argv = build_kimcli_argv(
            TuiRoute::OllamaDirect,
            "model",
            None,
            r"C:\Users\me\python.exe",
            r#"C:\kim"repo"#,
            "/cwd",
            &[],
        );
        let joined = argv.join(" ");
        assert!(
            joined.contains(r#"mcp_servers.kim.command="C:\\Users\\me\\python.exe""#),
            "got: {joined}"
        );
        assert!(
            joined.contains(r#"mcp_servers.kim.cwd="C:\\kim\"repo""#),
            "got: {joined}"
        );
    }

    #[test]
    fn toml_quote_escapes_control_chars_below_0x20_and_0x7f() {
        // FIX 8 (#61 review): every raw control char except tab must be
        // escaped in a TOML basic string, not just \n\r\t. Built via
        // char::from_u32 at runtime (rather than literal escapes in this
        // source file) so this file never has to embed a raw control byte.
        let esc = char::from_u32(0x1B).unwrap();
        let nul = char::from_u32(0x00).unwrap();
        let bel = char::from_u32(0x07).unwrap();
        let del = char::from_u32(0x7F).unwrap();
        let bs = char::from_u32(0x08).unwrap();
        let ff = char::from_u32(0x0C).unwrap();

        assert_eq!(
            toml_quote(&format!("a{esc}b")),
            format!("\"a\\u{:04X}b\"", 0x1Bu32),
            "ESC (U+001B)"
        );
        assert_eq!(
            toml_quote(&format!("a{nul}b")),
            format!("\"a\\u{:04X}b\"", 0x00u32),
            "NUL (U+0000)"
        );
        assert_eq!(
            toml_quote(&format!("a{bel}b")),
            format!("\"a\\u{:04X}b\"", 0x07u32),
            "BEL (U+0007)"
        );
        assert_eq!(
            toml_quote(&format!("a{del}b")),
            format!("\"a\\u{:04X}b\"", 0x7Fu32),
            "DEL (U+007F)"
        );
        // Named escapes still take priority over the generic \u00XX form.
        assert_eq!(
            toml_quote(&format!("a{bs}b")),
            "\"a\\bb\"",
            "backspace uses \\b"
        );
        assert_eq!(
            toml_quote(&format!("a{ff}b")),
            "\"a\\fb\"",
            "form feed uses \\f"
        );
        assert_eq!(toml_quote("a\tb"), "\"a\\tb\"");
        assert_eq!(toml_quote("a\nb"), "\"a\\nb\"");
        assert_eq!(toml_quote("a\rb"), "\"a\\rb\"");

        // A control-char path must still produce a well-formed `-c`
        // override argv element -- the raw control byte never survives
        // into the generated TOML.
        let target_cwd = format!("/cwd/proj{esc}ect");
        let argv = build_kimcli_argv(
            TuiRoute::OllamaDirect,
            "model",
            None,
            "/usr/bin/python3",
            "/root",
            &target_cwd,
            &[],
        );
        let joined = argv.join(" ");
        assert!(
            joined.contains("PROJECT_ROOT=\"/cwd/proj\\u001Bect\""),
            "got: {joined}"
        );
        assert!(!joined.contains(esc), "raw ESC byte must not survive");
    }
    #[test]
    fn argv_appends_passthrough_verbatim_at_the_end() {
        let passthrough: Vec<String> = ["--some-codex-flag", "value with spaces"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let argv = build_kimcli_argv(
            TuiRoute::OllamaDirect,
            "model",
            None,
            "/usr/bin/python3",
            "/root",
            "/cwd",
            &passthrough,
        );
        assert_eq!(&argv[argv.len() - 2..], &passthrough[..]);
    }
}
