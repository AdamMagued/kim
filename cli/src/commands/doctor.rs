use tokio::process::Command;

use crate::config::{config_path, KimConfig};
use crate::provider::{is_browser_provider, provider_info};

use super::providers::{known_ollama_cloud_models, model_options, ollama_models_at};
use super::CommandOutcome;

/// F-E-1: the outcome of a `kim doctor` run — the human-readable text plus two
/// health bits so `kim doctor` (and CI / install scripts calling it) can gate
/// on the exit code instead of always seeing 0.
///
/// - `required_ok` covers the universal prerequisites Kim cannot run without
///   (a Python interpreter for the orchestrator/agent; a home directory for
///   config + sessions). A required failure ALWAYS exits non-zero.
/// - `all_ok` additionally covers optional, provider-specific checks (Ollama
///   server/model, desktop bridge, API key, model-in-list). These gate the
///   exit code only under `--strict`.
pub struct DoctorReport {
    pub text: String,
    pub required_ok: bool,
    pub all_ok: bool,
}

/// F-E-1: should `kim doctor` exit non-zero? Required failures always gate;
/// optional/provider-specific failures gate only under `--strict`. Pure so it
/// is unit-testable without probing anything.
#[must_use]
pub fn doctor_should_fail(required_ok: bool, all_ok: bool, strict: bool) -> bool {
    !required_ok || (strict && !all_ok)
}

pub(super) async fn doctor(config: &KimConfig) -> CommandOutcome {
    // The REPL `/doctor` command only surfaces the text; the process exit-code
    // gating lives in main()'s `kim doctor` arm, which calls doctor_report.
    CommandOutcome::Message(doctor_report(config).await.text)
}

pub async fn doctor_report(config: &KimConfig) -> DoctorReport {
    let root =
        crate::sessions::find_kim_repo_root().unwrap_or_else(|| std::path::PathBuf::from("."));
    // Required: the interpreter chat/code mode actually run (find_python: repo
    // venv first, then python3/python). This is the check CI must gate on.
    let python_found = crate::agentic::find_python(&root).is_some();
    let config_ok = config_path().is_some();

    let mut lines = vec![
        "KimCLI doctor".to_string(),
        "Mode support: chat + code".to_string(),
        format!("Provider: {}", config.provider),
        format!("Model: {}", config.model),
        format!(
            "Config: {}",
            config_path().map_or_else(
                || "(no home directory)".to_string(),
                |path| path.display().to_string()
            )
        ),
        format!("Source root: {}", source_root_status()),
        format!("Bridge token: {}", crate::provider::bridge_token_source()),
        // F12: report the interpreter the CLI actually runs (find_python:
        // repo venv first, then system), not a bare PATH probe of `python3` —
        // the two could disagree in both directions.
        format!("python: {}", python_status().await),
        format!("codex: {}", command_status("codex", &["--version"]).await),
        format!("git: {}", command_status("git", &["--version"]).await),
        format!("cargo: {}", command_status("cargo", &["--version"]).await),
    ];

    // required_ok gates the exit code unconditionally; all_ok also folds in the
    // optional provider-specific checks below (only consulted under --strict).
    let mut required_ok = python_found && config_ok;
    let mut all_ok = required_ok;

    if config.provider == "ollama" {
        let base = crate::provider::normalize_base_url(&config.ollama_base_url);
        let server = ollama_models_at(&base).await;
        lines.push(format!(
            "Ollama server: {}",
            http_status(&base, "/api/tags").await
        ));
        // A8: doctor must verify the configured model is actually servable —
        // it previously reported "ok" while the selected model was missing.
        lines.push(format!(
            "Ollama model: {}",
            ollama_model_status(&base, &config.model).await
        ));
        let ollama_ok = known_ollama_cloud_models().contains(&config.model.as_str())
            || matches!(&server, Some(models) if models.iter().any(|m| m == &config.model));
        if !ollama_ok {
            all_ok = false;
        }
    }
    if config.provider == "desktop" || is_browser_provider(&config.provider) {
        let status = desktop_bridge_status(&config.desktop_bridge_url).await;
        // The bridge is optional (browser code mode can run via local
        // Playwright), so it only affects --strict.
        if !status.starts_with("ok") {
            all_ok = false;
        }
        lines.push(format!("Kim desktop bridge: {status}"));
    }
    if is_browser_provider(&config.provider) {
        lines.push(
            "Browser provider: keyless; chat needs Kim desktop open, code mode uses Kim's Codex browser bridge."
                .to_string(),
        );
    }
    if let Some(p) = provider_info(&config.provider).filter(|p| p.key_env.is_some()) {
        let key_env = p.key_env.unwrap();
        let env_val = std::env::var(key_env).ok();
        let stored = config.api_keys.get(&config.provider).map(String::as_str);
        let key_present = env_val
            .as_deref()
            .map(str::trim)
            .is_some_and(|v| !v.is_empty())
            || stored.map(str::trim).is_some_and(|v| !v.is_empty());
        lines.push(format!(
            "API key: {}",
            api_key_status(key_env, env_val, stored, &config.provider)
        ));
        if !key_present {
            all_ok = false;
        }
        // A8: note whether the configured model appears in the known list.
        let opts = model_options(config).await;
        if !opts.is_empty() {
            let model_known = opts.iter().any(|m| m == &config.model);
            lines.push(format!(
                "Model '{}': {}",
                config.model,
                if model_known {
                    "in the known model list".to_string()
                } else {
                    format!("not in the known list (known: {})", opts.join(", "))
                }
            ));
            if !model_known {
                all_ok = false;
            }
        }
    }

    if !python_found {
        // Belt-and-braces: python_status() already prints "not found", but make
        // the gating reason explicit at the end of the report.
        lines.push(
            "FAIL: no Python interpreter found — install Python 3.11+ (Kim's orchestrator runtime)."
                .to_string(),
        );
        required_ok = false;
        all_ok = false;
    }

    DoctorReport {
        text: lines.join("\n"),
        required_ok,
        all_ok,
    }
}

