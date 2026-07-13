//! Responses-API→Chat-Completions proxy that lets Codex talk to ollama, plus
//! the codex config file pointing at it.

use tokio::sync::mpsc::UnboundedSender;

use crate::config::KimConfig;

use super::{normalize_base_url, AppEvent};

// NOTE: include_str! is relative to THIS file (cli/src/provider/), while the
// proxy script lives beside provider.rs in cli/src/.
const RESPONSES_PROXY_PY: &str = include_str!("../responses_proxy.py");

/// Owns a running responses-proxy child plus its on-disk script (#35).
/// Dropping it kills the proxy (`kill_on_drop`) and removes the temp script —
/// so the caller MUST hold it for the lifetime of the codex run and let it drop
/// afterwards. The previous code detached `child.wait()` into a background task,
/// which leaked the proxy (it ran forever, port bound) after every `/code` turn.
pub(crate) struct ProxyHandle {
    pub(crate) port: u16,
    _child: tokio::process::Child,
    _script: tempfile::NamedTempFile,
}

pub(crate) async fn start_responses_proxy(
    config: &KimConfig,
    tx: &UnboundedSender<AppEvent>,
) -> Option<ProxyHandle> {
    use std::io::Write as _;
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::Command;

    // Randomized, exclusive temp file (O_CREAT|O_EXCL, 0600 on unix) instead of
    // a fixed `kim_responses_proxy.py` a local attacker could pre-create or
    // symlink (#35). Held by ProxyHandle so it is removed when the proxy dies.
    let mut script = match tempfile::Builder::new()
        .prefix("kim_responses_proxy_")
        .suffix(".py")
        .tempfile()
    {
        Ok(f) => f,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!("Failed to create proxy script: {e}")));
            return None;
        }
    };
    if let Err(e) = script
        .write_all(RESPONSES_PROXY_PY.as_bytes())
        .and_then(|()| script.flush())
    {
        let _ = tx.send(AppEvent::Err(format!("Failed to write proxy script: {e}")));
        return None;
    }
    let tmp_path = script.path().to_path_buf();

    // Resolve a Python interpreter the same way agentic.rs does: venv first,
    // then system python3/python — avoids hardcoding "python3" which is absent
    // on Windows.
    let python = {
        let root_opt = crate::sessions::find_kim_repo_root();
        let root = root_opt.unwrap_or_else(|| std::path::PathBuf::from("."));
        crate::agentic::find_python(&root).unwrap_or_else(|| std::path::PathBuf::from("python3"))
    };
    let ollama_base = format!("{}/v1", normalize_base_url(&config.ollama_base_url));
    let mut child = match Command::new(&python)
        .args([tmp_path.to_string_lossy().as_ref(), ollama_base.as_str()])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(AppEvent::Err(format!(
                "Failed to start responses proxy (Python interpreter not found): {e}"
            )));
            return None;
        }
    };

    // F11: `.take()?` returned None silently, ending the turn with no
    // diagnostic; every other failure path here sends an Err event.
    let stdout = match child.stdout.take() {
        Some(s) => s,
        None => {
            let _ = tx.send(AppEvent::Err(
                "Failed to capture responses proxy stdout.".to_string(),
            ));
            return None;
        }
    };
    let mut lines = BufReader::new(stdout).lines();
    let port_line =
        match tokio::time::timeout(std::time::Duration::from_secs(5), lines.next_line()).await {
            Ok(Ok(Some(line))) => line,
            _ => {
                let _ = tx.send(AppEvent::Err(
                    "Responses proxy did not start in time.".to_string(),
                ));
                return None;
            }
        };

    let port: u16 = match port_line.trim().parse() {
        Ok(p) => p,
        Err(_) => {
            let _ = tx.send(AppEvent::Err(format!(
                "Responses proxy returned unexpected output: {port_line}"
            )));
            return None;
        }
    };

    Some(ProxyHandle {
        port,
        _child: child,
        _script: script,
    })
}

// Retained (with its safety tests) for reference and potential reuse, but no
// longer wired into the live code path: the local codex run now routes through
// Kim's proxy via `-c` overrides on the user's real codex home (C3), instead of
// writing a config.toml into a throwaway CODEX_HOME. See
// `codex_stream::build_codex_args_with_proxy`.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn write_codex_config(
    proxy_port: u16,
    model: &str,
    codex_home: &std::path::Path,
) -> Result<(), String> {
    // F19: the model name is interpolated into TOML unescaped — a quote,
    // backslash, or newline (typo/paste) produced invalid TOML and a confusing
    // codex parse error. Reject up front with an actionable message.
    if model.chars().any(|c| matches!(c, '"' | '\\' | '\n' | '\r')) {
        return Err(format!(
            "model name {model:?} contains characters that are not valid here \
             (quotes, backslashes, or newlines). Fix it with /model."
        ));
    }
    std::fs::create_dir_all(codex_home)
        .map_err(|e| format!("Cannot create kim codex home {}: {e}", codex_home.display()))?;
    let config_path = codex_home.join("config.toml");
    let content = format!(
        "model = \"{model}\"\n\
         model_provider = \"kim-proxy\"\n\
         \n\
         [model_providers.kim-proxy]\n\
         name = \"Kim Proxy\"\n\
         base_url = \"http://127.0.0.1:{proxy_port}/v1\"\n\
         wire_api = \"responses\"\n\
         env_key = \"OPENAI_API_KEY\"\n"
    );
    std::fs::write(&config_path, content).map_err(|e| {
        format!(
            "Cannot write codex config to {}: {e}",
            config_path.display()
        )
    })
}

#[cfg(test)]
mod tests {
    use super::write_codex_config;

    // ── F19: model names that would break the generated TOML are rejected ───

    #[test]
    fn write_codex_config_rejects_toml_breaking_model_names() {
        let dir = tempfile::tempdir().expect("tempdir");
        for bad in ["lla\"ma", "back\\slash", "line\nbreak", "cr\rmodel"] {
            let err = write_codex_config(12345, bad, dir.path())
                .expect_err("TOML-breaking model name must be rejected");
            assert!(
                err.contains("model name"),
                "error should mention the model name, got: {err}"
            );
        }
    }

    #[test]
    fn write_codex_config_accepts_normal_model_names() {
        let dir = tempfile::tempdir().expect("tempdir");
        write_codex_config(12345, "qwen2.5-coder:7b", dir.path()).expect("valid name must pass");
        let content =
            std::fs::read_to_string(dir.path().join("config.toml")).expect("config written");
        assert!(content.contains("model = \"qwen2.5-coder:7b\""));
        assert!(content.contains("base_url = \"http://127.0.0.1:12345/v1\""));
    }
}
