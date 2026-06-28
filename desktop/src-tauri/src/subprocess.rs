use crate::*;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex as StdMutex, OnceLock};
use std::time::Duration;
use tauri::State;
use tokio::sync::Mutex as TokioMutex;

// ── HITL stdin channel (Rust → Python approval relay) ────────────────────────
//
// The running agent's stdin handle is stored here after spawn so that
// `hitl_respond_approval` can write the user's approve/deny decision to it.
// Each new task replaces the stored handle (old handle dropped = pipe closed).
fn hitl_stdin() -> &'static TokioMutex<Option<tokio::process::ChildStdin>> {
    static HITL_STDIN: OnceLock<TokioMutex<Option<tokio::process::ChildStdin>>> = OnceLock::new();
    HITL_STDIN.get_or_init(|| TokioMutex::new(None))
}

/// Write the user's HITL approval decision to the running agent's stdin.
/// Python's StdinApprovalBridge reads this line to unblock confirm_action().
#[tauri::command]
pub(crate) async fn hitl_respond_approval(approved: bool) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;
    let mut guard = hitl_stdin().lock().await;
    if let Some(ref mut stdin) = *guard {
        let msg = if approved {
            "{\"type\":\"hitl_approve\",\"approved\":true}\n"
        } else {
            "{\"type\":\"hitl_approve\",\"approved\":false}\n"
        };
        stdin.write_all(msg.as_bytes()).await.map_err(|e| e.to_string())?;
        stdin.flush().await.map_err(|e| e.to_string())
    } else {
        Err("No agent stdin available for HITL response".to_string())
    }
}

