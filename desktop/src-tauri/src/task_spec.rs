//! K2 — `TaskSpec` + `EnvBuilder`: the *pure* half of the task spawn path.
//!
//! A `TaskSpec` is a complete, inert description of one agent subprocess:
//! program, argv, cwd, env pairs, and stdio policy. It is produced by the
//! named builders below (`chat_task_spec`, `codex_browser_spec`,
//! `codex_direct_spec`) and consumed by `spawn_supervisor::spawn`.
//!
//! Nothing in this module touches Tauri, tokio, or the filesystem — every
//! builder is a pure function of its inputs, which is what makes the spawn
//! paths testable from `tests/task_spawn.rs` (T2). Async/IO concerns
//! (interpreter discovery, provider route resolution, OAuth env, Chrome
//! launch) are resolved by the caller *before* building the spec.
//!
//! Both the GUI (`subprocess.rs::send_task`) and the HTTP bridge
//! (`http_bridge.rs` `/v1/task`) build their specs here, which is what keeps
//! the two spawn paths from diverging again (A1/A3).

use std::path::{Path, PathBuf};

/// Source that spawned a task. Lives here (the pub seam) so integration
/// tests can name it; `task_runtime` re-exports it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpawnSource {
    /// Spawned by the Tauri GUI `send_task` command.
    Gui,
    /// Spawned by the HTTP bridge `/v1/task` endpoint (kimctl CLI).
    Bridge,
}

/// How the child's stdin should be wired.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StdinMode {
    /// Piped and retained in `TaskRuntime` for HITL approvals / steering.
    Piped,
    /// No stdin (direct Codex CLI runs never receive HITL lines).
    Null,
}

/// Complete, inert description of one agent subprocess.
#[derive(Debug, Clone)]
pub struct TaskSpec {
    pub program: String,
    pub args: Vec<String>,
    /// `None` = inherit the app's cwd (codex direct mode uses `-C` instead).
    pub cwd: Option<PathBuf>,
    pub envs: Vec<(String, String)>,
    pub stdin: StdinMode,
    pub session_id: String,
    pub source: SpawnSource,
    /// Codex-mode lines that fail typed decode are still forwarded raw.
    pub is_codex: bool,
}

// ---------------------------------------------------------------------------
// EnvBuilder — ordered env-pair assembly shared by every spec builder.
// ---------------------------------------------------------------------------

/// Accumulates `(key, value)` env pairs with conditional helpers so the spec
/// builders read as a declarative list instead of an if-forest.
#[derive(Debug, Default, Clone)]
pub struct EnvBuilder {
    envs: Vec<(String, String)>,
}

