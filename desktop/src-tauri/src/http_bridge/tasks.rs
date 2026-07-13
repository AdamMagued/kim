//! kimctl task-lifecycle routes: `GET /v1/status`, `POST /v1/task`,
//! `POST /v1/cancel` and `POST /v1/task/approve` (HITL decision forwarding).
//!
//! Extracted verbatim from the monolithic `http_bridge.rs` route match —
//! behavior unchanged.

use std::path::Path;
use std::sync::Mutex as StdMutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::{Emitter, Manager};
use tiny_http::Request;

use crate::http_util::respond_json;
use crate::paths::default_project_root;
use crate::provider_url::browser_restore_status_for_session;
use crate::session_store::validate_session_id;
use crate::subprocess::{find_python_interpreter, process_exists, send_signal};
use crate::{google_oauth, KIM_PREFERRED_SITE, WEBVIEW_BRIDGE_CFG};

use super::read_body_capped;

fn count_run_results(session_dir: &Path, session_id: &str) -> usize {
    let direct = session_dir.join(format!("{session_id}.jsonl"));
    let path = if direct.is_file() {
        Some(direct)
    } else {
        std::fs::read_dir(session_dir).ok().and_then(|entries| {
            entries.flatten().find_map(|entry| {
                let candidate = entry.path().join(format!("{session_id}.jsonl"));
                candidate.is_file().then_some(candidate)
            })
        })
    };
    path.and_then(|p| std::fs::read_to_string(p).ok())
        .map(|text| {
            text.lines()
                .filter(|line| {
                    serde_json::from_str::<serde_json::Value>(line)
                        .ok()
                        .and_then(|value| {
                            value
                                .get("type")
                                .and_then(|v| v.as_str())
                                .map(str::to_owned)
                        })
                        .as_deref()
                        == Some("run_result")
                })
                .count()
        })
        .unwrap_or(0)
}

/// `GET /v1/status` — report task-runtime and browser-window state.
pub(super) fn status(request: Request, app_handle: tauri::AppHandle) {
    let (has_running_task, active_session_id) = tauri::async_runtime::block_on(async {
        let mut rt = crate::task_runtime::task_runtime().lock().await;
        match rt.pid {
            Some(pid) if process_exists(pid) => (true, rt.session_id.clone()),
            Some(_) => {
                rt.clear();
                (false, None)
            }
            None => (false, None),
        }
    });
    let browser_visible = app_handle
        .get_webview_window("kim-browser-signin")
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);
    respond_json(
        request,
        200,
        serde_json::json!({
            "ok": true,
            "has_running_task": has_running_task,
            "active_session_id": active_session_id,
            "browser_visible": browser_visible,
        }),
    );
}

