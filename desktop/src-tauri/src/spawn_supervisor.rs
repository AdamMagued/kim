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
    use crate::subprocess::{CappedLineSplitter, MAX_STDOUT_LINE_BYTES};
    use tokio::io::AsyncReadExt;

    let is_codex = sup.is_codex;
    // F-D-5: read stdout in fixed 64 KiB chunks and split lines through a
    // bounded accumulator instead of BufReader::lines(), whose next_line()
    // grows an unbounded String for a no-newline / multi-GB-line child. Peak
    // per-line memory is now capped at MAX_STDOUT_LINE_BYTES.
    let stdout_handle = sup.child.stdout.take().map(|mut stdout| {
        let app = app.clone();
        tokio::spawn(async move {
            let mut splitter = CappedLineSplitter::new(MAX_STDOUT_LINE_BYTES);
            let mut chunk = vec![0u8; 64 * 1024];
            loop {
                match stdout.read(&mut chunk).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => splitter.push(&chunk[..n], |line| {
                        crate::subprocess::forward_agent_stdout_line(
                            &app, ipc_typed, is_codex, &line,
                        );
                    }),
                }
            }
            splitter.finish(|line| {
                crate::subprocess::forward_agent_stdout_line(&app, ipc_typed, is_codex, &line);
            });
        })
    });
    let stderr_handle = sup.child.stderr.take().map(|mut stderr| {
        let app = app.clone();
        tokio::spawn(async move {
            // Same bounded splitter for stderr (also an untrusted pump).
            let mut splitter = CappedLineSplitter::new(MAX_STDOUT_LINE_BYTES);
            let mut chunk = vec![0u8; 64 * 1024];
            loop {
                match stderr.read(&mut chunk).await {
                    Ok(0) | Err(_) => break,
                    Ok(n) => splitter.push(&chunk[..n], |line| {
                        let _ = app.emit("kim-agent-error", line);
                    }),
                }
            }
            splitter.finish(|line| {
                let _ = app.emit("kim-agent-error", line);
            });
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

// AUDIT FIX #6: send_task now reserves the slot via `reserve_slot()` as the
// very first thing it does (before any async I/O, including the Google
// OAuth refresh build_gui_chat_spec/build_gui_codex_spec can trigger),
// closing the gap where two near-simultaneous callers could both pass an
// earlier "is it occupied?" check and both run that expensive work before
// only one of them was ultimately allowed to spawn. These tests exercise
// the primitive that guarantee rests on: `reserve_slot()`'s check-and-set
// happens atomically under a single lock acquisition, so there is no window
// for a second concurrent caller to observe "free" after the first caller
// has already claimed the slot.
#[cfg(test)]
mod tests {
    use super::*;

    // These are the only tests in the crate that exercise the GLOBAL
    // `task_runtime()` singleton (every other test builds a local
    // `TaskRuntime`/`TokioMutex` instance to avoid cross-test interference).
    // cargo test runs tests in parallel by default, and both tests below
    // clear/reserve/release the SAME global singleton, so without
    // serialization they race each other (observed flaky in practice: one
    // test's `clear()` can land between the other's reserve and assert).
    // A module-local async lock keeps them from interleaving without
    // affecting any other test's parallelism.
    static TEST_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    #[tokio::test]
    async fn concurrent_reserve_slot_calls_yield_exactly_one_winner() {
        let _guard = TEST_LOCK.lock().await;
        task_runtime().lock().await.clear();

        // tokio::join! polls both futures concurrently on this task; each
        // reserve_slot() call contends on the same runtime lock, so this
        // reproduces the "two near-simultaneous send_task calls" race the
        // fix closes. Exactly one must observe the slot as free.
        let (a, b) = tokio::join!(reserve_slot(), reserve_slot());
        let ok_count = [a.is_ok(), b.is_ok()].into_iter().filter(|ok| *ok).count();
        assert_eq!(
            ok_count, 1,
            "exactly one of two concurrent reserve_slot() calls must win (a={a:?}, b={b:?})"
        );

        task_runtime().lock().await.clear();
    }

    #[tokio::test]
    async fn release_slot_frees_the_reservation_for_a_later_caller() {
        let _guard = TEST_LOCK.lock().await;
        task_runtime().lock().await.clear();

        reserve_slot().await.expect("first reserve must succeed");
        assert!(
            reserve_slot().await.is_err(),
            "slot must be held while reserved"
        );

        release_slot().await;
        assert!(
            reserve_slot().await.is_ok(),
            "slot must be reservable again once released"
        );

        task_runtime().lock().await.clear();
    }
}
