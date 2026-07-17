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
use env::build_child_env;
use proxy::{spawn_standalone_proxy, ProxyProcess};
use routing::{route_for_provider, TuiRoute};

/// `kim tui` falls back to this provider when neither `--provider` nor
/// `config.tui_provider` is set.
const DEFAULT_TUI_PROVIDER: &str = "browser:claude";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TuiOptions {
    pub(crate) provider: String,
    pub(crate) model: String,
    pub(crate) cwd: PathBuf,
    pub(crate) verbose: bool,
    pub(crate) passthrough: Vec<String>,
}

/// Parsed `--provider`/`--model`/`--cwd`/`--verbose`/`-- <passthrough>` flags,
/// before defaults (config / cwd) are applied. Shared verbatim by `kim tui`
/// and the standalone `kimcli` binary (see module docs).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(crate) struct TuiFlags {
    pub(crate) provider: Option<String>,
    pub(crate) model: Option<String>,
    pub(crate) cwd: Option<PathBuf>,
    pub(crate) verbose: bool,
    pub(crate) passthrough: Vec<String>,
}

/// Parse `kim tui`/`kimcli` flags. Recognizes `--provider <name>`,
/// `--model <name>`, `--cwd <dir>`, `--verbose`, and a trailing
/// `-- <raw args...>` passthrough (everything after `--` is forwarded
/// verbatim to the kimcli binary, unparsed).
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
                let value = args.get(i + 1).ok_or_else(|| {
                    "kim tui: --provider requires a value. Usage: kim tui [--provider <name>] \
                     [--model <name>] [--cwd <dir>] [--verbose] [-- <args...>]"
                        .to_string()
                })?;
                out.provider = Some(value.clone());
                i += 2;
            }
            "--model" => {
                let value = args.get(i + 1).ok_or_else(|| {
                    "kim tui: --model requires a value. Usage: kim tui [--provider <name>] \
                     [--model <name>] [--cwd <dir>] [--verbose] [-- <args...>]"
                        .to_string()
                })?;
                out.model = Some(value.clone());
                i += 2;
            }
            "--cwd" => {
                let value = args.get(i + 1).ok_or_else(|| {
                    "kim tui: --cwd requires a value. Usage: kim tui [--provider <name>] \
                     [--model <name>] [--cwd <dir>] [--verbose] [-- <args...>]"
                        .to_string()
                })?;
                out.cwd = Some(PathBuf::from(value));
                i += 2;
            }
            "--verbose" => {
                out.verbose = true;
                i += 1;
            }
            other => {
                return Err(format!(
                    "kim tui: unknown option '{other}'. \
                     Usage: kim tui [--provider <name>] [--model <name>] [--cwd <dir>] \
                     [--verbose] [-- <args...>]"
                ));
            }
        }
    }
    Ok(out)
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
    let provider = flags.provider.unwrap_or_else(|| {
        config
            .tui_provider
            .clone()
            .unwrap_or_else(|| DEFAULT_TUI_PROVIDER.to_string())
    });
    let model = flags
        .model
        .unwrap_or_else(|| config.tui_model.clone().unwrap_or(config.model));
    let cwd = flags
        .cwd
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));

    let opts = TuiOptions {
        provider,
        model,
        cwd,
        verbose: flags.verbose,
        passthrough: flags.passthrough,
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
    let route = route_for_provider(&opts.provider);

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
    let (extra_env, argv): (Vec<(String, String)>, Vec<String>) =
        match route {
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
                let spawned =
                    match spawn_standalone_proxy(&python, &kim_root, &opts.provider, opts.verbose)
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
        for argv in [vec!["--provider"], vec!["--model"], vec!["--cwd"]] {
            let err = parse_tui_flags(&strs(&argv)).unwrap_err();
            assert!(err.contains("requires a value"), "got: {err}");
        }
    }

    #[test]
    fn parse_tui_flags_unknown_option_is_an_error() {
        let err = parse_tui_flags(&strs(&["--nope"])).unwrap_err();
        assert!(err.contains("--nope"), "got: {err}");
        assert!(err.contains("Usage: kim tui"), "got: {err}");
    }
}
