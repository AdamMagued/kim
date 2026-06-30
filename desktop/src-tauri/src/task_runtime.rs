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
use std::sync::OnceLock;
use tokio::sync::Mutex as TokioMutex;

/// Source that spawned the currently-running task.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum SpawnSource {
    /// Spawned by the Tauri GUI `send_task` command.
    Gui,
    /// Spawned by the HTTP bridge `/v1/task` endpoint (kimctl CLI).
    Bridge,
}

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
    /// Stdin handle for the GUI path (tokio async child).
    /// Written by `hitl_respond_approval` / `steer_task`.
    pub gui_stdin: Option<tokio::process::ChildStdin>,
    /// Stdin handle for the bridge path (std sync child).
    /// Written by `/v1/task/approve`.
    pub bridge_stdin: Option<std::process::ChildStdin>,
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
        self.gui_stdin = None;
        self.bridge_stdin = None;
    }

    /// True if the slot is occupied (either starting or a pid is held).
    pub fn is_occupied(&self) -> bool {
        self.pid.is_some() || self.starting
    }

    /// Convenience: write a HITL/steer JSON line to whichever stdin handle is
    /// currently active. Called by both the Tauri command path (GUI) and the
    /// HTTP bridge path (CLI) so that cross-path approval always works.
    pub(crate) async fn write_stdin_line(&mut self, msg: &str) -> Result<(), String> {
        use std::io::Write as _;
        use tokio::io::AsyncWriteExt as _;

        if let Some(ref mut s) = self.gui_stdin {
            s.write_all(msg.as_bytes())
                .await
                .map_err(|e| e.to_string())?;
            s.flush().await.map_err(|e| e.to_string())?;
            return Ok(());
        }
        if let Some(ref mut s) = self.bridge_stdin {
            s.write_all(msg.as_bytes()).map_err(|e| e.to_string())?;
            s.flush().map_err(|e| e.to_string())?;
            return Ok(());
        }
        Err("No agent stdin available".to_string())
    }
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
}
