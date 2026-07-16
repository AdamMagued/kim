//! `kim tui` launcher + the standalone `kimcli` binary's shared entry point.
//!
//! Launches the full Codex-style TUI (the `kimcli` binary — a rebranded fork
//! of `codex`, or the upstream `codex` binary as a fallback) as a child
//! process that inherits the terminal, routed at one of Kim's providers:
//!
//! - `ollama` → DIRECT route: kimcli talks straight to the local Ollama
//!   `/v1/responses` endpoint (no Python involved).
//! - everything else (`browser:*`, `claude`, `gemini`, `deepseek`, …) → PROXY
//!   route: spawn `python -m codex_engine.standalone_proxy`, read its one-line
//!   JSON handshake (`{"event":"ready","port":...,"token":"..."}` or
//!   `{"event":"fatal","message":"..."}`), then point kimcli at that proxy.
//!
//! Both routes wire up `mcp_servers.kim` so kimcli gets Kim's MCP tools
//! (scoped to the `ui,browser` tiers) alongside the model connection.
//!
//! `kim tui` (via the `kim` binary) and the standalone `kimcli` binary are the
//! exact same entry point (`run_tui_standalone`) — see `cli/Cargo.toml`'s two
//! `[[bin]]` targets and `src/bin/kimcli.rs`.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufRead, AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

/// `kim tui` falls back to this provider when neither `--provider` nor
/// `config.tui_provider` is set.
const DEFAULT_TUI_PROVIDER: &str = "browser:claude";

/// KIM_ENABLED_TOOL_TIERS value passed to the spawned kimcli's MCP `kim`
/// server. `ui` expands to `screen,mouse,keyboard,windows` and `browser`
/// expands to `web` (see `mcp_server/tool_tiers.py::TIER_ALIASES`). `shell`,
/// `file_write`, and `git` are intentionally excluded — `kim tui` grants
/// Kim's OS/browser-control tools, not arbitrary shell/file/git access.
const KIM_TUI_ENABLED_TOOL_TIERS: &str = "ui,browser";

/// Deadline for the standalone proxy's one-line JSON handshake.
const PROXY_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);

/// Grace period between SIGTERM and SIGKILL when tearing down the proxy.
const PROXY_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);

/* ===========================================================
Options + flag parsing
=========================================================== */

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

/* ===========================================================
Routing
=========================================================== */

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

/* ===========================================================
kimcli/codex binary resolution
=========================================================== */

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum KimcliResolution {
    /// The real `kimcli` binary was found.
    Kimcli(PathBuf),
    /// No `kimcli` binary anywhere; falling back to the upstream `codex`
    /// binary on PATH. Callers should warn — the branding (and possibly some
    /// behavior) differs from a proper kimcli install.
    CodexFallback(PathBuf),
}

/// Resolution order: `$CODEX_BIN` env var → `~/.kim/bin/kimcli` (via
/// `config::kim_home()`) → `kimcli` on PATH → `codex` on PATH (fallback,
/// upstream branding) → an actionable error mentioning
/// `scripts/install_kimcli.sh`.
///
/// Dependency-injected (`kim_home_dir`, `which_fn`) so binary resolution is
/// unit-testable without touching the real filesystem or PATH.
pub(crate) fn resolve_kimcli_binary_with<W>(
    codex_bin_env: Option<String>,
    kim_home_dir: Option<PathBuf>,
    which_fn: W,
) -> Result<KimcliResolution, String>
where
    W: Fn(&str) -> Option<PathBuf>,
{
    if let Some(explicit) = codex_bin_env.filter(|v| !v.trim().is_empty()) {
        return Ok(KimcliResolution::Kimcli(PathBuf::from(explicit)));
    }
    if let Some(home) = kim_home_dir {
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let candidate = home.join(".kim").join("bin").join(bin_name);
        if candidate.is_file() {
            return Ok(KimcliResolution::Kimcli(candidate));
        }
    }
    if let Some(path) = which_fn("kimcli") {
        return Ok(KimcliResolution::Kimcli(path));
    }
    if let Some(path) = which_fn("codex") {
        return Ok(KimcliResolution::CodexFallback(path));
    }
    Err(
        "kimcli binary not found (checked $CODEX_BIN, ~/.kim/bin/kimcli, and PATH — including \
         the upstream 'codex' binary). Install it by running scripts/install_kimcli.sh from the \
         Kim repo, or set CODEX_BIN to an explicit path."
            .to_string(),
    )
}