/// `POST /v1/task` — spawn the orchestrator for a kimctl task.
pub(super) fn start_task(mut request: Request, app_handle: tauri::AppHandle) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    #[derive(Deserialize)]
    struct TaskRequest {
        task: String,
        #[serde(default)]
        session_id: Option<String>,
        #[serde(default)]
        provider: Option<String>,
        /// HITL permission gate → KIM_HITL_RISK_THRESHOLD (#15/#37).
        /// Canonical values match the GUI/subprocess.rs vocabulary:
        ///   "full_auto"  → no gate
        ///   "ask_risky"  → high-risk tools need approval
        ///   "ask_always" → medium+ tools need approval
        /// Legacy aliases also accepted: "auto"=full_auto,
        /// "confirm-sensitive"=ask_risky, "confirm-all"=ask_always.
        /// Anything unrecognized defaults to full_auto.
        #[serde(default)]
        permission_mode: Option<String>,
    }

    let parsed: TaskRequest = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => {
            respond_json(
                request,
                400,
                serde_json::json!({"ok": false, "error": format!("Invalid JSON: {}", e)}),
            );
            return;
        }
    };

    if parsed.task.trim().is_empty() {
        respond_json(
            request,
            400,
            serde_json::json!({"ok": false, "error": "Task cannot be empty."}),
        );
        return;
    }

    // Reject if a task is already running or starting (K2: the
    // stale-pid recovery + reservation is shared with the GUI path
    // via spawn_supervisor::reserve_slot).
    if let Err(error) = tauri::async_runtime::block_on(crate::spawn_supervisor::reserve_slot()) {
        let msg = if error.contains("starting") {
            "A task is already starting. Try again in a moment."
        } else {
            "A task is already running. Cancel it first."
        };
        respond_json(request, 409, serde_json::json!({"ok": false, "error": msg}));
        return;
    }

    let kim_root = default_project_root();
    let python = match find_python_interpreter(&kim_root) {
        Ok(p) => p,
        Err(e) => {
            tauri::async_runtime::block_on(crate::spawn_supervisor::release_slot());
            respond_json(request, 500, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };

    // M-BRIDGE-4: a client session_id becomes a file stem and flows
    // into --session-id/--resume argv — validate it like every other
    // route does instead of pushing the traversal to the Python side.
    if let Some(sid) = parsed
        .session_id
        .as_deref()
        .filter(|s| !s.trim().is_empty())
    {
        if let Err(e) = validate_session_id(sid) {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            tauri::async_runtime::block_on(crate::spawn_supervisor::release_slot());
            return;
        }
    }
    let session_id = parsed
        .session_id
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| {
            // M-BRIDGE-4: full millisecond timestamp + 32 random bits —
            // the old (ts&0xFFFF, counter&0xFFFF) fallback was 8 hex
            // chars and collision-prone across restarts.
            use rand::Rng as _;
            let ts = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            format!("{:x}-{:08x}", ts, rand::thread_rng().gen::<u32>())
        });
    let session_dir = kim_root.join("kim_sessions");
    let prior_run_results = count_run_results(&session_dir, &session_id);
    let provider = parsed
        .provider
        .filter(|s| !s.trim().is_empty() && s != "desktop")
        .unwrap_or_else(|| "browser".to_string());

    // Caller-resolved env extras (K2: the argv/env assembly itself is
    // the same task_spec::chat_task_spec builder the GUI path uses, so
    // the two spawn paths can no longer diverge on env/HITL/stdin).
    let mut extra = crate::task_spec::EnvBuilder::new();
    if let Ok(guard) = KIM_PREFERRED_SITE
        .get_or_init(|| StdMutex::new(None))
        .lock()
    {
        if let Some(site) = &*guard {
            extra = extra.set("KIM_PREFERRED_SITE", site);
        }
    }
    if let Some(cfg) = WEBVIEW_BRIDGE_CFG.get() {
        extra = extra.webview_bridge(Some((cfg.base_url.as_str(), cfg.token.as_str())));
    }
    if crate::task_spec::is_browser_provider(&provider) {
        extra = extra.set(
            "KIM_BROWSER_RESTORE_STATUS",
            browser_restore_status_for_session(&session_dir, Some(&session_id), &provider),
        );
    }
    if provider.trim().eq_ignore_ascii_case("gemini") {
        let google_env = match tauri::async_runtime::block_on(
            google_oauth::google_oauth_env_for_agent(),
        ) {
            Ok(value) => value,
            Err(err) => {
                tauri::async_runtime::block_on(crate::spawn_supervisor::release_slot());
                respond_json(
                    request,
                    400,
                    serde_json::json!({
                        "ok": false,
                        "error": format!(
                            "Google for Kim is not connected. Open Settings → Account → Google for Kim (API), then Continue with Google. {}",
                            err
                        ),
                    }),
                );
                return;
            }
        };
        extra = extra.extend(google_env.as_env_pairs());
    }

    let spec = crate::task_spec::chat_task_spec(crate::task_spec::ChatSpecParams {
        bundled_sidecar: crate::subprocess::is_bundled_orchestrator(&python),
        python: &python,
        kim_root: &kim_root,
        project_root: &kim_root,
        session_dir: &session_dir,
        task: &parsed.task,
        provider: &provider,
        session_id: session_id.clone(),
        resume: Some(session_id.clone()),
        permission_mode: parsed.permission_mode.as_deref(),
        source: crate::task_spec::SpawnSource::Bridge,
        extra_envs: extra.build(),
    });

    match tauri::async_runtime::block_on(crate::spawn_supervisor::spawn(spec)) {
        Ok(sup) => {
            // Emit event so the desktop UI knows a task started.
            let _ = app_handle.emit(
                "kim-agent-started",
                serde_json::json!({
                    "session_id": session_id,
                    "source": "kimctl",
                }),
            );

            // Detached supervision: pump stdout through the same typed
            // translator the GUI path uses (#33), wait for exit, then
            // restore the UI. Runs on the Tauri async runtime; the
            // bridge thread answers the HTTP request immediately.
            let ipc_typed = app_handle.state::<crate::config::AppConfig>().ipc_protocol == "typed";
            let app_for_wait = app_handle.clone();
            tauri::async_runtime::spawn(async move {
                let status =
                    crate::spawn_supervisor::supervise(sup, app_for_wait.clone(), ipc_typed).await;
                let success = status.map(|s| s.success()).unwrap_or(false);
                if let Some(cancel_win) = app_for_wait.get_webview_window("cancel-widget") {
                    let _ = cancel_win.close();
                }
                if let Some(flash_win) = app_for_wait.get_webview_window("screenshot-flash") {
                    let _ = flash_win.close();
                }
                if let Some(main_win) = app_for_wait.get_webview_window("main") {
                    let _ = main_win.show();
                    let _ = main_win.set_focus();
                }
                let _ = app_for_wait.emit("kim-agent-done", success);
            });

            respond_json(
                request,
                200,
                serde_json::json!({
                    "ok": true,
                    "session_id": session_id,
                    "sessions_dir": session_dir.to_string_lossy(),
                    "prior_run_results": prior_run_results,
                }),
            );
        }
        Err(e) => {
            // spawn() released the reservation already.
            respond_json(
                request,
                500,
                serde_json::json!({
                    "ok": false,
                    "error": format!("Failed to start agent: {}", e),
                }),
            );
        }
    }
}