/// Doctor line: is `model` actually servable by this ollama endpoint? (A8)
async fn ollama_model_status(base: &str, model: &str) -> String {
    if known_ollama_cloud_models().contains(&model) {
        return format!("'{model}' is a known Ollama cloud model");
    }
    match ollama_models_at(base).await {
        Some(models) if models.iter().any(|m| m == model) => {
            format!("'{model}' is installed")
        }
        Some(models) => format!(
            "⚠ '{model}' is not installed and not a known cloud model — pull it or pick another (installed: {})",
            if models.is_empty() { "none".to_string() } else { models.join(", ") }
        ),
        None => format!("⚠ could not reach /api/tags to verify '{model}'"),
    }
}

/// Returns a human-readable API-key status for /doctor.  Precedence mirrors
/// resolve_api_key in provider.rs: a non-empty, non-whitespace env var wins
/// over a saved config key; a blank env var or blank stored key is treated as
/// absent.  env_val and stored are passed by the caller so this function is
/// pure and testable without touching the process environment.
pub(super) fn api_key_status(
    key_env: &str,
    env_val: Option<String>,
    stored: Option<&str>,
    provider_name: &str,
) -> String {
    let env_present = env_val
        .as_deref()
        .map(str::trim)
        .is_some_and(|v| !v.is_empty());
    let stored_present = stored.map(str::trim).is_some_and(|v| !v.is_empty());

    match (env_present, stored_present) {
        (true, true) => format!("set via {key_env} env var (overrides saved key)"),
        (true, false) => format!("set via {key_env} env var"),
        (false, true) => "saved in Kim config".to_string(),
        (false, false) => format!("missing; run /login {provider_name}"),
    }
}

fn source_root_status() -> String {
    format_source_root(crate::sessions::find_kim_repo_root().as_deref())
}

/// Pure helper — formats the source-root status line for `kim doctor`.
/// Accepts the resolved root (or None) so it can be tested without the filesystem.
pub(super) fn format_source_root(found: Option<&std::path::Path>) -> String {
    match found {
        Some(path) => format!("ok ({})", path.display()),
        None => {
            let marker = dirs::home_dir()
                .map(|h| h.join(".kim_root").display().to_string())
                .unwrap_or_else(|| "~/.kim_root".to_string());
            format!(
                "not set — required for browser-backed code mode\n  \
                 Fix: run cli/install.sh (writes {marker}), set KIM_PROJECT_ROOT, \
                 or run kim from inside the Kim repo"
            )
        }
    }
}

/// F12: doctor's python line must reflect what chat/code mode actually run —
/// `find_python` (repo venv first, then python3/python) — not a PATH probe.
async fn python_status() -> String {
    let root =
        crate::sessions::find_kim_repo_root().unwrap_or_else(|| std::path::PathBuf::from("."));
    match crate::agentic::find_python(&root) {
        Some(python) => {
            let shown = python.display().to_string();
            format!(
                "{} — {}",
                shown,
                command_status(&shown, &["--version"]).await
            )
        }
        None => "not found (tried repo venv, python3, python)".to_string(),
    }
}

async fn command_status(program: &str, args: &[&str]) -> String {
    match Command::new(program).args(args).output().await {
        Ok(output) if output.status.success() => {
            let first_line = String::from_utf8_lossy(if output.stdout.is_empty() {
                &output.stderr
            } else {
                &output.stdout
            })
            .lines()
            .next()
            .unwrap_or("ok")
            .trim()
            .to_string();
            if first_line.is_empty() {
                "ok".to_string()
            } else {
                first_line
            }
        }
        Ok(output) => format!("found but exited {}", output.status),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => "not found".to_string(),
        Err(error) => format!("error: {error}"),
    }
}

async fn desktop_bridge_status(base_url: &str) -> String {
    let base = base_url.trim_end_matches('/');
    for path in ["/health", "/v1/health"] {
        let status = http_status(base, path).await;
        if status.starts_with("ok") {
            return status;
        }
    }
    format!("offline ({base})")
}