impl EnvBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(mut self, key: &str, value: impl Into<String>) -> Self {
        self.envs.push((key.to_string(), value.into()));
        self
    }

    /// Set only when the option holds a non-empty (trimmed) value.
    pub fn set_opt(mut self, key: &str, value: Option<&str>) -> Self {
        if let Some(v) = value.map(str::trim).filter(|v| !v.is_empty()) {
            self.envs.push((key.to_string(), v.to_string()));
        }
        self
    }

    pub fn extend(mut self, pairs: impl IntoIterator<Item = (String, String)>) -> Self {
        self.envs.extend(pairs);
        self
    }

    /// The invariant env every orchestrator (Python) spawn gets:
    /// import resolution, MCP project root, and Tauri-mode signalling.
    pub fn orchestrator_base(self, kim_root: &Path, project_root: &Path) -> Self {
        self.set("PYTHONPATH", kim_root.to_string_lossy())
            .set("PROJECT_ROOT", project_root.to_string_lossy())
            .set("KIM_TAURI_MODE", "1")
    }

    /// Map the GUI/CLI permission vocabulary onto `KIM_HITL_RISK_THRESHOLD`.
    /// Canonical: `full_auto` (no gate) / `ask_risky` (high) / `ask_always`
    /// (medium). Legacy aliases from the `/v1/task` docs are accepted on both
    /// paths so an integrator using either spelling gets the gate they asked
    /// for (#37). Unknown values default to full-auto.
    pub fn permission_mode(self, mode: Option<&str>) -> Self {
        match mode.unwrap_or("full_auto") {
            "ask_risky" | "confirm-sensitive" => self.set("KIM_HITL_RISK_THRESHOLD", "high"),
            "ask_always" | "confirm-all" => self.set("KIM_HITL_RISK_THRESHOLD", "medium"),
            _ => self,
        }
    }

    /// Webview-bridge credentials so BrowserProvider can drive the in-app
    /// sign-in window in headless mode.
    pub fn webview_bridge(self, cfg: Option<(&str, &str)>) -> Self {
        match cfg {
            Some((url, token)) => self
                .set("KIM_WEBVIEW_BRIDGE_URL", url)
                .set("KIM_WEBVIEW_BRIDGE_TOKEN", token)
                .set("KIM_WEBVIEW_WINDOW_LABEL", "kim-browser-signin"),
            None => self,
        }
    }

    /// Ollama routing overrides forwarded from the composer/settings.
    #[allow(clippy::too_many_arguments)]
    pub fn ollama(
        self,
        base_url: Option<&str>,
        mode: Option<&str>,
        local_model: Option<&str>,
        cloud_model: Option<&str>,
        context_limit_override: Option<u32>,
    ) -> Self {
        self.set_opt("KIM_OLLAMA_BASE_URL", base_url)
            .set_opt("KIM_OLLAMA_MODE", mode)
            .set_opt("KIM_OLLAMA_LOCAL_MODEL", local_model)
            .set_opt("KIM_OLLAMA_CLOUD_MODEL", cloud_model)
            .set_opt(
                "KIM_OLLAMA_CONTEXT_LIMIT_OVERRIDE",
                context_limit_override
                    .filter(|n| *n > 0)
                    .map(|n| n.to_string())
                    .as_deref(),
            )
    }

    pub fn build(self) -> Vec<(String, String)> {
        self.envs
    }
}

// ---------------------------------------------------------------------------
// Shared pure helpers.
// ---------------------------------------------------------------------------

/// Unique per-run id for file checkpoints (`~/.kim/checkpoints/<id>`), shared
/// verbatim by both spawn paths: sanitized session id + ms timestamp.
pub fn run_id_for_session(session_id: &str) -> String {
    let sess_safe: String = session_id
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect();
    format!(
        "{}-{}",
        sess_safe,
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0)
    )
}

/// Normalize the caller's provider choice.
///
/// In Code-tab (codex) mode, bare "gemini"/"chatgpt"/"deepseek-browser" have no
/// direct API-key path in Kim — promote them to `browser:<name>` so they route
/// through the browser bridge automatically.
pub fn promote_provider(raw: Option<String>, default: &str, is_codex: bool) -> String {
    let raw = raw
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| default.to_string());
    if !is_codex {
        return raw;
    }
    let lower = raw.trim().to_ascii_lowercase();
    match lower.as_str() {
        "gemini" | "chatgpt" | "deepseek-browser" if !raw.starts_with("browser:") => {
            format!("browser:{}", lower)
        }
        _ => raw,
    }
}

/// Whether a provider string routes through Kim's BrowserProvider.
pub fn is_browser_provider(provider: &str) -> bool {
    provider == "browser" || provider.starts_with("browser:")
}

/// Pre-resolved provider routing for direct Codex CLI runs: extra argv,
/// extra env, and a human-readable route label for the status line.
/// Produced by `configure_codex_direct_provider` (async, reads .env/config);
/// consumed by `codex_direct_spec` (pure).
#[derive(Debug, Clone, Default)]
pub struct ProviderRoute {
    pub args: Vec<String>,
    pub envs: Vec<(String, String)>,
    pub label: String,
}

// ---------------------------------------------------------------------------
// Named spec builders — one per spawn shape.
// ---------------------------------------------------------------------------

/// Parameters for a Kim orchestrator (Chat-tab / `/v1/task`) spawn.
pub struct ChatSpecParams<'a> {
    pub python: &'a str,
    /// True when `python` is the bundled `kim-orchestrator` sidecar (invoke
    /// directly instead of `python -m orchestrator.agent`).
    pub bundled_sidecar: bool,
    pub kim_root: &'a Path,
    /// PROJECT_ROOT for MCP tools (GUI: target project; bridge: kim root).
    pub project_root: &'a Path,
    pub session_dir: &'a Path,
    pub task: &'a str,
    pub provider: &'a str,
    pub session_id: String,
    pub resume: Option<String>,
    pub permission_mode: Option<&'a str>,
    pub source: SpawnSource,
    /// Extra env pairs the caller resolved (bridge cfg, restore status,
    /// OAuth pairs, ollama overrides, budget…), appended last.
    pub extra_envs: Vec<(String, String)>,
}

