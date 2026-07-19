//! `kim tui` launcher + the standalone `kimcli` binary's shared entry point.
//!
//! Launches the full Codex-style TUI (the `kimcli` binary — a rebranded fork
//! of `codex`) as a child process that inherits the terminal, routed at one
//! of Kim's providers:
//!
//! - every provider, INCLUDING `ollama` by default, → PROXY route: spawn
//!   `python -m codex_engine.standalone_proxy --provider <name>`, read its
//!   one-line JSON handshake
//!   (`{"event":"ready","port":...,"token":"..."}` or
//!   `{"event":"fatal","message":"..."}`), then point kimcli at that proxy.
//!   For `ollama` the proxy wraps `OllamaProvider`, which speaks Ollama's
//!   native `/api/chat` tool-calling wire format and re-emits Responses-API
//!   `function_call` items codex's tool router accepts.
//! - `ollama` ONLY, opt-in via `KIM_TUI_OLLAMA_DIRECT=1` → DIRECT route:
//!   kimcli talks straight to the local Ollama `/v1/responses` endpoint (no
//!   Python involved). Kept for testing/parity only — see `routing.rs` for
//!   why this stopped being the default (ollama 0.32.0's tool-call shape on
//!   that endpoint is rejected by codex 0.144.3's tool router).
//!
//! Both routes wire up `mcp_servers.kim` so kimcli gets Kim's MCP tools
//! (scoped to the `ui,browser` tiers) alongside the model connection.
//!
//! `kim tui` (via the `kim` binary) and the standalone `kimcli` binary are the
//! exact same entry point (`run_tui_standalone`) — see `cli/Cargo.toml`'s two
//! `[[bin]]` targets and `src/bin/kimcli.rs`.
//!
//! Split into focused submodules (file-size gate): `routing` (the
//! ollama-direct-opt-in/proxy routing table), `resolve` (kimcli/codex binary
//! resolution), `argv` (the `-c` override argv builders), `proxy` (the
//! standalone proxy's handshake/spawn/drain/teardown), `env` (the child
//! process env allowlist). This file is the entry point + orchestration.

mod argv;
mod env;
mod ollama_cloud;
mod proxy;
mod resolve;
mod routing;

// Re-exported so `commands::doctor` can reach these without reaching into the
// submodules directly (`kim doctor`'s kimcli section: binary resolution +
// version, and a proxy dry-check reusing the same handshake reader/types).
pub(crate) use proxy::{read_proxy_handshake_with_timeout, ProxyHandshake};
pub(crate) use resolve::resolve_kimcli_binary;

use std::path::PathBuf;

use tokio::process::Command;

use argv::build_kimcli_argv;
use env::{build_child_env, effort_env};
use ollama_cloud::resolve_ollama_cloud;
use proxy::{spawn_standalone_proxy, ProxyProcess};
use routing::{route_for_provider, TuiRoute};

/// `kim tui` falls back to this provider when neither `--provider` nor
/// `config.tui_provider` is set.
const DEFAULT_TUI_PROVIDER: &str = "browser:chatgpt";

/// Valid `--effort` values — mirrors
/// `orchestrator/providers/browser/reasoning_effort.py::EFFORT_LEVELS`.
const EFFORT_LEVELS: [&str; 3] = ["low", "medium", "high"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TuiOptions {
    pub(crate) provider: String,
    pub(crate) model: String,
    pub(crate) cwd: PathBuf,
    pub(crate) verbose: bool,
    pub(crate) passthrough: Vec<String>,
    pub(crate) effort: Option<String>,
}

/// Parsed `--provider`/`--model`/`--cwd`/`--effort`/`--verbose`/
/// `-- <passthrough>` flags, before defaults (config / cwd) are applied.
/// Shared verbatim by `kim tui` and the standalone `kimcli` binary (see
/// module docs).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(crate) struct TuiFlags {
    pub(crate) provider: Option<String>,
    pub(crate) model: Option<String>,
    pub(crate) cwd: Option<PathBuf>,
    pub(crate) verbose: bool,
    pub(crate) passthrough: Vec<String>,
    pub(crate) effort: Option<String>,
}