/// K3: write a mid-run steering message to the running agent's stdin. Python's
/// stdin pump folds it into memory as a user message before the next LLM call.
#[tauri::command]
pub(crate) async fn steer_task(text: String) -> Result<(), String> {
    use tokio::io::AsyncWriteExt;
    let payload = serde_json::json!({ "type": "user_steer", "text": text }).to_string();
    let mut guard = hitl_stdin().lock().await;
    if let Some(ref mut stdin) = *guard {
        stdin
            .write_all(format!("{payload}\n").as_bytes())
            .await
            .map_err(|e| e.to_string())?;
        stdin.flush().await.map_err(|e| e.to_string())
    } else {
        Err("No agent stdin available for steering".to_string())
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum KimEvent {
    Status { message: String },
    Plan { steps: Vec<serde_json::Value> },
    Step { n: usize, data: serde_json::Value },
    Done { n: usize },
    Context {
        cumulative_input: u64,
        budget: u64,
        phase: String,
        percent: u32,
        last_input: u64,
        last_output: u64,
        source: String,
        estimate: bool,
    },
    Stats { input: u64, output: u64, total: u64 },
    UiScreenshotFlash,
    UiShow,
    RunDone { termination: String, success: bool },
    RunFailed { reason: String, recoverable: bool, suggestion: String },
    ProviderError { code: String, retryable: bool },
    RateLimited { delay: f64, attempt: u32, max_retries: u32 },
    HitlApprovalRequest {
        tool: String,
        risk: String,
        reason: String,
        // K6: optional preview (command / unified diff / URL+label).
        #[serde(default)]
        preview: String,
    },
    HitlApprovalResult { tool: String, approved: bool },
}

/// Resolve a Python interpreter or bundled orchestrator sidecar.
///
/// Resolution order (first match wins):
///   1. Bundled `kim-orchestrator` sidecar adjacent to the Tauri executable
///      (set when running as a packaged .app; Tauri places sidecars in the
///      same MacOS/ directory as the main binary).
///   2. `.kim_root/venv` or `.kim/venv` under the user's home directory
///      (created by the install script).
///   3. Project-local `venv/` or `.venv/` under `project_root`.
///   4. System-level `python3` / `python` on PATH.
///
/// When the sidecar is found, the caller should invoke it directly (as a
/// standalone executable) rather than as `<interpreter> -m orchestrator.agent`.
/// The caller is responsible for detecting sidecar mode via
/// `is_bundled_orchestrator()` and adjusting the command accordingly.
pub(crate) fn find_python_interpreter(project_root: &Path) -> Result<String, String> {
    // ── 1. Bundled sidecar — highest priority when running as a packaged app ──
    if let Some(sidecar) = find_bundled_orchestrator() {
        return Ok(sidecar);
    }

    // ── 2. Install-script venv in ~/.kim_root or ~/.kim ───────────────────────
    if let Some(home) = dirs_home() {
        let install_candidates = [
            home.join(".kim_root").join("venv").join("bin").join("python"),
            home.join(".kim_root").join(".venv").join("bin").join("python"),
            home.join(".kim").join("venv").join("bin").join("python"),
            home.join(".kim").join(".venv").join("bin").join("python"),
        ];
        for c in install_candidates {
            if c.exists() {
                return Ok(c.to_string_lossy().to_string());
            }
        }
    }

    // ── 3. Project-local venv ─────────────────────────────────────────────────
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

    // ── 4. System Python ──────────────────────────────────────────────────────
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
        "No Python interpreter found. Install Python 3, create a project venv (venv/.venv), \
         or rebuild the app with the bundled kim-orchestrator sidecar."
            .to_string(),
    )
}

/// Returns the path to the bundled `kim-orchestrator` sidecar if running
/// as a packaged Tauri app. Returns `None` in development mode.
///
/// On macOS, Tauri places sidecars in `<app>.app/Contents/MacOS/` adjacent
/// to the main binary. The sidecar binary name carries the platform target
/// triple at bundle time but is resolved without it at runtime via the
/// `externalBin` Tauri config mechanism. We look for both the bare name and
/// the current-platform suffixed name so the function works in both contexts.
fn find_bundled_orchestrator() -> Option<String> {
    // Resolve the directory containing the current executable.
    let exe = std::env::current_exe().ok()?;
    let exe_dir = exe.parent()?;

    // On macOS .app bundles, exe_dir ends with Contents/MacOS/.
    // On Linux AppImage / Windows installer, exe_dir is the install root.
    let sidecar_name = "kim-orchestrator";
    let target_triple = std::env::consts::ARCH.to_string()
        + "-"
        + if cfg!(target_os = "macos") {
            "apple-darwin"
        } else if cfg!(target_os = "linux") {
            "unknown-linux-gnu"
        } else if cfg!(target_os = "windows") {
            "pc-windows-msvc"
        } else {
            "unknown"
        };

    // Try bare name first (development builds / installed via direct copy).
    let bare = exe_dir.join(sidecar_name);
    if bare.exists() {
        return Some(bare.to_string_lossy().to_string());
    }

    // Try platform-suffixed name (Tauri sidecar bundle convention).
    let suffixed = exe_dir.join(format!("{}-{}", sidecar_name, target_triple));
    if suffixed.exists() {
        return Some(suffixed.to_string_lossy().to_string());
    }

    // Windows: also check for .exe extension.
    #[cfg(target_os = "windows")]
    {
        let win_bare = exe_dir.join(format!("{}.exe", sidecar_name));
        if win_bare.exists() {
            return Some(win_bare.to_string_lossy().to_string());
        }
    }

    None
}

/// Returns true when the resolved interpreter path points to the bundled
/// `kim-orchestrator` sidecar (a standalone executable, not a Python binary).
/// The caller must invoke it directly rather than via `python -m orchestrator.agent`.
pub(crate) fn is_bundled_orchestrator(interpreter: &str) -> bool {
    interpreter.contains("kim-orchestrator")
}

/// Best-effort home directory resolution (avoids pulling in the `dirs` crate).
fn dirs_home() -> Option<PathBuf> {
    // Try $HOME on Unix / $USERPROFILE on Windows.
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .ok()
        .map(PathBuf::from)
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
    // Use the platform-appropriate PATH search command (#20).
    #[cfg(target_os = "windows")]
    let search_cmd = ("where", name);
    #[cfg(not(target_os = "windows"))]
    let search_cmd = ("which", name);

    let out = std::process::Command::new(search_cmd.0).arg(search_cmd.1).output().ok()?;
    if !out.status.success() {
        return None;
    }
    // `where` on Windows returns one match per line; take the first.
    let s = String::from_utf8_lossy(&out.stdout)
        .lines()
        .next()
        .unwrap_or("")
        .trim()
        .to_string();
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
    // HITL permission mode: "full_auto" | "ask_risky" | "ask_always"
    // Maps to KIM_HITL_RISK_THRESHOLD: full_auto=off, ask_risky=high, ask_always=medium
    permission_mode: Option<String>,
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
            c.args(["-m", "orchestrator.codex_bridge_service"])
                .arg("--task").arg(&task)
                .arg("--cwd").arg(target_root.to_string_lossy().to_string())
                .arg("--provider").arg(&provider_arg)
                .current_dir(&kim_root)
                .env("PYTHONPATH", kim_root.to_str().unwrap_or(""))
                // Tell run_codex_subtask exactly which codex binary to spawn,
                // so it doesn't have to repeat the search dance.
                .env("CODEX_BIN", code_bin.to_string_lossy().to_string())
                // Signal Tauri mode so codex_bridge_service emits a HITL
                // approval request before spawning Codex (#2). Stdin pipe is
                // required for the Python side to receive the approval decision.
                .env("KIM_TAURI_MODE", "1")
                .stdin(Stdio::piped())
                // Forward webview-bridge creds so BrowserProvider can drive
                // the in-app sign-in window in headless mode if configured.
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            // Map permission_mode → KIM_HITL_RISK_THRESHOLD (same vocab as send_task).
            match permission_mode.as_deref().unwrap_or("full_auto") {
                "ask_risky" => { c.env("KIM_HITL_RISK_THRESHOLD", "high"); }
                "ask_always" => { c.env("KIM_HITL_RISK_THRESHOLD", "medium"); }
                _ => {} // full_auto: no gate
            }
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
                    .arg("--json");
                // Gate the sandbox-bypass flag behind an explicit opt-in env var (#1).
                // Default: sandboxed + approvals enabled.
                if std::env::var("KIM_CODEX_BYPASS_SANDBOX").as_deref() == Ok("1") {
                    c.arg("--dangerously-bypass-approvals-and-sandbox");
                }
                c.arg("-C").arg(target_root.to_string_lossy().to_string())
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
        // K1: unique per-run id for file checkpoints (~/.kim/checkpoints/<id>).
        let sess = resume_session_id.as_deref().unwrap_or("run");
        let sess_safe: String = sess
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '_' })
            .collect();
        let run_id = format!(
            "{}-{}",
            sess_safe,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis())
                .unwrap_or(0)
        );
        c.env("KIM_RUN_ID", &run_id);
        // Let the frontend associate this run's pill with its checkpoint id.
        let _ = app_handle.emit("kim-run-id", run_id.clone());
        // When the resolved interpreter is the bundled sidecar, invoke it
        // directly (it is a standalone executable, not a Python binary).
        // Otherwise use the standard `python -m orchestrator.agent` invocation.
        if !is_bundled_orchestrator(&python) {
            c.args(["-m", "orchestrator.agent"]);
        }
        c.arg("--task")
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
            // Signal to Python that we are running under Tauri so StdinApprovalBridge
            // is auto-wired when a HITL risk threshold is set.
            .env("KIM_TAURI_MODE", "1")
            // Pipe stdin so hitl_respond_approval can write approval decisions to it.
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        // Map permission_mode → KIM_HITL_RISK_THRESHOLD env var.
        match permission_mode.as_deref().unwrap_or("full_auto") {
            "ask_risky" => { c.env("KIM_HITL_RISK_THRESHOLD", "high"); }
            "ask_always" => { c.env("KIM_HITL_RISK_THRESHOLD", "medium"); }
            _ => {} // full_auto: no threshold = no HITL gate
        }
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
    // K7: reflect the running task in the tray status line.
    crate::speed_access::set_tray_status(&app_handle, Some(task.as_str()));

    // Store stdin handle for HITL approval round-trip (only for Kim orchestrator, not Codex).
    if !is_codex {
        *hitl_stdin().lock().await = child.stdin.take();
    }

    let ipc_typed = app_config.ipc_protocol == "typed";
    let stdout_handle = if let Some(stdout) = child.stdout.take() {
        let reader = tokio::io::BufReader::new(stdout);
        let app = app_handle.clone();
        Some(tokio::spawn(async move {
            let mut lines = reader.lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if ipc_typed {
                    if let Ok(event) = serde_json::from_str::<KimEvent>(&line) {
                        match &event {
                            KimEvent::Status { message } => {
                                let _ = app.emit("kim:status", serde_json::json!({"message": message}));
                            }
                            KimEvent::Plan { steps } => {
                                let _ = app.emit("kim:plan", serde_json::json!({"steps": steps}));
                            }
                            KimEvent::Step { n, data } => {
                                let _ = app.emit("kim:step", serde_json::json!({"n": n, "data": data}));
                            }
                            KimEvent::Done { n } => {
                                let _ = app.emit("kim:done", serde_json::json!({"n": n}));
                            }
                            KimEvent::Context { cumulative_input, budget, phase, percent, last_input, last_output, source, estimate } => {
                                let _ = app.emit("kim:context", serde_json::json!({
                                    "cumulative_input": cumulative_input,
                                    "budget": budget,
                                    "phase": phase,
                                    "percent": percent,
                                    "last_input": last_input,
                                    "last_output": last_output,
                                    "source": source,
                                    "estimate": estimate,
                                }));
                            }
                            KimEvent::Stats { input, output, total } => {
                                let _ = app.emit("kim:stats", serde_json::json!({"input": input, "output": output, "total": total}));
                            }
                            KimEvent::UiScreenshotFlash => {
                                let _ = app.emit("kim:ui", serde_json::json!({"action": "screenshot_flash"}));
                            }
                            KimEvent::UiShow => {
                                let _ = app.emit("kim:ui", serde_json::json!({"action": "show"}));
                            }
                            KimEvent::RunDone { termination, success } => {
                                let _ = app.emit("kim:run-done", serde_json::json!({"termination": termination, "success": success}));
                            }
                            KimEvent::RunFailed { reason, recoverable, suggestion } => {
                                let _ = app.emit("kim:run-failed", serde_json::json!({
                                    "reason": reason,
                                    "recoverable": recoverable,
                                    "suggestion": suggestion,
                                }));
                            }
                            KimEvent::ProviderError { code, retryable } => {
                                let _ = app.emit("kim:provider-error", serde_json::json!({"code": code, "retryable": retryable}));
                            }
                            KimEvent::RateLimited { delay, attempt, max_retries } => {
                                let _ = app.emit("kim:rate-limited", serde_json::json!({
                                    "delay": delay,
                                    "attempt": attempt,
                                    "max_retries": max_retries,
                                }));
                            }
                            KimEvent::HitlApprovalRequest { tool, risk, reason, preview } => {
                                let _ = app.emit("kim:hitl-approval-request", serde_json::json!({"tool": tool, "risk": risk, "reason": reason, "preview": preview}));
                            }
                            KimEvent::HitlApprovalResult { tool, approved } => {
                                let _ = app.emit("kim:hitl-approval-result", serde_json::json!({"tool": tool, "approved": approved}));
                            }
                        }
                    } else {
                        // Not a typed KimEvent — forward on legacy channel for [TOOL] / [SUCCESS]
                        // text lines that still come from the Kim-agent path (those do not JSON-parse
                        // as KimEvent and carry real activity-feed content).
                        let _ = app.emit("kim-agent-output", &line);
                    }
                } else {
                    // Codex CLI subprocess path — all lines forwarded on the legacy channel.
                    // Codex exec speaks item.completed JSONL + [STATUS] / [ANSWER] text protocol;
                    // that output format is not under our control and cannot be schema-firsted.
                    let _ = app.emit("kim-agent-output", &line);
                }
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
    // K7: tray back to idle.
    crate::speed_access::set_tray_status(&app_handle, None);
    // Drop the HITL stdin handle so the pipe is closed.
    *hitl_stdin().lock().await = None;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn test_parse_run_done_event() {
        let json = r#"{"type":"run_done","termination":"task_complete","success":true}"#;
        let event: KimEvent = serde_json::from_str(json).expect("run_done should deserialize");
        match event {
            KimEvent::RunDone { termination, success } => {
                assert_eq!(termination, "task_complete");
                assert!(success);
            }
            _ => panic!("Expected RunDone, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_run_done_failed_variant() {
        let json = r#"{"type":"run_done","termination":"max_iterations","success":false}"#;
        let event: KimEvent = serde_json::from_str(json).expect("run_done failed should deserialize");
        match event {
            KimEvent::RunDone { termination, success } => {
                assert_eq!(termination, "max_iterations");
                assert!(!success);
            }
            _ => panic!("Expected RunDone"),
        }
    }

    #[test]
    fn test_parse_provider_error_event() {
        let json = r#"{"type":"provider_error","code":"rate_limit","retryable":false}"#;
        let event: KimEvent = serde_json::from_str(json).expect("provider_error should deserialize");
        match event {
            KimEvent::ProviderError { code, retryable } => {
                assert_eq!(code, "rate_limit");
                assert!(!retryable);
            }
            _ => panic!("Expected ProviderError, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_provider_error_retryable_variant() {
        let json = r#"{"type":"provider_error","code":"server_error","retryable":true}"#;
        let event: KimEvent = serde_json::from_str(json).expect("provider_error retryable should deserialize");
        match event {
            KimEvent::ProviderError { code, retryable } => {
                assert_eq!(code, "server_error");
                assert!(retryable);
            }
            _ => panic!("Expected ProviderError"),
        }
    }

    #[test]
    fn test_parse_run_done_all_terminations() {
        let terminations = [
            "task_complete", "cancelled", "max_iterations",
            "stuck", "provider_failed", "need_help", "conversational_loop",
        ];
        for term in &terminations {
            let json = format!(r#"{{"type":"run_done","termination":"{}","success":false}}"#, term);
            let event: KimEvent = serde_json::from_str(&json)
                .unwrap_or_else(|e| panic!("run_done with termination={} failed: {}", term, e));
            match event {
                KimEvent::RunDone { termination, .. } => assert_eq!(&termination, term),
                _ => panic!("Expected RunDone for termination={}", term),
            }
        }
    }

    #[test]
    fn test_parse_hitl_approval_request_event() {
        let json = r#"{"type":"hitl_approval_request","tool":"run_command","risk":"high","reason":"arbitrary_code_execution"}"#;
        let event: KimEvent = serde_json::from_str(json).expect("hitl_approval_request should deserialize");
        match event {
            KimEvent::HitlApprovalRequest { tool, risk, reason, preview } => {
                assert_eq!(tool, "run_command");
                assert_eq!(risk, "high");
                assert_eq!(reason, "arbitrary_code_execution");
                assert_eq!(preview, ""); // absent in legacy line → default
            }
            _ => panic!("Expected HitlApprovalRequest, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_hitl_approval_result_approved_event() {
        let json = r#"{"type":"hitl_approval_result","tool":"run_command","approved":true}"#;
        let event: KimEvent = serde_json::from_str(json).expect("hitl_approval_result should deserialize");
        match event {
            KimEvent::HitlApprovalResult { tool, approved } => {
                assert_eq!(tool, "run_command");
                assert!(approved);
            }
            _ => panic!("Expected HitlApprovalResult, got {:?}", event),
        }
    }

    #[test]
    fn test_parse_hitl_approval_result_denied_event() {
        let json = r#"{"type":"hitl_approval_result","tool":"delete_file","approved":false}"#;
        let event: KimEvent = serde_json::from_str(json).expect("hitl_approval_result denied should deserialize");
        match event {
            KimEvent::HitlApprovalResult { tool, approved } => {
                assert_eq!(tool, "delete_file");
                assert!(!approved);
            }
            _ => panic!("Expected HitlApprovalResult, got {:?}", event),
        }
    }

    #[test]
    fn test_find_python_finds_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create a fake venv/bin/python
        let venv_bin = root.join("venv").join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let python_path = venv_bin.join("python");
        fs::write(&python_path, "#!/bin/sh\n").unwrap();

        // Make it "exist" (the function only checks .exists(), not executability)
        let result = find_python_interpreter(root);
        assert!(result.is_ok(), "Expected Ok, got {:?}", result);
        let found = result.unwrap();
        assert!(
            found.contains("venv"),
            "Expected venv path, got: {}",
            found
        );
    }

    #[test]
    fn test_find_python_finds_dot_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create a fake .venv/bin/python (no venv/)
        let venv_bin = root.join(".venv").join("bin");
        fs::create_dir_all(&venv_bin).unwrap();
        let python_path = venv_bin.join("python");
        fs::write(&python_path, "#!/bin/sh\n").unwrap();

        let result = find_python_interpreter(root);
        assert!(result.is_ok());
        let found = result.unwrap();
        assert!(
            found.contains(".venv"),
            "Expected .venv path, got: {}",
            found
        );
    }

    #[test]
    fn test_find_python_prefers_venv_over_dot_venv() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();

        // Create both venv/ and .venv/
        for dir_name in &["venv", ".venv"] {
            let venv_bin = root.join(dir_name).join("bin");
            fs::create_dir_all(&venv_bin).unwrap();
            fs::write(venv_bin.join("python"), "#!/bin/sh\n").unwrap();
        }

        let result = find_python_interpreter(root);
        assert!(result.is_ok());
        let found = result.unwrap();
        // venv/ comes first in the candidate list
        assert!(
            found.contains("/venv/") && !found.contains("/.venv/"),
            "Expected venv/ to be preferred over .venv/, got: {}",
            found
        );
    }

    #[test]
    fn test_find_python_fallback_to_system() {
        let tmp = tempfile::tempdir().expect("failed to create temp dir");
        let root = tmp.path();
        // No venv directories — should fall back to system python3/python

        let result = find_python_interpreter(root);
        // On a system with Python installed this should succeed;
        // on CI without Python it may fail — both are valid.
        // We just verify it doesn't panic.
        match result {
            Ok(cmd) => assert!(!cmd.is_empty()),
            Err(msg) => assert!(msg.contains("No Python interpreter")),
        }
    }

    #[test]
    fn test_is_bundled_orchestrator_positive() {
        assert!(is_bundled_orchestrator("/path/to/kim-orchestrator-aarch64-apple-darwin"));
        assert!(is_bundled_orchestrator("/Applications/Kim.app/Contents/MacOS/kim-orchestrator"));
        assert!(is_bundled_orchestrator("kim-orchestrator"));
    }

    #[test]
    fn test_is_bundled_orchestrator_negative() {
        assert!(!is_bundled_orchestrator("/usr/bin/python3"));
        assert!(!is_bundled_orchestrator("python"));
        assert!(!is_bundled_orchestrator("/path/venv/bin/python"));
    }

    #[test]
    fn test_find_bundled_orchestrator_absent_in_test() {
        // In the test binary context, there is no kim-orchestrator sidecar
        // adjacent to the test executable. find_bundled_orchestrator() must
        // return None rather than panicking.
        let result = find_bundled_orchestrator();
        // Either None (no sidecar found) or Some (if by coincidence a binary
        // named kim-orchestrator exists in the test dir) — both are valid.
        // This test just asserts the function doesn't panic.
        let _ = result;
    }

    #[test]
    fn test_dirs_home_returns_something() {
        // HOME or USERPROFILE should be set in any normal environment.
        let home = dirs_home();
        assert!(home.is_some(), "dirs_home() returned None — HOME not set?");
    }
}

