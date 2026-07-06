//! K2 — `SpawnSupervisor`: the *effectful* half of the task spawn path.
//!
//! Consumes a pure [`crate::task_spec::TaskSpec`] and owns the full process
//! lifecycle against the single [`crate::task_runtime::TaskRuntime`] slot:
//!
//! ```text
//! reserve_slot() → spawn(spec) → supervise(child) → clear_if_pid
//! ```
//!
//! Both spawn paths use it (A1/A3):
//! - the GUI `send_task` command awaits `supervise` inline;
//! - the HTTP bridge `/v1/task` wraps `supervise` in
//!   `tauri::async_runtime::spawn` and returns immediately.
//!
//! Every child is a tokio child with piped stdout/stderr; stdout lines go
//! through `subprocess::forward_agent_stdout_line` (the one IPC translator),
//! stderr lines are emitted on `kim-agent-error`. The stdin handle (when the
//! spec pipes it) is stored in `TaskRuntime.stdin` for HITL/steering.

use crate::task_runtime::task_runtime;
use crate::task_spec::{StdinMode, TaskSpec};
use tauri::Emitter;

/// Reserve the single-runner slot, first recovering from a stale dead pid
/// (#25: a missed clear after a wait() error must not block every future
/// task until app restart). Shared verbatim by both spawn paths.
pub(crate) async fn reserve_slot() -> Result<(), String> {
    let mut rt = task_runtime().lock().await;
    if let Some(pid) = rt.pid {
        if !crate::subprocess::process_exists(pid) {
            rt.clear();
        }
    }
    rt.reserve()
}

/// Release a reservation that never reached spawn.
pub(crate) async fn release_slot() {
    task_runtime().lock().await.release();
}

/// A spawned, registered child ready to be supervised.
pub(crate) struct SupervisedChild {
    pub pid: u32,
    child: tokio::process::Child,
    is_codex: bool,
}

/// Spawn the spec's child process and register it in the `TaskRuntime`.
///
/// The caller must hold a reservation (`reserve_slot`). On any failure the
/// reservation is released before returning, so callers never leak the slot.
pub(crate) async fn spawn(spec: TaskSpec) -> Result<SupervisedChild, String> {
    use std::process::Stdio;

    let mut cmd = tokio::process::Command::new(&spec.program);
    cmd.args(&spec.args);
    if let Some(cwd) = &spec.cwd {
        cmd.current_dir(cwd);
    }
    for (key, value) in &spec.envs {
        cmd.env(key, value);
    }
    cmd.stdin(match spec.stdin {
        StdinMode::Piped => Stdio::piped(),
        StdinMode::Null => Stdio::null(),
    })
    .stdout(Stdio::piped())
    .stderr(Stdio::piped());

    // Own process group so cancellation (kill -TERM -<pid>) reaps the whole
    // tree (MCP server, browser/Playwright helpers), not just the parent.
    #[cfg(unix)]
    {
        cmd.process_group(0);
    }

    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => {
            release_slot().await;
            return Err(format!("Failed to start Kim: {}", e));
        }
    };

    let Some(pid) = child.id() else {
        release_slot().await;
        return Err("Failed to read child PID after spawn.".to_string());
    };

    {
        let mut rt = task_runtime().lock().await;
        rt.store_pid(pid, spec.session_id.clone(), spec.source);
        if spec.stdin == StdinMode::Piped {
            // H-PROC-2: stdin lives behind its own Arc<Mutex> so HITL/steer
            // writers never hold the global runtime lock across an await.
            rt.stdin = child
                .stdin
                .take()
                .map(|s| std::sync::Arc::new(tokio::sync::Mutex::new(s)));
        }
    }

    Ok(SupervisedChild {
        pid,
        child,
        is_codex: spec.is_codex,
    })
}

/// Pump stdout/stderr to the UI, wait for exit, then clear the runtime slot
/// (pid-guarded, #24: a late clear must never wipe a successor task).
///
/// Returns the exit status; IO/wait errors are stringified AFTER cleanup so
/// an error can never strand a stale pid (#25).
pub(crate) async fn supervise(
    mut sup: SupervisedChild,
    app: tauri::AppHandle,
    ipc_typed: bool,
) -> Result<std::process::ExitStatus, String> {
    use tokio::io::AsyncBufReadExt;

    let is_codex = sup.is_codex;
    let stdout_handle = sup.child.stdout.take().map(|stdout| {
        let app = app.clone();
        tokio::spawn(async move {
            let mut lines = tokio::io::BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                crate::subprocess::forward_agent_stdout_line(&app, ipc_typed, is_codex, &line);
            }
        })
    });
    let stderr_handle = sup.child.stderr.take().map(|stderr| {
        let app = app.clone();
        tokio::spawn(async move {
            let mut lines = tokio::io::BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let _ = app.emit("kim-agent-error", line);
            }
        })
    });

    let wait_result = sup.child.wait().await;

    // L-PROC-10: surface pump-task panics instead of silently dropping them —
    // a dead pump means UI output stopped mid-run and we want that in the log.
    if let Some(handle) = stdout_handle {
        if let Err(e) = handle.await {
            eprintln!("[Kim] stdout pump task failed: {e}");
        }
    }
    if let Some(handle) = stderr_handle {
        if let Err(e) = handle.await {
            eprintln!("[Kim] stderr pump task failed: {e}");
        }
    }

    task_runtime().lock().await.clear_if_pid(sup.pid);

    wait_result.map_err(|e| e.to_string())
}
