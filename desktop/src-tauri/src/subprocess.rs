use crate::*;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;
use tauri::State;

pub(crate) fn find_python_interpreter(project_root: &Path) -> Result<String, String> {
    let candidates = [
        project_root.join("venv").join("bin").join("python"),
        project_root.join(".venv").join("bin").join("python"),
        project_root.join("venv").join("Scripts").join("python.exe"),
        project_root.join(".venv").join("Scripts").join("python.exe"),
    ];

    for candidate in candidates {
        if candidate.exists() {
            return Ok(candidate.to_string_lossy().to_string());
        }
    }

    #[cfg(target_os = "windows")]
    let cmd_candidates = ["py", "python", "python3"];
    #[cfg(not(target_os = "windows"))]
    let cmd_candidates = ["python3", "python"];

    for cmd in cmd_candidates {
        if command_exists(cmd) {
            return Ok(cmd.to_string());
        }
    }

    Err(
        "No Python interpreter found. Install Python 3 or create a project venv (venv/.venv)."
            .to_string(),
    )
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CodeBackendKind {
    Codex,
    Claw,
}

impl CodeBackendKind {
    fn label(self) -> &'static str {
        match self {
            Self::Codex => "Codex",
            Self::Claw => "Claw compatibility",
        }
    }

    fn binary_label(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claw => "claw",
        }
    }
}

#[derive(Clone, Debug)]
struct CodeBackend {
    kind: CodeBackendKind,
    binary: PathBuf,
}

fn executable_from_env(key: &str, kind: CodeBackendKind) -> Option<CodeBackend> {
    let path = PathBuf::from(std::env::var(key).ok()?);
    if path.is_file() {
        Some(CodeBackend { kind, binary: path })
    } else {
        None
    }
}

fn executable_on_path(name: &str, kind: CodeBackendKind) -> Option<CodeBackend> {
    let out = std::process::Command::new("which").arg(name).output().ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(CodeBackend { kind, binary: PathBuf::from(s) })
    }
}

fn bundled_code_backend(kim_root: &Path, kind: CodeBackendKind) -> Option<CodeBackend> {
    let mut roots: Vec<PathBuf> = vec![kim_root.to_path_buf()];
    if let Some(parent) = kim_root.parent() {
        roots.push(parent.to_path_buf());
    }
    let relative = match kind {
        CodeBackendKind::Codex => "pythonExperimentTool/codex-code/rust/target",
        CodeBackendKind::Claw => "pythonExperimentTool/claw-code/rust/target",
    };
    let binary_name = kind.binary_label();
    for root in &roots {
        for sub in &["release", "debug"] {
            let p = root.join(relative).join(sub).join(binary_name);
            if p.is_file() {
                return Some(CodeBackend { kind, binary: p });
            }
        }
    }
    None
}

/// Locate the code-agent binary. Prefer the post-migration `codex` binary,
/// but fall back to the bundled `claw` binary while this branch still carries
/// the old Rust workspace layout.
fn find_code_backend(kim_root: &Path) -> Option<CodeBackend> {
    if let Ok(p) = std::env::var("CODEX_BIN") {
        let path = PathBuf::from(p);
        if path.is_file() {
            let filename = path.file_name().and_then(|name| name.to_str()).unwrap_or_default();
            let kind = if filename == "claw" {
                CodeBackendKind::Claw
            } else {
                CodeBackendKind::Codex
            };
            return Some(CodeBackend { kind, binary: path });
        }
    }
    executable_from_env("CLAW_BIN", CodeBackendKind::Claw)
        .or_else(|| bundled_code_backend(kim_root, CodeBackendKind::Codex))
        .or_else(|| executable_on_path("codex", CodeBackendKind::Codex))
        .or_else(|| bundled_code_backend(kim_root, CodeBackendKind::Claw))
        .or_else(|| executable_on_path("claw", CodeBackendKind::Claw))
}