const TUI_USAGE: &str = "Usage: kim tui [--provider <name>] [--model <name>] [--cwd <dir>] \
     [--effort <low|medium|high>] [--verbose] [-- <args...>]";

/// Parse `kim tui`/`kimcli` flags. Recognizes `--provider <name>`,
/// `--model <name>`, `--cwd <dir>`, `--effort <low|medium|high>`,
/// `--verbose`, and a trailing `-- <raw args...>` passthrough (everything
/// after `--` is forwarded verbatim to the kimcli binary, unparsed).
pub(crate) fn parse_tui_flags(args: &[String]) -> Result<TuiFlags, String> {
    let mut out = TuiFlags::default();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--" => {
                out.passthrough = args[i + 1..].to_vec();
                return Ok(out);
            }
            "--provider" => {
                let value = args
                    .get(i + 1)
                    .ok_or_else(|| format!("kim tui: --provider requires a value. {TUI_USAGE}"))?;
                out.provider = Some(value.clone());
                i += 2;
            }
            "--model" => {
                let value = args
                    .get(i + 1)
                    .ok_or_else(|| format!("kim tui: --model requires a value. {TUI_USAGE}"))?;
                out.model = Some(value.clone());
                i += 2;
            }
            "--cwd" => {
                let value = args
                    .get(i + 1)
                    .ok_or_else(|| format!("kim tui: --cwd requires a value. {TUI_USAGE}"))?;
                out.cwd = Some(PathBuf::from(value));
                i += 2;
            }
            "--effort" => {
                let value = args
                    .get(i + 1)
                    .ok_or_else(|| format!("kim tui: --effort requires a value. {TUI_USAGE}"))?;
                let normalized = value.trim().to_ascii_lowercase();
                if !EFFORT_LEVELS.contains(&normalized.as_str()) {
                    return Err(format!(
                        "kim tui: --effort must be one of {} (got {value:?}). {TUI_USAGE}",
                        EFFORT_LEVELS.join(", "),
                    ));
                }
                out.effort = Some(normalized);
                i += 2;
            }
            "--verbose" => {
                out.verbose = true;
                i += 1;
            }
            other => {
                return Err(format!("kim tui: unknown option '{other}'. {TUI_USAGE}"));
            }
        }
    }
    Ok(out)
}

/// Resolve the effective (provider, model) pair an explicit `--provider`
/// flag must win for BOTH: routing (unchanged) AND the display label kimcli
/// shows as its header/footer/model-metadata lookup.
///
/// F-B-16: a browser provider's "model" is a fixed, cosmetic label derived
/// from the provider itself (browser-chatgpt/-gemini/-deepseek) — not a real
/// installed model. Persisted `tui_model`/`model` can be stale from a PRIOR
/// `--provider` run (e.g. last session used browser:chatgpt) and must not
/// leak into THIS session's header/footer/"Model metadata for … not found"
/// warning: execution already correctly used the resolved `provider` for
/// routing, but the display label was reading old, unrelated config. Non-
/// browser providers keep persisted-model precedence unchanged — that
/// string is a real user choice (an installed Ollama tag, etc.) worth
/// remembering across runs.
pub(crate) fn resolve_provider_and_model(
    flags: &TuiFlags,
    config: &crate::config::KimConfig,
) -> (String, String) {
    let provider = flags.provider.clone().unwrap_or_else(|| {
        config
            .tui_provider
            .clone()
            .unwrap_or_else(|| DEFAULT_TUI_PROVIDER.to_string())
    });
    let model = flags.model.clone().unwrap_or_else(|| {
        if crate::provider::is_browser_provider(&provider) {
            if let Some(info) = crate::provider::provider_info(&provider) {
                return info.default_model.to_string();
            }
        }
        config
            .tui_model
            .clone()
            .unwrap_or_else(|| config.model.clone())
    });
    (provider, model)
}