async fn http_status(base_url: &str, path: &str) -> String {
    let url = format!("{}{}", base_url.trim_end_matches('/'), path);
    match reqwest::Client::new()
        .get(&url)
        .timeout(std::time::Duration::from_millis(800))
        .send()
        .await
    {
        Ok(resp) if resp.status().is_success() => format!("ok ({url})"),
        Ok(resp) => format!("HTTP {} ({url})", resp.status()),
        Err(_) => format!("offline ({url})"),
    }
}

#[cfg(test)]
mod tests {
    use crate::config::KimConfig;

    // ── source_root formatting ────────────────────────────────────────────────

    #[test]
    fn format_source_root_ok_when_path_supplied() {
        let p = std::path::Path::new("/some/kim");
        let s = super::format_source_root(Some(p));
        assert!(s.starts_with("ok ("), "expected 'ok (' prefix, got: {s}");
        assert!(s.contains("/some/kim"), "expected path in output, got: {s}");
    }

    #[test]
    fn format_source_root_not_set_mentions_fix_options() {
        let s = super::format_source_root(None);
        assert!(
            s.contains("not set"),
            "expected 'not set' in output, got: {s}"
        );
        assert!(
            s.contains("browser-backed code mode"),
            "expected capability note, got: {s}"
        );
        assert!(
            s.contains("install.sh") || s.contains("KIM_PROJECT_ROOT"),
            "expected fix hint, got: {s}"
        );
    }

    // ── api_key_status ────────────────────────────────────────────────────────

    #[test]
    fn api_key_status_env_var_only_reports_env_name() {
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some("sk-real".to_string()),
            None,
            "claude",
        );
        assert!(s.contains("ANTHROPIC_API_KEY"), "{s}");
        assert!(!s.contains("missing"), "{s}");
    }

    #[test]
    fn api_key_status_both_present_mentions_override() {
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some("sk-env".to_string()),
            Some("sk-saved"),
            "claude",
        );
        assert!(s.contains("overrides"), "{s}");
    }

    #[test]
    fn api_key_status_stored_only_reports_config() {
        let s = super::api_key_status("ANTHROPIC_API_KEY", None, Some("sk-saved"), "claude");
        assert_eq!(s, "saved in Kim config");
    }

    #[test]
    fn api_key_status_blank_env_falls_through_to_stored() {
        // ANTHROPIC_API_KEY="" must not shadow a saved key (matches resolve_api_key).
        let s = super::api_key_status(
            "ANTHROPIC_API_KEY",
            Some(String::new()),
            Some("sk-saved"),
            "claude",
        );
        assert_eq!(s, "saved in Kim config");
    }

    #[test]
    fn api_key_status_whitespace_stored_key_treated_as_absent() {
        // A stored key of "   " is effectively blank — login_with_key may trim on write,
        // but a hand-edited config could contain whitespace.  Doctor must not say
        // "saved in Kim config" when the actual request would fail with "Run /login".
        let s = super::api_key_status("ANTHROPIC_API_KEY", None, Some("   "), "claude");
        assert!(s.contains("missing"), "{s}");
        assert!(s.contains("/login"), "{s}");
    }

    #[test]
    fn api_key_status_both_absent_names_the_provider() {
        let s = super::api_key_status("OPENAI_API_KEY", None, None, "openai");
        assert!(s.contains("missing"), "{s}");
        assert!(s.contains("openai"), "{s}");
    }

    // ── F-E-1: doctor exit-code gating ──────────────────────────────────────

    /// Required failures always gate the exit code; optional/provider-specific
    /// failures gate only under --strict.
    #[test]
    fn doctor_should_fail_gating_matrix() {
        use super::doctor_should_fail;
        // All green.
        assert!(!doctor_should_fail(true, true, false));
        assert!(!doctor_should_fail(true, true, true));
        // Optional failure: gated only by --strict.
        assert!(!doctor_should_fail(true, false, false));
        assert!(doctor_should_fail(true, false, true));
        // Required failure: always gates, regardless of --strict.
        assert!(doctor_should_fail(false, false, false));
        assert!(doctor_should_fail(false, false, true));
    }

    /// A provider-specific failure (Ollama server unreachable + configured model
    /// not installed / not a known cloud tag) must clear `all_ok` — so `kim
    /// doctor --strict` exits non-zero — while the required Python interpreter
    /// present on the test host keeps `required_ok` true (plain `kim doctor`
    /// stays 0). 127.0.0.1:1 refuses instantly, so the probe is deterministic.
    #[tokio::test]
    async fn doctor_report_flags_unreachable_ollama_without_gating_required() {
        let config = KimConfig {
            provider: "ollama".to_string(),
            model: "definitely-not-a-real-model-xyz".to_string(),
            ollama_base_url: "http://127.0.0.1:1".to_string(),
            ..KimConfig::default()
        };
        let report = super::doctor_report(&config).await;
        assert!(
            !report.all_ok,
            "unreachable Ollama + unknown model must clear all_ok; text:\n{}",
            report.text
        );
        assert!(
            report.text.contains("Ollama"),
            "the human-readable report must still surface the Ollama status"
        );
    }
}
