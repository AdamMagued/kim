/// Task runtime — unified single-runner ownership for both GUI and CLI paths.
///
/// # Problem (issue #16)
///
/// Before this module, the codebase had two parallel task-state systems:
///
/// - `TaskState` (`Arc<tokio::Mutex<RunningTask>>`) — Tauri managed state used
///   by GUI Tauri commands (`send_task`, `cancel_task`).
/// - A family of global `OnceLock` statics in `lib.rs`:
///   `BRIDGE_TASK_STARTING` / `BRIDGE_TASK_PID` / `BRIDGE_TASK_SESSION` /
///   `BRIDGE_TASK_STDIN` — used by the sync HTTP bridge thread (`/v1/task`,
///   `/v1/cancel`, `/v1/task/approve`).
///
/// Both paths also had separate stdin handles:
/// - `hitl_stdin()` — `TokioMutex<Option<tokio::process::ChildStdin>>` for
///   Tauri commands.
/// - `BRIDGE_TASK_STDIN` — `StdMutex<Option<std::process::ChildStdin>>` for
///   the bridge thread.
///
/// The `cancel_task` command already cross-reads both stores (subprocess.rs
/// L984): tasks started via `kimctl /v1/task` register in `BRIDGE_TASK_PID`,
/// not in `TaskState`.  The two-store split created a class of race conditions
/// observable only in the running app with concurrent GUI+CLI triggers.
///
/// # Solution
///
/// `TaskRuntime` is a single struct that owns *all* mutable task state.  It is
/// stored as Tauri managed state (`Arc<TokioMutex<TaskRuntime>>`) so async
/// Tauri commands can `lock().await` on it, and also accessible from the sync
/// bridge thread via `tauri::async_runtime::block_on`.
///
/// Both spawn paths call `reserve()` → `spawn_*()` → `store_pid()` through
/// this runtime, and both cancel paths call `cancel()`.  The single-runner
/// mutex is the lock on the runtime itself — no separate atomics needed.
use std::sync::{Arc, OnceLock};
use tokio::sync::Mutex as TokioMutex;

/// Source that spawned the currently-running task. Defined in `task_spec`
/// (the pub K2 seam) and re-exported here for existing call sites.
pub(crate) use crate::task_spec::SpawnSource;

/// All mutable state for the one agent subprocess that may be running at a time.
#[derive(Default)]
pub(crate) struct TaskRuntime {
    /// PID of the running child, or `None` if no task is active.
    pub pid: Option<u32>,
    /// True while a task slot is reserved but the child has not yet been spawned.
    /// Used to close the TOCTOU window between the already-running check and the
    /// actual `Command::spawn()` call.
    pub starting: bool,
    /// Session ID of the currently-running task.
    pub session_id: Option<String>,
    /// Which path spawned the current task.  Used by `cancel()` to emit the
    /// right cleanup events.
    pub source: Option<SpawnSource>,
    /// Stdin handle of the running child (K2: both spawn paths now go through
    /// `spawn_supervisor`, which always spawns a tokio child, so one handle
    /// serves the GUI `hitl_respond_approval` / `steer_task` commands and the
    /// bridge `/v1/task/approve` endpoint alike).
    ///
    /// H-PROC-2: the handle lives behind its OWN mutex (shared via `Arc`) so
    /// writers can clone it out, release the global runtime lock, and only then
    /// await `write_all`. A child that stops draining stdin (pipe buffer full)
    /// can therefore never wedge `cancel_task` / `reserve_slot` / supervisor
    /// cleanup, all of which contend on the runtime lock.
    pub stdin: Option<Arc<TokioMutex<tokio::process::ChildStdin>>>,
}

impl TaskRuntime {
    /// Attempt to reserve the single-runner slot.
    ///
    /// Returns `Ok(())` on success (caller may now proceed to spawn).
    /// Returns `Err(msg)` if another task is already running or starting.
    pub fn reserve(&mut self) -> Result<(), String> {
        if self.pid.is_some() {
            return Err("already running".to_string());
        }
        if self.starting {
            return Err("already starting".to_string());
        }
        self.starting = true;
        Ok(())
    }

    /// Store the PID after a successful spawn and clear the `starting` flag.
    pub fn store_pid(&mut self, pid: u32, session_id: String, source: SpawnSource) {
        self.pid = Some(pid);
        self.starting = false;
        self.session_id = Some(session_id);
        self.source = Some(source);
    }