pub(crate) fn resolve_kimcli_binary() -> Result<KimcliResolution, String> {
    resolve_kimcli_binary_with(
        std::env::var("CODEX_BIN").ok(),
        crate::config::kim_home(),
        crate::agentic::which,
    )
}

/* ===========================================================
TOML `-c` override argv construction
=========================================================== */

/// Escape a string for embedding as a TOML basic string (the same quoting
/// `-c key="value"` codex/kimcli's config-override parser expects). Unlike
/// `codex_stream::sanitize_proxy_model` (which strips characters that would
/// break the generated TOML), this ESCAPES them — required here because the
/// values passed through this path include filesystem paths (which must
/// round-trip byte-for-byte, e.g. a `cwd` with a backslash on Windows or a
/// literal quote) rather than a cosmetic model name that can tolerate lossy
/// sanitization.
fn toml_quote(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
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
/// `/v1/responses` endpoint (`provider == "ollama"`). Ollama gained an
/// OpenAI-Responses-compatible endpoint in codex's built-in provider
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

/* ===========================================================
Child env allowlist
=========================================================== */

/// Build the kimcli child's environment from an explicit `(key, value)`
/// iterator (the real parent env, or a fixture in tests). Mirrors
/// `orchestrator/codex_bridge_service.py`'s `_run_exec_task` minimal env
/// (lines ~772-805): PATH, HOME (falling back to USERPROFILE on Windows),
/// USER (falling back to USERNAME), TMPDIR (falling back to TEMP/TMP), LANG,
/// every `LC_*` locale var, TERM, COLORTERM, TERMINFO. Deliberately does NOT
/// forward CODEX_HOME or OPENAI_BASE_URL — those are set explicitly by the
/// caller per-route (or left unset so kimcli/codex uses its own home).
pub(crate) fn build_child_env_from<I>(parent_env: I) -> Vec<(String, String)>
where
    I: IntoIterator<Item = (String, String)>,
{
    let mut map: std::collections::HashMap<String, String> = parent_env.into_iter().collect();
    let mut out = Vec::new();

    if let Some(home) = map.remove("HOME").or_else(|| map.remove("USERPROFILE")) {
        out.push(("HOME".to_string(), home));
    }
    if let Some(user) = map.remove("USER").or_else(|| map.remove("USERNAME")) {
        out.push(("USER".to_string(), user));
    }
    if let Some(tmp) = map
        .remove("TMPDIR")
        .or_else(|| map.remove("TEMP"))
        .or_else(|| map.remove("TMP"))
    {
        out.push(("TMPDIR".to_string(), tmp));
    }
    for key in ["PATH", "LANG", "TERM", "COLORTERM", "TERMINFO"] {
        if let Some(value) = map.remove(key) {
            out.push((key.to_string(), value));
        }
    }
    let mut lc_keys: Vec<String> = map
        .keys()
        .filter(|k| k.starts_with("LC_"))
        .cloned()
        .collect();
    lc_keys.sort();
    for key in lc_keys {
        if let Some(value) = map.remove(&key) {
            out.push((key, value));
        }
    }
    out
}

pub(crate) fn build_child_env() -> Vec<(String, String)> {
    build_child_env_from(std::env::vars())
}

/* ===========================================================
Proxy handshake
=========================================================== */

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ProxyHandshake {
    Ready { port: u16, token: String },
    Fatal { message: String },
}

/// Parse one line of the standalone proxy's stdout handshake contract (see
/// `codex_engine/standalone_proxy.py`'s module docstring):
/// `{"event":"ready","port":<int>,"token":"<bearer>"}` or
/// `{"event":"fatal","message":"<...>"}`.
pub(crate) fn parse_handshake_line(line: &str) -> Result<ProxyHandshake, String> {
    let trimmed = line.trim();
    let value: serde_json::Value = serde_json::from_str(trimmed)
        .map_err(|e| format!("proxy handshake was not valid JSON ({e}): {trimmed:?}"))?;
    match value.get("event").and_then(serde_json::Value::as_str) {
        Some("ready") => {
            let port = value
                .get("port")
                .and_then(serde_json::Value::as_u64)
                .and_then(|p| u16::try_from(p).ok())
                .ok_or_else(|| {
                    format!("proxy ready handshake missing/invalid 'port': {trimmed:?}")
                })?;
            let token = value
                .get("token")
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| format!("proxy ready handshake missing 'token': {trimmed:?}"))?
                .to_string();
            Ok(ProxyHandshake::Ready { port, token })
        }
        Some("fatal") => {
            let message = value
                .get("message")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("(no message)")
                .to_string();
            Ok(ProxyHandshake::Fatal { message })
        }
        other => Err(format!(
            "unexpected proxy handshake event {other:?}: {trimmed:?}"
        )),
    }
}