#[allow(clippy::too_many_arguments)]
#[tauri::command]
pub(crate) async fn send_task(
    task: String,
    provider: Option<String>,
    project_root: Option<String>,
    resume_session_id: Option<String>,
    google_authuser: Option<u32>,
    // Forwarded as KIM_CONTEXT_BUDGET_TOKENS for orchestrator context meter.
    context_budget_tokens: Option<u32>,
    ollama_base_url: Option<String>,
    ollama_mode: Option<String>,
    ollama_local_model: Option<String>,
    ollama_cloud_model: Option<String>,
    ollama_context_limit_override: Option<u32>,
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    use std::process::Stdio;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    // Refuse to start a second task if one is already running or spawning.
    {
        let guard = state.lock().await;
        if guard.pid.is_some() || guard.starting {
            return Err("A task is already running. Stop it before starting a new one.".to_string());
        }
    }
    let app_config = app_handle.state::<config::AppConfig>();

    // The Kim repo root (where orchestrator/, mcp_server/, and venv/ live).
    // This is ALWAYS used for finding the Python interpreter, setting PYTHONPATH,
    // and as the working directory so `python -m orchestrator.agent` resolves.
    let kim_root = default_project_root();

    // The target project the user wants Kim/Codex to work on.  For the normal
    // Chat tab this is typically the Kim repo itself.  For the Code tab it is
    // the user's external code project.  MCP tools use PROJECT_ROOT to know
    // which directory to operate in (file reads/writes, git, search, etc.).
    let target_root = project_root
        .map(PathBuf::from)
        .filter(|p| p.exists())
        .unwrap_or_else(|| kim_root.clone());

    // Codex mode is ONLY when the target project is a real external project,
    // i.e. distinct from the Kim repo itself. The Chat tab passes the Kim
    // repo as project_root (so MCP tools know the workspace), but those
    // sessions must still land in kim_sessions/, not .codex/sessions/.
    let is_codex = target_root.canonicalize().ok()
        != kim_root.canonicalize().ok();

    let session_dir = if is_codex {
        target_root.join(".codex").join("sessions")
    } else {
        kim_root.join("kim_sessions")
    };

    // Default to Ollama (no API key in Kim) when the caller omits a provider.
    // The frontend normally sends an explicit provider, but keeping this
    // fallback aligned with Settings avoids surprise Browser runs.
    let provider_arg = {
        let raw = provider
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| "ollama".to_string());
        // In Code-tab (Codex) mode, bare "gemini" / "claude" / "chatgpt" etc.
        // have no direct API key path in Kim — promote them to "browser:<name>"
        // so they route through the browser bridge automatically.
        if is_codex {
            let lower = raw.trim().to_ascii_lowercase();
            match lower.as_str() {
                "gemini" | "claude" | "chatgpt" | "grok" | "deepseek-browser"
                    if !raw.starts_with("browser:") =>
                {
                    format!("browser:{}", lower)
                }
                _ => raw,
            }
        } else {
            raw
        }
    };
    // Whether the user picked a browser-backed provider in the composer (e.g.
    // "browser:gemini"). For Code-tab tasks this routes Codex through Kim's
    // BrowserProvider via the file-bridge instead of calling Anthropic.
    let is_browser_provider = provider_arg == "browser" || provider_arg.starts_with("browser:");

    let code_backend = if is_codex {
        Some(find_code_backend(&kim_root).ok_or_else(|| {
            "Code agent binary not found. Build `pythonExperimentTool/codex-code/rust` for Codex, build `pythonExperimentTool/claw-code/rust` for the bundled compatibility backend, or set CODEX_BIN/CLAW_BIN to the binary path.".to_string()
        })?)
    } else {
        None
    };

    // Code sessions run the coding-agent binary directly against the user's
    // project, with its own coding-agent system prompt. Chat (Kim) sessions
    // continue to run `python -m orchestrator.agent` with the desktop-control
    // system prompt. Routing them through different binaries is what keeps the
    // two personas separate end-to-end.
    let mut cmd = if is_codex {
        let code_backend = code_backend
            .as_ref()
            .expect("code backend resolved for code mode");
        let code_bin = &code_backend.binary;

        if is_browser_provider {
            if code_backend.kind == CodeBackendKind::Claw {
                return Err("Browser-backed Code mode needs the Codex binary. This checkout only has the bundled Claw compatibility binary, so switch the provider to Ollama/OpenAI or build/set CODEX_BIN.".to_string());
            }
            // ── Browser-bridge mode ──────────────────────────────────────
            // Spawn `python -m orchestrator.run_codex_bridge`, which spawns
            // Codex with CODEX_FILE_BRIDGE=1 and relays each LLM request
            // through Kim's BrowserProvider. No Anthropic key required.
            let python = find_python_interpreter(&kim_root)?;
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] Routing to Codex via Kim's browser provider ({})", provider_arg),
            );
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] codex binary: {}", code_bin.display()),
            );
            let mut c = Command::new(&python);
            c.args(["-m", "orchestrator.run_codex_bridge"])
                .arg("--task").arg(&task)
                .arg("--cwd").arg(target_root.to_string_lossy().to_string())
                .arg("--provider").arg(&provider_arg)
                .current_dir(&kim_root)
                .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
                // Tell run_codex_subtask exactly which codex binary to spawn,
                // so it doesn't have to repeat the search dance.
                .env("CODEX_BIN", code_bin.to_string_lossy().to_string())
                // Forward webview-bridge creds so BrowserProvider can drive
                // the in-app sign-in window in headless mode if configured.
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            c
        } else {
            // ── Direct API mode ──────────────────────────────────────────
            let mut c = Command::new(code_bin);
            if code_backend.kind == CodeBackendKind::Codex {
                // New OpenAI Codex CLI: uses `exec --json` subcommand.
                // When the provider is Ollama, use --oss --local-provider ollama
                // so Codex bypasses its own ChatGPT account auth entirely and
                // routes through the local Ollama daemon instead.
                c.arg("exec")
                    .arg("--json")
                    .arg("--dangerously-bypass-approvals-and-sandbox")
                    .arg("-C").arg(target_root.to_string_lossy().to_string())
                    .stdin(Stdio::null())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                let route = if provider_arg.trim().eq_ignore_ascii_case("ollama") {
                    let model = selected_ollama_codex_model(
                        ollama_mode.as_deref(),
                        ollama_base_url.as_deref(),
                        ollama_local_model.as_deref(),
                        ollama_cloud_model.as_deref(),
                        &app_config,
                    ).await?;
                    c.arg("--oss")
                        .arg("--local-provider").arg("ollama")
                        .arg("--model").arg(&model);
                    format!("Ollama via local daemon ({model})")
                } else {
                    configure_codex_direct_provider(
                        &mut c,
                        &provider_arg,
                        &kim_root,
                        ollama_base_url.as_deref(),
                        ollama_mode.as_deref(),
                        ollama_local_model.as_deref(),
                        ollama_cloud_model.as_deref(),
                        &app_config,
                    ).await?
                };
                let _ = app_handle.emit(
                    "kim-agent-output",
                    format!(
                        "[STATUS] ✓ Using Codex CLI via {}: {}",
                        route,
                        code_bin.display(),
                    ),
                );
                c.arg(&task);
            } else {
                // Claw compatibility binary: uses old --output-format json interface.
                c.arg("--output-format").arg("json")
                    .current_dir(&target_root)
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                let route = configure_codex_direct_provider(
                    &mut c,
                    &provider_arg,
                    &kim_root,
                    ollama_base_url.as_deref(),
                    ollama_mode.as_deref(),
                    ollama_local_model.as_deref(),
                    ollama_cloud_model.as_deref(),
                    &app_config,
                ).await?;
                let _ = app_handle.emit(
                    "kim-agent-output",
                    format!(
                        "[STATUS] Routing code task to {} via {}: {}",
                        code_backend.kind.label(),
                        route,
                        code_bin.display(),
                    ),
                );
                c.arg("prompt").arg(&task);
            }
            c
        }
    } else {
        let python = find_python_interpreter(&kim_root)?;
        let mut c = Command::new(&python);
        c.args(["-m", "orchestrator.agent"])
            .arg("--task")
            .arg(&task)
            .arg("--session-dir")
            .arg(session_dir.to_string_lossy().to_string())
            .current_dir(&kim_root)
            // Tell the MCP server which directory to operate on (file tools, git, etc.).
            // For the Chat tab this is the Kim repo itself.
            .env("PROJECT_ROOT", target_root.to_str().unwrap_or(""))
            // Ensure `import orchestrator` and `import mcp_server` always resolve
            // from the Kim repo, regardless of the target project.
            .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        c
    };

    let bridge_cfg = WEBVIEW_BRIDGE_CFG.get().cloned();
    // The webview-bridge creds let BrowserProvider drive the in-app sign-in
    // window. Forward them whenever a browser provider is in play, including
    // Codex-via-browser-bridge runs, so headless flows stay consistent.
    if !is_codex || is_browser_provider {
        if let Some(cfg) = &bridge_cfg {
            cmd.env("KIM_WEBVIEW_BRIDGE_URL", &cfg.base_url)
                .env("KIM_WEBVIEW_BRIDGE_TOKEN", &cfg.token)
                .env("KIM_WEBVIEW_WINDOW_LABEL", "kim-browser-signin");
        }
    }

    if is_browser_provider {
        let restore_status = browser_restore_status_for_session(
            &session_dir,
            resume_session_id.as_deref(),
            &provider_arg,
        );
        cmd.env("KIM_BROWSER_RESTORE_STATUS", restore_status);
    }

    if !is_codex && provider_arg.trim().eq_ignore_ascii_case("gemini") {
        let google_env = google_oauth::google_oauth_env_for_agent().await.map_err(|err| {
            format!(
                "Google for Kim is not connected. Open Settings → Account → Google for Kim (API), then Continue with Google. {}",
                err
            )
        })?;
        for (key, value) in google_env.as_env_pairs() {
            cmd.env(key, value);
        }
    }

    // Chrome CDP launch is needed whenever a browser provider is in play —
    // both for Kim's Chat-tab tasks and for Codex running in browser-bridge
    // mode (which calls into BrowserProvider via run_codex_bridge).
    if is_browser_provider {
        if bridge_cfg.is_none() {
            let root_for_chrome = kim_root.clone();
            match tokio::task::spawn_blocking(move || launch_chrome_for_cdp(&root_for_chrome)).await {
                Ok(Ok(true)) => {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
                Ok(Ok(false)) => {}
                Ok(Err(e)) => {
                    eprintln!("[Kim] Chrome launch skipped: {}", e);
                }
                Err(e) => {
                    eprintln!("[Kim] Chrome launch task panicked: {}", e);
                }
            }
        } else {
            eprintln!("[Kim] Browser provider using in-app bridge (no Chrome CDP launch)");
        }
    }

    let resume_arg = if is_codex {
        None
    } else {
        resume_session_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string())
    };
    if let Some(resume_id) = resume_arg {
        cmd.arg("--resume").arg(resume_id);
    }

    if !is_codex {
        cmd.arg("--provider").arg(&provider_arg);
    }

    if !is_codex && provider_arg.trim().eq_ignore_ascii_case("ollama") {
        if let Some(base_url) = ollama_base_url.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_BASE_URL", base_url);
        }
        if let Some(mode) = ollama_mode.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_MODE", mode);
        }
        if let Some(model) = ollama_local_model.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_LOCAL_MODEL", model);
        }
        if let Some(model) = ollama_cloud_model.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            cmd.env("KIM_OLLAMA_CLOUD_MODEL", model);
        }
        if let Some(limit) = ollama_context_limit_override.filter(|n| *n > 0) {
            cmd.env("KIM_OLLAMA_CONTEXT_LIMIT_OVERRIDE", limit.to_string());
        }
    }

    if let Some(idx) = google_authuser {
        cmd.env("KIM_GEMINI_AUTHUSER", idx.to_string());
    }

    if !is_codex {
        if let Some(budget) = context_budget_tokens.filter(|b| *b > 0) {
            cmd.env("KIM_CONTEXT_BUDGET_TOKENS", budget.to_string());
        }
    }

    #[cfg(unix)]
    {
        cmd.process_group(0);
    }

    // Reserve the runner slot immediately before spawning. Command setup can be
    // slow, and two frontend invocations can otherwise both pass the initial
    // pid check and spawn separate agents.
    {
        let mut guard = state.lock().await;
        if guard.pid.is_some() || guard.starting {
            return Err("A task is already running. Stop it before starting a new one.".to_string());
        }
        guard.starting = true;
    }

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => {
            let mut guard = state.lock().await;
            guard.starting = false;
            return Err(format!("Failed to start Kim: {}", e));
        }
    };

    // Record the PID so cancel_task can signal it.
    let child_pid = child.id();
    {
        let mut guard = state.lock().await;
        guard.pid = child_pid;
        guard.starting = false;
    }
    // Also sync to the bridge-accessible static so kimctl cancel works.
    if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = child_pid;
    }

    let stdout_handle = if let Some(stdout) = child.stdout.take() {
        let reader = tokio::io::BufReader::new(stdout);
        let app = app_handle.clone();
        Some(tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-output", line);
            }
        }))
    } else {
        None
    };

    let stderr_handle = if let Some(stderr) = child.stderr.take() {
        let reader = tokio::io::BufReader::new(stderr);
        let app = app_handle.clone();
        Some(tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-error", line);
            }
        }))
    } else {
        None
    };

    let status = child.wait().await.map_err(|e| e.to_string())?;

    if let Some(handle) = stdout_handle {
        let _ = handle.await;
    }
    if let Some(handle) = stderr_handle {
        let _ = handle.await;
    }

    // Clear the recorded PID regardless of exit reason (normal, error, cancelled).
    {
        let mut guard = state.lock().await;
        guard.pid = None;
        guard.starting = false;
    }
    // Clear bridge-accessible PID too.
    if let Ok(mut guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = None;
    }
    if let Ok(mut guard) = BRIDGE_TASK_SESSION.get_or_init(|| StdMutex::new(None)).lock() {
        *guard = None;
    }

    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    if is_codex {
        if code_backend
            .as_ref()
            .is_some_and(|backend| backend.kind == CodeBackendKind::Claw)
        {
            let _ = mirror_latest_claw_session_to_codex(&target_root);
        }
        if let Some(session) = newest_codex_session(&target_root) {
            let _ = app_handle.emit("kim-agent-code-session", session);
        }
    }

    let _ = app_handle.emit("kim-agent-done", status.success());

    // Always return Ok — the frontend learns about failure via the kim-agent-done
    // event (payload = false). Returning Err here causes a second error path in the
    // JS catch block, leading to duplicate error messages and UI state conflicts.
    Ok(if status.success() { "Task completed".to_string() } else { "Task ended".to_string() })
}