/// Build the `python -m orchestrator.agent` spec used by BOTH the GUI Chat
/// tab and the HTTP bridge `/v1/task` endpoint.
pub fn chat_task_spec(p: ChatSpecParams<'_>) -> TaskSpec {
    let mut args: Vec<String> = Vec::new();
    if !p.bundled_sidecar {
        args.extend(["-m".into(), "orchestrator.agent".into()]);
    }
    args.extend([
        "--task".into(),
        p.task.into(),
        "--session-dir".into(),
        p.session_dir.to_string_lossy().into_owned(),
    ]);
    if let Some(resume) = p.resume.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
        args.extend(["--resume".into(), resume.into()]);
    }
    args.extend(["--provider".into(), p.provider.into()]);

    let envs = EnvBuilder::new()
        .set("KIM_RUN_ID", run_id_for_session(&p.session_id))
        // RUN-IDENTITY: the session this run belongs to. Python's emit_event
        // stamps it onto every event so the desktop files output under THIS
        // session regardless of which view is mounted when the run completes.
        .set("KIM_SESSION_ID", p.session_id.as_str())
        .orchestrator_base(p.kim_root, p.project_root)
        .permission_mode(p.permission_mode)
        .extend(p.extra_envs)
        .build();

    TaskSpec {
        program: p.python.to_string(),
        args,
        cwd: Some(p.kim_root.to_path_buf()),
        envs,
        stdin: StdinMode::Piped,
        session_id: p.session_id,
        source: p.source,
        is_codex: false,
    }
}

/// Parameters for the Codex browser-bridge spawn
/// (`python -m orchestrator.codex_bridge_service`).
pub struct CodexBridgeSpecParams<'a> {
    pub python: &'a str,
    pub kim_root: &'a Path,
    pub target_root: &'a Path,
    pub task: &'a str,
    pub provider: &'a str,
    pub codex_bin: &'a Path,
    pub session_id: String,
    pub permission_mode: Option<&'a str>,
    pub extra_envs: Vec<(String, String)>,
}

/// Build the browser-bridge Codex spec: `codex_bridge_service` spawns Codex
/// with `CODEX_FILE_BRIDGE=1` and relays each LLM request through Kim's
/// BrowserProvider. Stdin stays piped for the pre-spawn HITL round-trip.
pub fn codex_browser_spec(p: CodexBridgeSpecParams<'_>) -> TaskSpec {
    let args = vec![
        "-m".into(),
        "orchestrator.codex_bridge_service".into(),
        "--task".into(),
        p.task.into(),
        "--cwd".into(),
        p.target_root.to_string_lossy().into_owned(),
        "--provider".into(),
        p.provider.into(),
    ];
    let envs = EnvBuilder::new()
        .set("PYTHONPATH", p.kim_root.to_string_lossy())
        .set("CODEX_BIN", p.codex_bin.to_string_lossy())
        .set("KIM_TAURI_MODE", "1")
        // F-H-8/F-H-2: export the run-identity envelope EXACTLY like
        // `chat_task_spec` so the codex browser-bridge's typed events
        // (kim:status / kim:answer / kim:tool / kim:hitl-approval-request,
        // all emitted by codex_bridge_service via events_gen) self-stamp
        // KIM_RUN_ID / KIM_SESSION_ID. Without this the Code-tab (browser
        // bridge) path emitted typed events with NO envelope, so the frontend
        // routed them to whatever view was mounted instead of their owning
        // session (root cause of F-F-2/F-F-8). Names match the Python side
        // landed in a61ffb4 (orchestrator/events_gen.py reads these two).
        .set("KIM_RUN_ID", run_id_for_session(&p.session_id))
        .set("KIM_SESSION_ID", p.session_id.as_str())
        .permission_mode(p.permission_mode)
        .extend(p.extra_envs)
        .build();

    TaskSpec {
        program: p.python.to_string(),
        args,
        cwd: Some(p.kim_root.to_path_buf()),
        envs,
        stdin: StdinMode::Piped,
        session_id: p.session_id,
        source: SpawnSource::Gui,
        is_codex: true,
    }
}