/// Read exactly one handshake line from `reader` within `timeout`. Generic
/// over `AsyncBufRead` so it is unit-testable against an in-memory buffer
/// instead of a real child's stdout.
pub(crate) async fn read_proxy_handshake_with_timeout<R>(
    reader: &mut R,
    timeout: Duration,
) -> Result<ProxyHandshake, String>
where
    R: AsyncBufRead + Unpin,
{
    let mut line = String::new();
    match tokio::time::timeout(timeout, reader.read_line(&mut line)).await {
        Ok(Ok(0)) => Err("proxy exited before sending a handshake line".to_string()),
        Ok(Ok(_)) => parse_handshake_line(&line),
        Ok(Err(e)) => Err(format!("error reading proxy handshake: {e}")),
        Err(_) => Err(format!(
            "timed out after {}s waiting for the proxy handshake",
            timeout.as_secs()
        )),
    }
}

pub(crate) async fn read_proxy_handshake<R>(reader: &mut R) -> Result<ProxyHandshake, String>
where
    R: AsyncBufRead + Unpin,
{
    read_proxy_handshake_with_timeout(reader, PROXY_HANDSHAKE_TIMEOUT).await
}

/* ===========================================================
Standalone proxy process
=========================================================== */

/// Owns the running `codex_engine.standalone_proxy` child plus its background
/// stdout/stderr drain tasks. Must be kept alive for the whole kimcli run and
/// torn down afterward via [`ProxyProcess::shutdown`].
pub(crate) struct ProxyProcess {
    pub(crate) port: u16,
    pub(crate) token: String,
    child: Child,
    stdout_drain: tokio::task::JoinHandle<()>,
    stderr_drain: tokio::task::JoinHandle<()>,
}

impl ProxyProcess {
    /// Non-blocking: has the proxy already exited on its own? Used to decide
    /// whether to print a "proxy died early" note after kimcli exits.
    fn has_exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)))
    }

    /// SIGTERM, `PROXY_SHUTDOWN_GRACE`, then SIGKILL if still alive (Unix).
    /// Windows has no real SIGTERM delivery (mirrors
    /// `codex_engine/standalone_proxy.py`'s own documented limitation), so it
    /// hard-kills immediately. Always joins the child so it's reaped rather
    /// than left a zombie/orphan.
    async fn shutdown(mut self) {
        self.stdout_drain.abort();
        self.stderr_drain.abort();
        if matches!(self.child.try_wait(), Ok(Some(_))) {
            return; // already exited — nothing to signal or wait for
        }

        #[cfg(unix)]
        {
            if let Some(pid) = self.child.id() {
                // SAFETY: `pid` is this child's own pid (u32, non-negative),
                // and SIGTERM is a plain signal-send with no preconditions.
                unsafe {
                    libc::kill(pid as libc::pid_t, libc::SIGTERM);
                }
            }
        }
        #[cfg(not(unix))]
        {
            let _ = self.child.start_kill();
        }

        if tokio::time::timeout(PROXY_SHUTDOWN_GRACE, self.child.wait())
            .await
            .is_err()
        {
            let _ = self.child.start_kill();
            let _ = self.child.wait().await;
        }
    }
}