/// Resolve `--cwd` into the absolute directory used both as the spawned
/// kimcli process's own working directory AND (via `argv.rs`'s
/// `mcp_kim_overrides`) as `mcp_servers.kim.env.PROJECT_ROOT`.
///
/// FIX 3 (#61 review): an explicit `--cwd` MUST be canonicalized here. The
/// MCP server's own process cwd is pinned to `kim_root`
/// (`mcp_servers.kim.cwd` in `argv.rs`), and `mcp_server/config.py`'s
/// `PROJECT_ROOT` resolution deliberately resolves a *relative*
/// `PROJECT_ROOT` against its own directory (`_PROJECT_DIR`, i.e.
/// `kim_root`), NOT the caller's intended project directory — see that
/// file's "Resolve project_root relative to config.yaml's directory — NOT
/// cwd" comment. So `kim tui --cwd ./myproj` without this canonicalization
/// would root every MCP tool at `<kim_root>/myproj` instead of the user's
/// actual project. `std::env::current_dir()` (the no-flag default) already
/// returns an absolute path, so this only changes behavior for an explicit
/// relative `--cwd`. Falls back to the given (possibly relative/
/// non-existent) path if canonicalization fails, so a typo'd or
/// not-yet-created `--cwd` surfaces its own clear error later (e.g. kimcli
/// failing to spawn with that cwd) rather than a silent swap here.
fn resolve_tui_cwd(flag_cwd: Option<PathBuf>) -> PathBuf {
    flag_cwd
        .map(|dir| dir.canonicalize().unwrap_or(dir))
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")))
}

/// Standalone `kimcli` binary's entry point AND `kim tui`'s dispatch target
/// (see `lib.rs`'s `CliCommand::Tui` arm) — the exact same function serves
/// both. Parses flags, resolves defaults from `KimConfig`, and runs.
pub async fn run_tui_standalone(args: Vec<String>) -> i32 {
    let flags = match parse_tui_flags(&args) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let config = crate::config::KimConfig::load();
    let (provider, model) = resolve_provider_and_model(&flags, &config);
    let cwd = resolve_tui_cwd(flags.cwd);

    let opts = TuiOptions {
        provider,
        model,
        cwd,
        verbose: flags.verbose,
        passthrough: flags.passthrough,
        effort: flags.effort,
    };
    run_tui(&opts).await
}