/// Parameters for a direct Codex CLI spawn (no Kim orchestrator).
pub struct CodexDirectSpecParams<'a> {
    pub code_bin: &'a Path,
    pub target_root: &'a Path,
    pub task: &'a str,
    /// `KIM_CODEX_BYPASS_SANDBOX=1` opt-in (#1) — resolved by the caller.
    pub bypass_sandbox: bool,
    pub route: ProviderRoute,
    pub session_id: String,
}

/// Build the direct-API Codex CLI spec (`codex exec --json …`).
///
/// F-G-1: the legacy Claw compatibility shape (`claw --output-format json
/// prompt …`) was removed along with the deleted `pythonExperimentTool/` tree.
pub fn codex_direct_spec(p: CodexDirectSpecParams<'_>) -> TaskSpec {
    // OpenAI Codex CLI: `exec --json`, target dir via -C (cwd inherited).
    let mut args: Vec<String> = vec!["exec".into(), "--json".into()];
    if p.bypass_sandbox {
        args.push("--dangerously-bypass-approvals-and-sandbox".into());
    }
    args.extend(["-C".into(), p.target_root.to_string_lossy().into_owned()]);
    args.extend(p.route.args.iter().cloned());
    args.push(p.task.into());

    TaskSpec {
        program: p.code_bin.to_string_lossy().into_owned(),
        args,
        cwd: None,
        envs: p.route.envs,
        stdin: StdinMode::Null,
        session_id: p.session_id,
        source: SpawnSource::Gui,
        is_codex: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env<'a>(spec: &'a TaskSpec, key: &str) -> Option<&'a str> {
        spec.envs
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    #[test]
    fn run_id_sanitizes_and_appends_timestamp() {
        let id = run_id_for_session("ab/c d!");
        assert!(id.starts_with("ab_c_d_-"), "unexpected: {id}");
        let ts: u128 = id.rsplit('-').next().unwrap().parse().unwrap();
        assert!(ts > 0);
    }

    #[test]
    fn promote_provider_codex_promotes_bare_browser_names() {
        for name in ["gemini", "chatgpt", "deepseek-browser"] {
            assert_eq!(
                promote_provider(Some(name.to_string()), "ollama", true),
                format!("browser:{name}")
            );
        }
        // Already-prefixed and non-browser providers pass through.
        assert_eq!(
            promote_provider(Some("browser:gemini".into()), "ollama", true),
            "browser:gemini"
        );
        assert_eq!(
            promote_provider(Some("openai".into()), "ollama", true),
            "openai"
        );
    }

    #[test]
    fn promote_provider_chat_mode_never_promotes() {
        assert_eq!(
            promote_provider(Some("gemini".into()), "ollama", false),
            "gemini"
        );
        assert_eq!(promote_provider(None, "ollama", false), "ollama");
        assert_eq!(
            promote_provider(Some("  ".into()), "browser", false),
            "browser"
        );
    }

    #[test]
    fn permission_mode_vocabulary_canonical_and_legacy() {
        for (mode, want) in [
            ("ask_risky", Some("high")),
            ("confirm-sensitive", Some("high")),
            ("ask_always", Some("medium")),
            ("confirm-all", Some("medium")),
            ("full_auto", None),
            ("auto", None),
            ("garbage", None),
        ] {
            let envs = EnvBuilder::new().permission_mode(Some(mode)).build();
            let got = envs
                .iter()
                .find(|(k, _)| k == "KIM_HITL_RISK_THRESHOLD")
                .map(|(_, v)| v.as_str());
            assert_eq!(got, want, "mode={mode}");
        }
        assert!(EnvBuilder::new().permission_mode(None).build().is_empty());
    }

    #[test]
    fn chat_spec_shape() {
        let spec = chat_task_spec(ChatSpecParams {
            python: "/venv/bin/python",
            bundled_sidecar: false,
            kim_root: Path::new("/kim"),
            project_root: Path::new("/proj"),
            session_dir: Path::new("/kim/kim_sessions"),
            task: "do it",
            provider: "ollama",
            session_id: "s1".into(),
            resume: Some("s1".into()),
            permission_mode: Some("ask_risky"),
            source: SpawnSource::Bridge,
            extra_envs: vec![("EXTRA".into(), "1".into())],
        });
        assert_eq!(
            spec.args,
            vec![
                "-m",
                "orchestrator.agent",
                "--task",
                "do it",
                "--session-dir",
                "/kim/kim_sessions",
                "--resume",
                "s1",
                "--provider",
                "ollama"
            ]
        );
        assert_eq!(spec.cwd.as_deref(), Some(Path::new("/kim")));
        assert_eq!(spec.stdin, StdinMode::Piped);
        assert!(!spec.is_codex);
        assert_eq!(env(&spec, "PYTHONPATH"), Some("/kim"));
        assert_eq!(env(&spec, "PROJECT_ROOT"), Some("/proj"));
        assert_eq!(env(&spec, "KIM_TAURI_MODE"), Some("1"));
        assert_eq!(env(&spec, "KIM_HITL_RISK_THRESHOLD"), Some("high"));
        assert_eq!(env(&spec, "EXTRA"), Some("1"));
        assert!(env(&spec, "KIM_RUN_ID").unwrap().starts_with("s1-"));
    }

    #[test]
    fn chat_spec_sidecar_omits_module_args_and_blank_resume_skipped() {
        let spec = chat_task_spec(ChatSpecParams {
            python: "/app/kim-orchestrator",
            bundled_sidecar: true,
            kim_root: Path::new("/kim"),
            project_root: Path::new("/kim"),
            session_dir: Path::new("/kim/kim_sessions"),
            task: "t",
            provider: "browser",
            session_id: "x".into(),
            resume: Some("  ".into()),
            permission_mode: None,
            source: SpawnSource::Gui,
            extra_envs: vec![],
        });
        assert_eq!(spec.args[0], "--task");
        assert!(!spec.args.contains(&"--resume".to_string()));
        assert!(!spec
            .envs
            .iter()
            .any(|(k, _)| k == "KIM_HITL_RISK_THRESHOLD"));
    }

    #[test]
    fn codex_browser_spec_shape() {
        let spec = codex_browser_spec(CodexBridgeSpecParams {
            python: "/venv/bin/python",
            kim_root: Path::new("/kim"),
            target_root: Path::new("/proj"),
            task: "build",
            provider: "browser:gemini",
            codex_bin: Path::new("/bin/codex"),
            session_id: "sess".into(),
            permission_mode: Some("ask_always"),
            extra_envs: vec![],
        });
        assert_eq!(
            spec.args,
            vec![
                "-m",
                "orchestrator.codex_bridge_service",
                "--task",
                "build",
                "--cwd",
                "/proj",
                "--provider",
                "browser:gemini"
            ]
        );
        assert_eq!(env(&spec, "CODEX_BIN"), Some("/bin/codex"));
        assert_eq!(env(&spec, "KIM_HITL_RISK_THRESHOLD"), Some("medium"));
        assert_eq!(spec.stdin, StdinMode::Piped);
        assert!(spec.is_codex);
    }

    #[test]
    fn codex_direct_spec_codex_shape_with_bypass_and_route() {
        let spec = codex_direct_spec(CodexDirectSpecParams {
            code_bin: Path::new("/bin/codex"),
            target_root: Path::new("/proj"),
            task: "fix bug",
            bypass_sandbox: true,
            route: ProviderRoute {
                args: vec!["--model".into(), "m1".into()],
                envs: vec![("OPENAI_API_KEY".into(), "k".into())],
                label: "test".into(),
            },
            session_id: "s".into(),
        });
        assert_eq!(
            spec.args,
            vec![
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                "/proj",
                "--model",
                "m1",
                "fix bug"
            ]
        );
        assert_eq!(spec.cwd, None);
        assert_eq!(spec.stdin, StdinMode::Null);
        assert_eq!(env(&spec, "OPENAI_API_KEY"), Some("k"));
    }

    #[test]
    fn codex_direct_spec_default_is_sandboxed() {
        let spec = codex_direct_spec(CodexDirectSpecParams {
            code_bin: Path::new("/bin/codex"),
            target_root: Path::new("/proj"),
            task: "t",
            bypass_sandbox: false,
            route: ProviderRoute::default(),
            session_id: "s".into(),
        });
        assert!(!spec
            .args
            .contains(&"--dangerously-bypass-approvals-and-sandbox".to_string()));
    }
}