// ---------------------------------------------------------------------------
// Cancel a running task — SIGTERM, then SIGKILL after 2s if still alive.
// ---------------------------------------------------------------------------

/// Clear the task PID from BOTH tracking stores so subsequent cancel/status
/// calls don't see a stale PID regardless of how the task was started.
async fn clear_task_pid(state: &TaskState) {
    let mut guard = state.lock().await;
    guard.pid = None;
    guard.starting = false;
    if let Ok(mut bridge_guard) = BRIDGE_TASK_PID.get_or_init(|| StdMutex::new(None)).lock() {
        *bridge_guard = None;
    }
}

#[tauri::command]
pub(crate) async fn cancel_task(
    app_handle: tauri::AppHandle,
    state: State<'_, TaskState>,
) -> Result<String, String> {
    let (pid, starting) = {
        let guard = state.lock().await;
        (guard.pid, guard.starting)
    };

    // Fall back to the bridge-registered PID. Tasks started via kimctl /v1/task
    // register in BRIDGE_TASK_PID, not TaskState — so the GUI Stop button needs
    // to check both stores or it silently no-ops while the task keeps running.
    let pid = pid.or_else(|| {
        BRIDGE_TASK_PID
            .get_or_init(|| StdMutex::new(None))
            .lock()
            .ok()
            .and_then(|g| *g)
    });

    let Some(pid) = pid else {
        if starting {
            return Err("Task is still starting. Try stopping again in a moment.".to_string());
        }
        return Err("No task is currently running.".to_string());
    };

    // Step 1 — graceful signal.
    send_signal(pid, false)
        .map_err(|e| format!("Failed to send stop signal: {}", e))?;

    // Restore the UI immediately. If the agent is cancelled while a screenshot
    // tool is blocking, Python may never reach its finally block that normally
    // shows the main window again.
    let _ = set_task_active_mode(app_handle.clone(), false).await;
    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
        let _ = flash_win.close();
    }

    // Step 2 — wait up to 2 seconds for the process to exit; if it's still
    // alive, force kill. We poll by re-sending signal 0 (existence check).
    let app = app_handle.clone();
    // Clone the inner Arc so the spawned task has its own handle —
    // dereference State<'_, TaskState> to TaskState (= Arc<Mutex<…>>),
    // then Arc::clone just bumps the refcount.
    let state_clone: TaskState = Arc::clone(&*state);
    tokio::spawn(async move {
        for _ in 0..20 {
            tokio::time::sleep(Duration::from_millis(100)).await;
            if !process_exists(pid) {
                clear_task_pid(&state_clone).await;
                let _ = app.emit("kim-agent-cancelled", true);
                return;
            }
        }
        // Still alive after 2s → SIGKILL / taskkill /F.
        let _ = send_signal(pid, true);
        clear_task_pid(&state_clone).await;
        let _ = app.emit("kim-agent-cancelled", true);
    });

    Ok("Cancelling task…".to_string())
}