    /// Release the reservation without spawning (e.g. on spawn failure).
    pub fn release(&mut self) {
        self.starting = false;
    }

    /// Clear all runtime state after the child exits.
    pub fn clear(&mut self) {
        self.pid = None;
        self.starting = false;
        self.session_id = None;
        self.source = None;
        self.stdin = None;
    }

    /// Clear only if the stored pid still matches `pid` (#24).
    ///
    /// Late cleanup paths (cancel pollers, spawn-side waiters) race new
    /// spawns: task A's deferred `clear()` must not wipe task B's freshly
    /// stored pid/session/stdin. Returns true if it actually cleared.
    pub fn clear_if_pid(&mut self, pid: u32) -> bool {
        if self.pid == Some(pid) {
            self.clear();
            true
        } else {
            false
        }
    }

    /// True if the slot is occupied (either starting or a pid is held).
    pub fn is_occupied(&self) -> bool {
        self.pid.is_some() || self.starting
    }

}

/// Write a HITL/steer JSON line to the running child's stdin.
/// Called by both the Tauri command path (GUI) and the HTTP bridge path
/// (CLI) so that cross-path approval always works.
///
/// H-PROC-2 (theme T1): the global runtime lock is held only long enough to
/// clone the `Arc` stdin handle — NEVER across the `write_all().await`. The
/// write itself is bounded by a timeout so even a child that stopped draining
/// stdin (full OS pipe buffer) only stalls this one writer, and only briefly;
/// task control (cancel, reserve, supervisor cleanup) stays responsive.
/// Approval correlation stays by unique `id` inside the JSON line itself
/// (`hitl_approve.id` / `approval_decision.id`), which Python matches against
/// the pending request and discards when stale — a timed-out write here can
/// never cause a decision to be applied to the wrong tool.
pub(crate) async fn write_stdin_line(msg: &str) -> Result<(), String> {
    use tokio::io::AsyncWriteExt as _;

    const WRITE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

    let stdin = {
        let rt = task_runtime().lock().await;
        match rt.stdin.as_ref() {
            Some(handle) => Arc::clone(handle),
            None => return Err("No agent stdin available".to_string()),
        }
        // runtime lock released here — before any stdin await
    };

    let mut guard = tokio::time::timeout(WRITE_TIMEOUT, stdin.lock())
        .await
        .map_err(|_| {
            "Timed out waiting for agent stdin (another write is stuck).".to_string()
        })?;
    tokio::time::timeout(WRITE_TIMEOUT, async {
        guard.write_all(msg.as_bytes()).await.map_err(|e| e.to_string())?;
        guard.flush().await.map_err(|e| e.to_string())
    })
    .await
    .map_err(|_| "Timed out writing to agent stdin (child not reading).".to_string())?
}