/// Drain a stdout/stderr stream in the background so its OS pipe never fills
/// (which would block the proxy's `write()` and hang the whole run). Under
/// `--verbose` each line is forwarded to the launcher's stderr; otherwise it's
/// discarded.
fn spawn_drain_task<R>(
    mut reader: R,
    verbose: bool,
    label: &'static str,
) -> tokio::task::JoinHandle<()>
where
    R: AsyncBufRead + Unpin + Send + 'static,
{
    tokio::spawn(async move {
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line).await {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    if verbose {
                        eprint!("[{label}] {line}");
                    }
                }
            }
        }
    })
}

/// Spawn `python -m codex_engine.standalone_proxy --provider <provider>
/// --parent-pid <this process's pid>` with cwd = the Kim repo root, complete
/// its one-line handshake, then start draining its stdout/stderr in the
/// background so the pipes never fill.
async fn spawn_standalone_proxy(
    python: &Path,
    kim_root: &Path,
    provider: &str,
    verbose: bool,
) -> Result<ProxyProcess, String> {
    let mut child = Command::new(python)
        .args([
            "-m",
            "codex_engine.standalone_proxy",
            "--provider",
            provider,
            "--parent-pid",
            &std::process::id().to_string(),
        ])
        .current_dir(kim_root)
        .env("PYTHONPATH", kim_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| {
            format!(
                "failed to start the codex proxy ({}): {e}",
                python.display()
            )
        })?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "failed to capture the codex proxy's stdout".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "failed to capture the codex proxy's stderr".to_string())?;

    let mut stdout_reader = BufReader::new(stdout);
    let handshake = read_proxy_handshake(&mut stdout_reader).await;

    let (port, token) = match handshake {
        Ok(ProxyHandshake::Ready { port, token }) => (port, token),
        Ok(ProxyHandshake::Fatal { message }) => {
            let _ = child.start_kill();
            return Err(format!("the codex proxy reported a fatal error: {message}"));
        }
        Err(e) => {
            let _ = child.start_kill();
            return Err(format!("codex proxy handshake failed: {e}"));
        }
    };

    let stdout_drain = spawn_drain_task(stdout_reader, verbose, "proxy");
    let stderr_drain = spawn_drain_task(BufReader::new(stderr), verbose, "proxy");

    Ok(ProxyProcess {
        port,
        token,
        child,
        stdout_drain,
        stderr_drain,
    })
}