// ── Platform-specific signalling ─────────────────────────────────────────────

#[cfg(unix)]
pub(crate) fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    let sig = if force { "-KILL" } else { "-TERM" };
    // Kill the process group (-pid) so child processes are also terminated (#64).
    // If group kill fails (e.g. the process is not a group leader), fall back
    // to killing only the parent process.
    let neg_pid = format!("-{}", pid);
    let status = Command::new("kill")
        .args([sig, &neg_pid])
        .status();
    match status {
        Ok(s) if s.success() => Ok(()),
        _ => {
            // Fallback: kill just the parent process
            let status = Command::new("kill")
                .args([sig, &pid.to_string()])
                .status()?;
            if !status.success() {
                return Err(std::io::Error::other(
                    format!("kill {} {} failed with {}", sig, pid, status),
                ));
            }
            Ok(())
        }
    }
}

#[cfg(unix)]
pub(crate) fn process_exists(pid: u32) -> bool {
    use std::process::Command;
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

#[cfg(windows)]
pub(crate) fn send_signal(pid: u32, force: bool) -> std::io::Result<()> {
    use std::process::Command;
    // Windows has no SIGTERM; use taskkill. /T kills the process tree so
    // the Python interpreter and any child processes it spawned go too.
    let mut cmd = Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T"]);
    if force {
        cmd.arg("/F");
    }
    let status = cmd.status()?;
    if !status.success() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("taskkill failed with {}", status),
        ));
    }
    Ok(())
}

#[cfg(windows)]
pub(crate) fn process_exists(pid: u32) -> bool {
    use std::process::Command;
    // `tasklist /FI "PID eq N"` prints a header only when there's no match.
    match Command::new("tasklist")
        .args(["/FI", &format!("PID eq {}", pid), "/NH"])
        .output()
    {
        Ok(out) => {
            let s = String::from_utf8_lossy(&out.stdout);
            s.contains(&pid.to_string())
        }
        Err(_) => false,
    }
}