/// `POST /v1/cancel` — signal the running task and reap stale pids.
pub(super) fn cancel(request: Request, app_handle: tauri::AppHandle) {
    let pid = tauri::async_runtime::block_on(async {
        let rt = crate::task_runtime::task_runtime().lock().await;
        rt.pid
    });

    match pid {
        Some(pid) if process_exists(pid) => {
            match send_signal(pid, false) {
                Ok(()) => {
                    if let Some(cancel_win) = app_handle.get_webview_window("cancel-widget") {
                        let _ = cancel_win.close();
                    }
                    if let Some(main_win) = app_handle.get_webview_window("main") {
                        let _ = main_win.show();
                        let _ = main_win.set_focus();
                    }
                    if let Some(flash_win) = app_handle.get_webview_window("screenshot-flash") {
                        let _ = flash_win.close();
                    }
                    // Background cleanup: wait 2s then force-kill if alive
                    std::thread::spawn(move || {
                        for _ in 0..20 {
                            std::thread::sleep(Duration::from_millis(100));
                            if !process_exists(pid) {
                                break;
                            }
                        }
                        // M-PROC-3: only escalate to SIGKILL if the
                        // runtime still tracks THIS pid — after exit +
                        // pid reuse a blind group-kill would hit an
                        // unrelated process tree.
                        tauri::async_runtime::block_on(async {
                            let mut rt = crate::task_runtime::task_runtime().lock().await;
                            if rt.pid == Some(pid) {
                                if process_exists(pid) {
                                    let _ = send_signal(pid, true);
                                }
                                // #24: pid-guarded — never wipe a successor task.
                                rt.clear_if_pid(pid);
                            }
                        });
                    });
                    respond_json(
                        request,
                        200,
                        serde_json::json!({"ok": true, "message": "Cancelling task."}),
                    );
                }
                Err(e) => {
                    respond_json(
                        request,
                        500,
                        serde_json::json!({
                            "ok": false,
                            "error": format!("Failed to send stop signal: {}", e),
                        }),
                    );
                }
            }
        }
        _ => {
            // #24: only reap a stale DEAD pid here. The old
            // unconditional clear() also wiped the `starting` flag,
            // letting a second task reserve while one was mid-spawn.
            tauri::async_runtime::block_on(async {
                let mut rt = crate::task_runtime::task_runtime().lock().await;
                if let Some(stale) = rt.pid {
                    if !process_exists(stale) {
                        rt.clear();
                    }
                }
            });
            respond_json(
                request,
                200,
                serde_json::json!({
                    "ok": true,
                    "message": "No task is currently running.",
                }),
            );
        }
    }
}

