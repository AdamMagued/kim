//! The standalone `codex_engine.standalone_proxy` process: its one-line JSON
//! handshake contract, spawning, background stdout/stderr draining, and
//! graceful teardown. Split out of the former single-file
//! `commands/tui.rs` — pure relocation, no behavior changes.

use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncBufRead, AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

/// Deadline for the standalone proxy's one-line JSON handshake.
const PROXY_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(30);

/// Grace period between SIGTERM and SIGKILL when tearing down the proxy.
const PROXY_SHUTDOWN_GRACE: Duration = Duration::from_secs(2);

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
    pub(crate) fn has_exited(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(Some(_)))
    }

    /// SIGTERM, `PROXY_SHUTDOWN_GRACE`, then SIGKILL if still alive (Unix).
    /// Windows has no real SIGTERM delivery (mirrors
    /// `codex_engine/standalone_proxy.py`'s own documented limitation), so it
    /// hard-kills immediately. Always joins the child so it's reaped rather
    /// than left a zombie/orphan.
    pub(crate) async fn shutdown(mut self) {
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
///
/// `extra_env` is set on the child IN ADDITION to this launcher's own
/// inherited environment (this `Command` has no `env_clear()`, unlike the
/// kimcli child's — see `env::build_child_env`'s allowlist docs) — used by
/// `ollama_cloud::resolve_ollama_cloud` to force `KIM_OLLAMA_MODE=cloud` /
/// `KIM_OLLAMA_CLOUD_MODEL=<model>` on the proxy so `OllamaProvider` actually
/// enters cloud mode for an Ollama cloud request.
pub(crate) async fn spawn_standalone_proxy(
    python: &Path,
    kim_root: &Path,
    provider: &str,
    verbose: bool,
    extra_env: &[(String, String)],
) -> Result<ProxyProcess, String> {
    let mut command = Command::new(python);
    command
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
        .kill_on_drop(true);
    for (key, value) in extra_env {
        command.env(key, value);
    }
    let mut child = command.spawn().map_err(|e| {
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