/* ===========================================================
Entry points
=========================================================== */

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
/// the route (spawn+handshake the proxy, or go direct at Ollama), spawn
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
        Ok(KimcliResolution::Kimcli(path)) => path,
        Ok(KimcliResolution::CodexFallback(path)) => {
            eprintln!(
                "kim tui: no kimcli binary found — falling back to the upstream 'codex' binary \
                 ({}). Branding (and possibly some behavior) will differ; install kimcli via \
                 scripts/install_kimcli.sh.",
                path.display()
            );
            path
        }
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

    // ── parse_tui_flags ──────────────────────────────────────────────────────

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

    // ── routing table ────────────────────────────────────────────────────────

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

    // ── binary resolution order ──────────────────────────────────────────────

    #[test]
    fn resolve_kimcli_binary_prefers_codex_bin_env_over_everything() {
        let resolved = resolve_kimcli_binary_with(
            Some("/explicit/codex-bin".to_string()),
            Some(PathBuf::from("/home/whoever")),
            |_| Some(PathBuf::from("/usr/bin/kimcli")),
        )
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::Kimcli(PathBuf::from("/explicit/codex-bin"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_blank_codex_bin_env_is_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"stub").unwrap();

        let resolved = resolve_kimcli_binary_with(
            Some("   ".to_string()),
            Some(dir.path().to_path_buf()),
            |_| None,
        )
        .unwrap();
        assert_eq!(resolved, KimcliResolution::Kimcli(kimcli_path));
    }

    #[test]
    fn resolve_kimcli_binary_finds_kim_home_bin_kimcli() {
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = dir.path().join(".kim").join("bin");
        std::fs::create_dir_all(&bin_dir).unwrap();
        let bin_name = if cfg!(windows) {
            "kimcli.exe"
        } else {
            "kimcli"
        };
        let kimcli_path = bin_dir.join(bin_name);
        std::fs::write(&kimcli_path, b"stub").unwrap();

        let resolved =
            resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None).unwrap();
        assert_eq!(resolved, KimcliResolution::Kimcli(kimcli_path));
    }

    #[test]
    fn resolve_kimcli_binary_falls_back_to_path_kimcli() {
        let dir = tempfile::tempdir().unwrap(); // no .kim/bin/kimcli inside
        let resolved = resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |name| {
            (name == "kimcli").then(|| PathBuf::from("/usr/local/bin/kimcli"))
        })
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::Kimcli(PathBuf::from("/usr/local/bin/kimcli"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_falls_back_to_codex_with_warning_variant() {
        let dir = tempfile::tempdir().unwrap();
        let resolved = resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |name| {
            (name == "codex").then(|| PathBuf::from("/usr/local/bin/codex"))
        })
        .unwrap();
        assert_eq!(
            resolved,
            KimcliResolution::CodexFallback(PathBuf::from("/usr/local/bin/codex"))
        );
    }

    #[test]
    fn resolve_kimcli_binary_none_found_is_an_actionable_error() {
        let dir = tempfile::tempdir().unwrap();
        let err =
            resolve_kimcli_binary_with(None, Some(dir.path().to_path_buf()), |_| None).unwrap_err();
        assert!(err.contains("install_kimcli.sh"), "got: {err}");
        assert!(err.contains("CODEX_BIN"), "got: {err}");
    }

    // ── argv construction: both routes ───────────────────────────────────────

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
    fn argv_appends_passthrough_verbatim_at_the_end() {
        let passthrough = strs(&["--some-codex-flag", "value with spaces"]);
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

    // ── handshake parsing ────────────────────────────────────────────────────

    #[test]
    fn parse_handshake_line_ready() {
        let parsed =
            parse_handshake_line(r#"{"event":"ready","port":45999,"token":"tok-123"}"#).unwrap();
        assert_eq!(
            parsed,
            ProxyHandshake::Ready {
                port: 45999,
                token: "tok-123".to_string(),
            }
        );
    }

    #[test]
    fn parse_handshake_line_fatal() {
        let parsed = parse_handshake_line(r#"{"event":"fatal","message":"boom"}"#).unwrap();
        assert_eq!(
            parsed,
            ProxyHandshake::Fatal {
                message: "boom".to_string(),
            }
        );
    }

    #[test]
    fn parse_handshake_line_garbage_is_an_error() {
        assert!(parse_handshake_line("not json at all").is_err());
        assert!(parse_handshake_line(r#"{"event":"unknown"}"#).is_err());
        assert!(parse_handshake_line(r#"{"event":"ready","port":"not-a-number"}"#).is_err());
        assert!(parse_handshake_line(r#"{"event":"ready","port":45999}"#).is_err());
        // missing token
    }

    #[tokio::test]
    async fn read_proxy_handshake_ready_line() {
        let data = b"{\"event\":\"ready\",\"port\":9,\"token\":\"t\"}\n";
        let mut reader = tokio::io::BufReader::new(&data[..]);
        let parsed = read_proxy_handshake(&mut reader).await.unwrap();
        assert_eq!(
            parsed,
            ProxyHandshake::Ready {
                port: 9,
                token: "t".to_string(),
            }
        );
    }

    #[tokio::test]
    async fn read_proxy_handshake_fatal_line() {
        let data = b"{\"event\":\"fatal\",\"message\":\"nope\"}\n";
        let mut reader = tokio::io::BufReader::new(&data[..]);
        let parsed = read_proxy_handshake(&mut reader).await.unwrap();
        assert_eq!(
            parsed,
            ProxyHandshake::Fatal {
                message: "nope".to_string(),
            }
        );
    }

    #[tokio::test]
    async fn read_proxy_handshake_garbage_is_an_error() {
        let data = b"totally not json\n";
        let mut reader = tokio::io::BufReader::new(&data[..]);
        let err = read_proxy_handshake(&mut reader).await.unwrap_err();
        assert!(err.contains("not valid JSON"), "got: {err}");
    }

    #[tokio::test]
    async fn read_proxy_handshake_eof_before_any_line_is_an_error() {
        let data: &[u8] = b"";
        let mut reader = tokio::io::BufReader::new(data);
        let err = read_proxy_handshake(&mut reader).await.unwrap_err();
        assert!(err.contains("exited before"), "got: {err}");
    }

    #[tokio::test]
    async fn read_proxy_handshake_times_out_on_a_silent_reader() {
        // A duplex pipe whose write half is kept open but never written to:
        // the read half blocks forever, exercising the real timeout path
        // (rather than EOF, which `tokio::io::empty()` would give instantly).
        let (read_half, _write_half_kept_alive) = tokio::io::duplex(64);
        let mut reader = tokio::io::BufReader::new(read_half);
        let err = read_proxy_handshake_with_timeout(&mut reader, Duration::from_millis(50))
            .await
            .unwrap_err();
        assert!(err.contains("timed out"), "got: {err}");
    }

    // ── env allowlist ─────────────────────────────────────────────────────────

    fn env_pairs(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn build_child_env_keeps_the_allowlist_and_drops_everything_else() {
        let env = build_child_env_from(env_pairs(&[
            ("PATH", "/usr/bin"),
            ("HOME", "/home/kim"),
            ("USER", "kim"),
            ("TMPDIR", "/tmp"),
            ("LANG", "en_US.UTF-8"),
            ("TERM", "xterm-256color"),
            ("COLORTERM", "truecolor"),
            ("TERMINFO", "/usr/share/terminfo"),
            ("LC_ALL", "C"),
            ("LC_CTYPE", "en_US.UTF-8"),
            // Must be dropped:
            ("CODEX_HOME", "/should/not/leak"),
            ("OPENAI_BASE_URL", "http://should-not-leak"),
            ("SECRET_TOKEN", "leaked-if-present"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();

        assert_eq!(map.get("TERM").map(String::as_str), Some("xterm-256color"));
        assert_eq!(map.get("PATH").map(String::as_str), Some("/usr/bin"));
        assert_eq!(map.get("HOME").map(String::as_str), Some("/home/kim"));
        assert_eq!(map.get("LC_ALL").map(String::as_str), Some("C"));
        assert_eq!(map.get("LC_CTYPE").map(String::as_str), Some("en_US.UTF-8"));

        assert!(!map.contains_key("CODEX_HOME"), "CODEX_HOME must not leak");
        assert!(
            !map.contains_key("OPENAI_BASE_URL"),
            "OPENAI_BASE_URL must not leak"
        );
        assert!(
            !map.contains_key("SECRET_TOKEN"),
            "arbitrary parent env vars must not leak"
        );
    }

    #[test]
    fn build_child_env_collapses_windows_home_and_user_aliases() {
        let env = build_child_env_from(env_pairs(&[
            ("USERPROFILE", r"C:\Users\kim"),
            ("USERNAME", "kim"),
            ("TEMP", r"C:\Temp"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map.get("HOME").map(String::as_str), Some(r"C:\Users\kim"));
        assert_eq!(map.get("USER").map(String::as_str), Some("kim"));
        assert_eq!(map.get("TMPDIR").map(String::as_str), Some(r"C:\Temp"));
        // The Windows-native names themselves are not forwarded verbatim.
        assert!(!map.contains_key("USERPROFILE"));
        assert!(!map.contains_key("USERNAME"));
        assert!(!map.contains_key("TEMP"));
    }

    #[test]
    fn build_child_env_native_home_wins_over_userprofile() {
        let env = build_child_env_from(env_pairs(&[
            ("HOME", "/home/kim"),
            ("USERPROFILE", r"C:\Users\kim"),
        ]));
        let map: std::collections::HashMap<_, _> = env.into_iter().collect();
        assert_eq!(map.get("HOME").map(String::as_str), Some("/home/kim"));
    }
}