/// Full launch: resolve the Kim repo root + Python + kimcli binary, set up
/// the route (spawn+handshake the proxy, or — opt-in only — go direct at
/// Ollama), spawn
/// kimcli as a child inheriting this terminal's stdio, wait for it while
/// swallowing our own Ctrl-C so we don't die before it's reaped, tear the
/// proxy down, and propagate kimcli's exit code.
pub(crate) async fn run_tui(opts: &TuiOptions) -> i32 {
    let route = route_for_provider(&opts.provider, &opts.model);

    let kim_root = match crate::sessions::find_kim_repo_root() {
        Some(root) => root,
        None => {
            eprintln!(
                "kim tui: Kim source root not found. Run install.sh to write ~/.kim_root, set \
                 KIM_PROJECT_ROOT, or run kim from inside the Kim repo directory."
            );
            return 1;
        }
    };
    let python = match crate::agentic::find_python(&kim_root) {
        Some(p) => p,
        None => {
            eprintln!(
                "kim tui: no Python interpreter found (tried the repo venv, python3, python)."
            );
            return 1;
        }
    };
    let kimcli_bin = match resolve_kimcli_binary() {
        Ok(path) => path,
        Err(e) => {
            eprintln!("kim tui: {e}");
            return 1;
        }
    };

    let mut proxy: Option<ProxyProcess> = None;
    let (extra_env, argv): (Vec<(String, String)>, Vec<String>) = match route {
        TuiRoute::OllamaDirect => {
            let argv = build_kimcli_argv(
                route,
                &opts.model,
                None,
                &python.to_string_lossy(),
                &kim_root.to_string_lossy(),
                &opts.cwd.to_string_lossy(),
                &opts.passthrough,
            );
            // Dummy token mirrors codex_stream.rs's convention for ollama —
            // there is no real bearer token to check on that path.
            let env = vec![
                ("CODEX_API_KEY".to_string(), "ollama".to_string()),
                ("OPENAI_API_KEY".to_string(), "ollama".to_string()),
            ];
            (env, argv)
        }
        TuiRoute::Proxy => {
            // FINDING 1 (#61): `--model <name>-cloud` (or an explicit
            // `--provider ollama-cloud`) must actually reach
            // OllamaProvider as cloud mode — see ollama_cloud.rs. The
            // resolved `proxy_provider` is what `create_provider`
            // (orchestrator/providers/base.py) understands; the alias is
            // a kim-tui-only surface concept.
            let cloud = resolve_ollama_cloud(&opts.provider, &opts.model);
            let spawned = match spawn_standalone_proxy(
                &python,
                &kim_root,
                &cloud.proxy_provider,
                opts.verbose,
                &cloud.extra_env,
            )
            .await
            {
                Ok(p) => p,
                Err(e) => {
                    eprintln!("kim tui: {e}");
                    return 1;
                }
            };
            let argv = build_kimcli_argv(
                route,
                &opts.model,
                Some(spawned.port),
                &python.to_string_lossy(),
                &kim_root.to_string_lossy(),
                &opts.cwd.to_string_lossy(),
                &opts.passthrough,
            );
            let env = vec![
                ("CODEX_API_KEY".to_string(), spawned.token.clone()),
                ("OPENAI_API_KEY".to_string(), spawned.token.clone()),
            ];
            proxy = Some(spawned);
            (env, argv)
        }
    };

    let mut cmd = Command::new(&kimcli_bin);
    cmd.args(&argv).current_dir(&opts.cwd).env_clear();
    for (key, value) in build_child_env() {
        cmd.env(key, value);
    }
    for (key, value) in &extra_env {
        cmd.env(key, value);
    }
    // `--effort` (Goal C): sets KIM_BROWSER_EFFORT on the child so the
    // browser-contract path's existing env>config resolution
    // (reasoning_effort.py::resolve_effort) picks it up — no Python change
    // needed. A no-flag run leaves this unset, same as before.
    for (key, value) in effort_env(opts.effort.as_deref()) {
        cmd.env(key, value);
    }
    // Inherit stdio: the TUI owns the terminal (no piping/capturing).
    cmd.kill_on_drop(true);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("kim tui: failed to start {}: {e}", kimcli_bin.display());
            if let Some(p) = proxy.take() {
                p.shutdown().await;
            }
            return 1;
        }
    };

    // Ctrl-C: the terminal delivers SIGINT to our whole process group
    // (including kimcli), so we must NOT exit before kimcli has a chance to
    // handle it and get reaped below — swallow our own SIGINT and keep
    // waiting on the child.
    let wait_result = loop {
        tokio::select! {
            status = child.wait() => break status,
            _ = tokio::signal::ctrl_c() => continue,
        }
    };

    if let Some(p) = proxy.as_mut() {
        if p.has_exited() {
            eprintln!("kim tui: note — the codex proxy process exited before kimcli did.");
        }
    }
    if let Some(p) = proxy.take() {
        p.shutdown().await;
    }

    match wait_result {
        Ok(status) => status.code().unwrap_or(1),
        Err(e) => {
            eprintln!("kim tui: error waiting for kimcli: {e}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strs(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn parse_tui_flags_defaults_are_empty() {
        let flags = parse_tui_flags(&[]).unwrap();
        assert_eq!(flags, TuiFlags::default());
    }

    #[test]
    fn parse_tui_flags_reads_all_named_flags() {
        let flags = parse_tui_flags(&strs(&[
            "--provider",
            "browser:chatgpt",
            "--model",
            "gpt-5",
            "--cwd",
            "/tmp/proj",
            "--verbose",
        ]))
        .unwrap();
        assert_eq!(flags.provider.as_deref(), Some("browser:chatgpt"));
        assert_eq!(flags.model.as_deref(), Some("gpt-5"));
        assert_eq!(flags.cwd, Some(PathBuf::from("/tmp/proj")));
        assert!(flags.verbose);
        assert!(flags.passthrough.is_empty());
    }

    #[test]
    fn parse_tui_flags_passthrough_after_double_dash() {
        let flags = parse_tui_flags(&strs(&["--verbose", "--", "--foo", "bar"])).unwrap();
        assert!(flags.verbose);
        assert_eq!(flags.passthrough, vec!["--foo", "bar"]);
    }

    #[test]
    fn parse_tui_flags_missing_value_is_an_error() {
        for argv in [
            vec!["--provider"],
            vec!["--model"],
            vec!["--cwd"],
            vec!["--effort"],
        ] {
            let err = parse_tui_flags(&strs(&argv)).unwrap_err();
            assert!(err.contains("requires a value"), "got: {err}");
        }
    }

    // ── Goal C: --effort flag ───────────────────────────────────────────────

    #[test]
    fn parse_tui_flags_effort_absent_is_unset() {
        let flags = parse_tui_flags(&[]).unwrap();
        assert_eq!(flags.effort, None);
    }

    #[test]
    fn parse_tui_flags_effort_accepts_each_valid_level() {
        for level in ["low", "medium", "high"] {
            let flags = parse_tui_flags(&strs(&["--effort", level])).unwrap();
            assert_eq!(flags.effort.as_deref(), Some(level));
        }
    }

    #[test]
    fn parse_tui_flags_effort_is_case_insensitive() {
        let flags = parse_tui_flags(&strs(&["--effort", "HIGH"])).unwrap();
        assert_eq!(flags.effort.as_deref(), Some("high"));
    }

    #[test]
    fn parse_tui_flags_effort_rejects_invalid_value() {
        let err = parse_tui_flags(&strs(&["--effort", "ultra"])).unwrap_err();
        assert!(err.contains("--effort must be one of"), "got: {err}");
        assert!(err.contains("low, medium, high"), "got: {err}");
    }

    #[test]
    fn parse_tui_flags_effort_combines_with_other_flags() {
        let flags = parse_tui_flags(&strs(&[
            "--provider",
            "browser:chatgpt",
            "--effort",
            "medium",
            "--verbose",
        ]))
        .unwrap();
        assert_eq!(flags.provider.as_deref(), Some("browser:chatgpt"));
        assert_eq!(flags.effort.as_deref(), Some("medium"));
        assert!(flags.verbose);
    }

    #[test]
    fn parse_tui_flags_unknown_option_is_an_error() {
        let err = parse_tui_flags(&strs(&["--nope"])).unwrap_err();
        assert!(err.contains("--nope"), "got: {err}");
        assert!(err.contains("Usage: kim tui"), "got: {err}");
    }

    // ── F-B-16: explicit --provider must win the DISPLAY label too ────────

    fn config_with(
        tui_provider: Option<&str>,
        tui_model: Option<&str>,
        model: &str,
    ) -> crate::config::KimConfig {
        crate::config::KimConfig {
            tui_provider: tui_provider.map(str::to_string),
            tui_model: tui_model.map(str::to_string),
            model: model.to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn explicit_provider_flag_derives_the_matching_browser_model_label() {
        // The exact QA repro: `kim tui --provider browser:chatgpt` with a
        // stale `tui_model: "browser-gemini"` left over from a prior
        // `--provider browser:gemini` session.
        let flags = TuiFlags {
            provider: Some("browser:chatgpt".to_string()),
            ..Default::default()
        };
        let config = config_with(
            Some("browser:gemini"),
            Some("browser-gemini"),
            "browser-gemini",
        );
        let (provider, model) = resolve_provider_and_model(&flags, &config);
        assert_eq!(provider, "browser:chatgpt");
        assert_eq!(
            model, "browser-chatgpt",
            "display label must match the flag, not stale config"
        );
    }

    #[test]
    fn explicit_model_flag_always_wins_even_for_browser_providers() {
        let flags = TuiFlags {
            provider: Some("browser:gemini".to_string()),
            model: Some("custom-label".to_string()),
            ..Default::default()
        };
        let config = config_with(None, None, "llama3.2");
        let (provider, model) = resolve_provider_and_model(&flags, &config);
        assert_eq!(provider, "browser:gemini");
        assert_eq!(model, "custom-label");
    }

    #[test]
    fn no_flags_falls_back_to_persisted_provider_and_its_matching_model() {
        let flags = TuiFlags::default();
        let config = config_with(
            Some("browser:chatgpt"),
            Some("stale-unrelated-label"),
            "llama3.2",
        );
        let (provider, model) = resolve_provider_and_model(&flags, &config);
        assert_eq!(provider, "browser:chatgpt");
        // Even the PERSISTED provider's own stale tui_model gets corrected —
        // the derivation is keyed off the resolved provider, not off which
        // input (flag vs config) supplied it.
        assert_eq!(model, "browser-chatgpt");
    }

    #[test]
    fn non_browser_provider_keeps_persisted_model_precedence() {
        // A real installed Ollama tag must NOT be clobbered by any
        // browser-style derivation — ollama has no provider-derived model.
        let flags = TuiFlags {
            provider: Some("ollama".to_string()),
            ..Default::default()
        };
        let config = config_with(None, Some("qwen2.5-coder:7b"), "llama3.2");
        let (provider, model) = resolve_provider_and_model(&flags, &config);
        assert_eq!(provider, "ollama");
        assert_eq!(model, "qwen2.5-coder:7b");
    }

    #[test]
    fn no_flags_and_no_config_falls_back_to_default_provider_and_model() {
        let flags = TuiFlags::default();
        let config = config_with(None, None, "llama3.2");
        let (provider, model) = resolve_provider_and_model(&flags, &config);
        assert_eq!(provider, DEFAULT_TUI_PROVIDER);
        assert_eq!(model, "browser-chatgpt");
    }

    // ── FIX 3 (#61 review): --cwd must be canonicalized ────────────────────

    #[test]
    fn resolve_tui_cwd_canonicalizes_and_normalizes_an_explicit_cwd() {
        // Regression for FIX 3 (#61 review): resolve_tui_cwd must actually
        // canonicalize (not just pass through) an explicit --cwd, since an
        // unresolved --cwd flows straight into mcp_servers.kim.env.
        // PROJECT_ROOT, and mcp_server/config.py resolves a *relative*
        // PROJECT_ROOT against kim_root (its own directory), not the user's
        // intended project — rooting every MCP tool at the wrong place.
        // Deliberately avoids mutating the real process cwd (which would be
        // racy under parallel test execution): build a path with a
        // redundant `..` component instead, so canonicalize()'s
        // normalization is directly observable without touching global
        // state.
        let base = tempfile::tempdir().unwrap();
        let sub = base.path().join("myproj");
        std::fs::create_dir_all(&sub).unwrap();
        let unresolved = base.path().join("myproj").join("..").join("myproj");
        assert_ne!(
            unresolved, sub,
            "test fixture sanity: the unresolved path must be syntactically different"
        );

        let resolved = resolve_tui_cwd(Some(unresolved));
        assert_eq!(resolved, sub.canonicalize().unwrap());
        assert!(
            resolved.is_absolute(),
            "PROJECT_ROOT must not stay relative — mcp_server/config.py \
             resolves a relative PROJECT_ROOT against kim_root, not the \
             user's --cwd"
        );
    }

    #[test]
    fn resolve_tui_cwd_leaves_an_already_absolute_cwd_alone() {
        let dir = tempfile::tempdir().unwrap();
        let resolved = resolve_tui_cwd(Some(dir.path().to_path_buf()));
        assert_eq!(resolved, dir.path().canonicalize().unwrap());
    }

    #[test]
    fn resolve_tui_cwd_falls_back_to_current_dir_when_no_flag_given() {
        let resolved = resolve_tui_cwd(None);
        assert_eq!(resolved, std::env::current_dir().unwrap());
    }

    #[test]
    fn resolve_tui_cwd_falls_back_to_the_given_path_if_canonicalize_fails() {
        // A typo'd/not-yet-created --cwd must not panic or silently vanish
        // here — it surfaces its own clear error later (e.g. spawn failure).
        let bogus = PathBuf::from("/definitely/does/not/exist/kim-cwd-test");
        let resolved = resolve_tui_cwd(Some(bogus.clone()));
        assert_eq!(resolved, bogus);
    }
}