// HITL approval forwarding (#15): kimctl sends the user's yes/no decision
// here; we write a JSON line to the orchestrator's stdin so the Python
// StdinApprovalBridge receives it and unblocks the agent loop.
pub(super) fn approve(mut request: Request) {
    let body = match read_body_capped(&mut request) {
        Ok(b) => b,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };
    let payload = match crate::hitl::approve_line_from_body(&body) {
        Ok(p) => p,
        Err(e) => {
            respond_json(request, 400, serde_json::json!({"ok": false, "error": e}));
            return;
        }
    };
    // H-PROC-2: write via the shared stdin handle — never holds the
    // global runtime lock across the stdin await.
    let result = tauri::async_runtime::block_on(crate::task_runtime::write_stdin_line(&payload));
    match result {
        Ok(()) => respond_json(request, 200, serde_json::json!({"ok": true})),
        Err(e) => respond_json(request, 409, serde_json::json!({"ok": false, "error": e})),
    }
}

// ---------------------------------------------------------------------------
// Unit tests for the /v1/task argv builder logic
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::count_run_results;

    #[test]
    fn counts_only_completed_runs_for_reused_session() {
        let dir = tempfile::tempdir().unwrap();
        let session_id = "session-reused";
        let path = dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(
            path,
            concat!(
                "{\"type\":\"run_started\"}\n",
                "{\"type\":\"run_result\",\"summary\":\"one\"}\n",
                "not json\n",
                "{\"type\":\"run_result\",\"summary\":\"two\"}\n",
            ),
        )
        .unwrap();

        assert_eq!(count_run_results(dir.path(), session_id), 2);
    }

    /// K2: build the REAL argv the `/v1/task` handler produces, by calling the
    /// shared `task_spec::chat_task_spec` builder exactly like the handler
    /// does (no more hand-mirrored logic that could drift).
    fn build_task_argv(interpreter: &str, task: &str, session_dir: &str) -> Vec<String> {
        use crate::task_spec::{chat_task_spec, ChatSpecParams, SpawnSource};
        use std::path::Path;
        chat_task_spec(ChatSpecParams {
            python: interpreter,
            bundled_sidecar: crate::subprocess::is_bundled_orchestrator(interpreter),
            kim_root: Path::new("/kim"),
            project_root: Path::new("/kim"),
            session_dir: Path::new(session_dir),
            task,
            provider: "browser",
            session_id: "test".to_string(),
            resume: None,
            permission_mode: None,
            source: SpawnSource::Bridge,
            extra_envs: vec![],
        })
        .args
    }

    /// Regression: a bundled sidecar interpreter must NOT receive `-m orchestrator.agent`
    /// because it is a standalone executable, not a Python binary.
    /// Previously, every kimctl /v1/task spawn passed the flag unconditionally and
    /// caused an immediate "No module named orchestrator" failure.
    #[test]
    fn bundled_omits_module_flag() {
        let bundled_paths = [
            "kim-orchestrator",
            "kim-orchestrator-aarch64-apple-darwin",
            "/Applications/Kim.app/Contents/MacOS/kim-orchestrator-aarch64-apple-darwin",
            "/usr/local/lib/kim/kim-orchestrator-x86_64-unknown-linux-gnu",
        ];

        for interpreter in bundled_paths {
            let argv = build_task_argv(interpreter, "do something", "/tmp/sessions");
            assert!(
                !argv.contains(&"-m".to_string()),
                "bundled interpreter {:?} must NOT receive '-m' flag, got: {:?}",
                interpreter,
                argv,
            );
            assert!(
                !argv.contains(&"orchestrator.agent".to_string()),
                "bundled interpreter {:?} must NOT receive 'orchestrator.agent', got: {:?}",
                interpreter,
                argv,
            );
        }
    }

    /// Regression: a plain Python interpreter MUST receive `-m orchestrator.agent`
    /// so that the orchestrator package is located on PYTHONPATH.
    /// Without this flag every kimctl task silently failed to import the module.
    #[test]
    fn python_includes_module_flag() {
        let python_paths = [
            "python3",
            "python",
            "/usr/bin/python3",
            "/home/user/.kim/venv/bin/python",
            "/path/venv/bin/python3.11",
        ];

        for interpreter in python_paths {
            let argv = build_task_argv(interpreter, "do something", "/tmp/sessions");
            let m_pos = argv.iter().position(|a| a == "-m");
            let module_pos = argv.iter().position(|a| a == "orchestrator.agent");

            assert!(
                m_pos.is_some(),
                "python interpreter {:?} must receive '-m' flag, got: {:?}",
                interpreter,
                argv,
            );
            assert!(
                module_pos.is_some(),
                "python interpreter {:?} must receive 'orchestrator.agent', got: {:?}",
                interpreter,
                argv,
            );
            // The two flags must be adjacent: `-m orchestrator.agent`
            assert_eq!(
                m_pos.unwrap() + 1,
                module_pos.unwrap(),
                "'-m' must immediately precede 'orchestrator.agent' for {:?}",
                interpreter,
            );
        }
    }

    /// Regression: regardless of interpreter type, the built argv must carry
    /// `--task <task>` and `--session-dir <dir>` so the orchestrator receives
    /// the user's request and stores its state in the right location.
    #[test]
    fn task_and_session_args_present() {
        let cases = [
            // (interpreter, expected_has_module_flag)
            ("kim-orchestrator-aarch64-apple-darwin", false),
            ("/usr/bin/python3", true),
        ];
        let task_value = "write a hello world script";
        let session_dir_value = "/home/user/.kim/sessions";

        for (interpreter, _) in cases {
            let argv = build_task_argv(interpreter, task_value, session_dir_value);

            let task_pos = argv.iter().position(|a| a == "--task");
            assert!(
                task_pos.is_some(),
                "interpreter {:?}: '--task' missing from argv {:?}",
                interpreter,
                argv,
            );
            assert_eq!(
                argv.get(task_pos.unwrap() + 1).map(String::as_str),
                Some(task_value),
                "interpreter {:?}: value after '--task' must be the task string",
                interpreter,
            );

            let sdir_pos = argv.iter().position(|a| a == "--session-dir");
            assert!(
                sdir_pos.is_some(),
                "interpreter {:?}: '--session-dir' missing from argv {:?}",
                interpreter,
                argv,
            );
            assert_eq!(
                argv.get(sdir_pos.unwrap() + 1).map(String::as_str),
                Some(session_dir_value),
                "interpreter {:?}: value after '--session-dir' must be the session dir",
                interpreter,
            );
        }
    }
}