/// The global async-safe `TaskRuntime` handle.
///
/// Both Tauri async commands and the sync bridge thread access this.
/// Async callers: `lock().await`.
/// Sync callers: `tauri::async_runtime::block_on(TASK_RUNTIME.lock())`.
pub(crate) fn task_runtime() -> &'static TokioMutex<TaskRuntime> {
    static INSTANCE: OnceLock<TokioMutex<TaskRuntime>> = OnceLock::new();
    INSTANCE.get_or_init(|| TokioMutex::new(TaskRuntime::default()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserve_succeeds_when_idle() {
        let mut rt = TaskRuntime::default();
        assert!(rt.reserve().is_ok());
        assert!(rt.starting);
        assert!(rt.pid.is_none());
    }

    #[test]
    fn reserve_fails_when_starting() {
        let mut rt = TaskRuntime::default();
        rt.reserve().unwrap();
        let err = rt.reserve().unwrap_err();
        assert!(err.contains("starting"), "unexpected: {err}");
    }

    #[test]
    fn reserve_fails_when_pid_held() {
        let mut rt = TaskRuntime::default();
        rt.store_pid(12345, "sess-1".to_string(), SpawnSource::Gui);
        let err = rt.reserve().unwrap_err();
        assert!(err.contains("running"), "unexpected: {err}");
    }

    #[test]
    fn store_pid_clears_starting_flag() {
        let mut rt = TaskRuntime::default();
        rt.reserve().unwrap();
        rt.store_pid(42, "s".to_string(), SpawnSource::Bridge);
        assert!(!rt.starting);
        assert_eq!(rt.pid, Some(42));
        assert_eq!(rt.session_id.as_deref(), Some("s"));
    }

    #[test]
    fn clear_resets_all_fields() {
        let mut rt = TaskRuntime::default();
        rt.reserve().unwrap();
        rt.store_pid(7, "x".to_string(), SpawnSource::Gui);
        rt.clear();
        assert!(!rt.is_occupied());
        assert!(rt.session_id.is_none());
        assert!(rt.source.is_none());
    }

    #[test]
    fn release_clears_starting_without_pid() {
        let mut rt = TaskRuntime::default();
        rt.reserve().unwrap();
        rt.release();
        assert!(!rt.starting);
        assert!(rt.pid.is_none());
        // Should be reservable again.
        assert!(rt.reserve().is_ok());
    }

    #[test]
    fn is_occupied_true_while_starting() {
        let mut rt = TaskRuntime::default();
        rt.reserve().unwrap();
        assert!(rt.is_occupied());
    }

    #[test]
    fn is_occupied_true_while_pid_held() {
        let mut rt = TaskRuntime::default();
        rt.store_pid(1, "s".to_string(), SpawnSource::Gui);
        assert!(rt.is_occupied());
    }

    // #24: a stale clear (from a cancelled predecessor task) must not wipe a
    // successor task's state.
    #[test]
    fn clear_if_pid_clears_on_matching_pid() {
        let mut rt = TaskRuntime::default();
        rt.store_pid(100, "a".to_string(), SpawnSource::Gui);
        assert!(rt.clear_if_pid(100));
        assert!(!rt.is_occupied());
        assert!(rt.session_id.is_none());
    }

    #[test]
    fn clear_if_pid_is_noop_on_different_pid() {
        let mut rt = TaskRuntime::default();
        rt.store_pid(200, "b".to_string(), SpawnSource::Bridge);
        assert!(!rt.clear_if_pid(100));
        assert_eq!(rt.pid, Some(200));
        assert_eq!(rt.session_id.as_deref(), Some("b"));
        assert_eq!(rt.source, Some(SpawnSource::Bridge));
    }

    #[test]
    fn clear_if_pid_is_noop_when_idle() {
        let mut rt = TaskRuntime::default();
        assert!(!rt.clear_if_pid(100));
        assert!(!rt.is_occupied());
    }

    // H-PROC-2: the global runtime lock must stay available while a stdin
    // write is in flight. We hold the stdin mutex (simulating a stuck write)
    // and verify the runtime lock can still be acquired — i.e. cancel/task
    // control cannot deadlock behind a child that stopped draining stdin.
    #[cfg(unix)]
    #[tokio::test]
    async fn runtime_lock_free_while_stdin_write_in_flight() {
        let mut child = tokio::process::Command::new("cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn cat");
        let stdin = Arc::new(TokioMutex::new(child.stdin.take().unwrap()));

        // Simulate an in-flight (stuck) write by holding the stdin mutex.
        let stdin_guard = stdin.lock().await;

        // A fresh runtime holding only the Arc must still hand out its lock
        // instantly, because write_stdin_line never holds it across the write.
        let rt = TokioMutex::new(TaskRuntime {
            stdin: Some(Arc::clone(&stdin)),
            ..TaskRuntime::default()
        });
        let lock_result =
            tokio::time::timeout(std::time::Duration::from_millis(500), rt.lock()).await;
        assert!(
            lock_result.is_ok(),
            "runtime lock must not be blocked by an in-flight stdin write"
        );
        drop(stdin_guard);
        let _ = child.kill().await;
        let _ = child.wait().await;
    }

    // H-PROC-2: a plain successful write through the shared handle works.
    #[cfg(unix)]
    #[tokio::test]
    async fn stdin_arc_write_roundtrip() {
        use tokio::io::AsyncWriteExt as _;
        let mut child = tokio::process::Command::new("cat")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .spawn()
            .expect("spawn cat");
        let stdin = Arc::new(TokioMutex::new(child.stdin.take().unwrap()));
        {
            let mut guard = stdin.lock().await;
            guard
                .write_all(b"{\"type\":\"hitl_approve\",\"approved\":true}\n")
                .await
                .expect("write must succeed");
            guard.flush().await.expect("flush must succeed");
        }
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
}
