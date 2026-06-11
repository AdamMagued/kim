//! IPC commands for the scheduled-task surface.
//!
//! All commands are thin bridges that shell out to `python -m kimctl schedule`
//! subcommands and return raw JSON strings. All scheduling logic stays in
//! Python (orchestrator/cron_store.py + orchestrator/scheduled_runner.py).
//!
//! Commands:
//!   `list_scheduled_tasks`    -- schedule list [--enabled-only] --json
//!   `add_scheduled_task`      -- schedule add <task> <expr> [opts] --json
//!   `update_scheduled_task`   -- schedule update <id> [opts] --json
//!   `delete_scheduled_task`   -- schedule delete <id> --json
//!   `list_due_scheduled_tasks`-- schedule due [--as-of] --json
//!   `run_due_scheduled_task`  -- schedule run-due [--dry-run] --json

use crate::{default_project_root, subprocess::find_python_interpreter};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::Mutex;
use tauri::{AppHandle, Emitter, State};

// ---------------------------------------------------------------------------
// Shared subprocess helper
// ---------------------------------------------------------------------------

/// Run a kimctl subcommand given a pre-built args vec (args[0] = interpreter).
/// Returns stdout on success, or a structured Err preferring stderr then stdout
/// then a generic fallback (kimctl writes JSON errors to stdout with exit 1).
fn run_kimctl(args: &[String], kim_root: &PathBuf) -> Result<String, String> {
    let mut cmd = std::process::Command::new(&args[0]);
    for arg in &args[1..] {
        cmd.arg(arg);
    }
    cmd.current_dir(kim_root)
        .env("PYTHONPATH", kim_root.to_str().unwrap_or(""));

    let output = cmd
        .output()
        .map_err(|e| format!("Failed to start kimctl: {e}"))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Err(if !stderr.is_empty() {
            stderr
        } else if !stdout.is_empty() {
            stdout
        } else {
            format!("kimctl exited with {}", output.status)
        })
    }
}

fn run_due_once(kim_root: &PathBuf, dry_run: bool) -> Result<String, String> {
    let python = find_python_interpreter(kim_root)?;
    let args = build_run_due_args(&python, dry_run);
    run_kimctl(&args, kim_root)
}

// ---------------------------------------------------------------------------
// schedule timer state
// ---------------------------------------------------------------------------

const MIN_TIMER_INTERVAL_SECONDS: u64 = 60;

#[derive(Default)]
pub(crate) struct ScheduleTimerInner {
    interval_seconds: u64,
    tick_count: u64,
    last_result: Option<String>,
    last_error: Option<String>,
    handle: Option<tokio::task::JoinHandle<()>>,
}

pub(crate) type ScheduleTimerState = Arc<Mutex<ScheduleTimerInner>>;

pub(crate) fn new_schedule_timer_state() -> ScheduleTimerState {
    Arc::new(Mutex::new(ScheduleTimerInner::default()))
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct ScheduleTimerStatus {
    running: bool,
    interval_seconds: u64,
    tick_count: u64,
    last_result: Option<String>,
    last_error: Option<String>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub(crate) struct ScheduleTimerTickEvent {
    tick_count: u64,
    result: Option<String>,
    error: Option<String>,
}

pub(crate) fn clamp_timer_interval(seconds: u64) -> u64 {
    seconds.max(MIN_TIMER_INTERVAL_SECONDS)
}

fn tick_event_from_status(status: &ScheduleTimerStatus) -> ScheduleTimerTickEvent {
    ScheduleTimerTickEvent {
        tick_count: status.tick_count,
        result: status.last_result.clone(),
        error: status.last_error.clone(),
    }
}

fn status_from_inner(inner: &ScheduleTimerInner) -> ScheduleTimerStatus {
    ScheduleTimerStatus {
        running: inner.handle.is_some(),
        interval_seconds: inner.interval_seconds,
        tick_count: inner.tick_count,
        last_result: inner.last_result.clone(),
        last_error: inner.last_error.clone(),
    }
}

// ---------------------------------------------------------------------------
// list_scheduled_tasks
// ---------------------------------------------------------------------------

/// Build args for `kimctl schedule list [--enabled-only] --json`.
pub(crate) fn build_list_args(python: &str, enabled_only: bool) -> Vec<String> {
    let mut args = vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "list".to_string(),
        "--json".to_string(),
    ];
    if enabled_only {
        args.push("--enabled-only".to_string());
    }
    args
}

/// Return the JSON array of all scheduled tasks.
/// Pass `enabled_only: true` to filter to enabled tasks only.
#[tauri::command]
pub(crate) async fn list_scheduled_tasks(
    enabled_only: bool,
) -> Result<String, String> {
    let kim_root = default_project_root();
    let python = find_python_interpreter(&kim_root)?;
    let args = build_list_args(&python, enabled_only);
    tokio::task::spawn_blocking(move || run_kimctl(&args, &kim_root))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// add_scheduled_task
// ---------------------------------------------------------------------------

/// Build args for `kimctl schedule add <task> <expr> [--provider] [--disabled] --json`.
pub(crate) fn build_add_args(
    python: &str,
    task: &str,
    expr: &str,
    provider: Option<&str>,
    disabled: bool,
) -> Vec<String> {
    let mut args = vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "add".to_string(),
        task.to_string(),
        expr.to_string(),
        "--json".to_string(),
    ];
    if let Some(p) = provider {
        args.push("--provider".to_string());
        args.push(p.to_string());
    }
    if disabled {
        args.push("--disabled".to_string());
    }
    args
}

/// Add a new scheduled task. Returns the JSON object `{"ok": true, "task": {...}}`.
#[tauri::command]
pub(crate) async fn add_scheduled_task(
    task: String,
    expr: String,
    provider: Option<String>,
    disabled: bool,
) -> Result<String, String> {
    let kim_root = default_project_root();
    let python = find_python_interpreter(&kim_root)?;
    let args = build_add_args(&python, &task, &expr, provider.as_deref(), disabled);
    tokio::task::spawn_blocking(move || run_kimctl(&args, &kim_root))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// update_scheduled_task
// ---------------------------------------------------------------------------

/// Build args for `kimctl schedule update <id> [opts] --json`.
/// `enabled`: Some(true) -> `--enable`, Some(false) -> `--disable`, None -> omitted.
pub(crate) fn build_update_args(
    python: &str,
    id: &str,
    task: Option<&str>,
    expr: Option<&str>,
    provider: Option<&str>,
    enabled: Option<bool>,
) -> Vec<String> {
    let mut args = vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "update".to_string(),
        id.to_string(),
        "--json".to_string(),
    ];
    if let Some(t) = task {
        args.push("--task".to_string());
        args.push(t.to_string());
    }
    if let Some(e) = expr {
        args.push("--expr".to_string());
        args.push(e.to_string());
    }
    if let Some(p) = provider {
        args.push("--provider".to_string());
        args.push(p.to_string());
    }
    match enabled {
        Some(true) => args.push("--enable".to_string()),
        Some(false) => args.push("--disable".to_string()),
        None => {}
    }
    args
}

/// Update an existing scheduled task. Returns `{"ok": true, "task": {...}}`.
/// `enabled`: `true` -> enable, `false` -> disable, `null` -> no change.
#[tauri::command]
pub(crate) async fn update_scheduled_task(
    id: String,
    task: Option<String>,
    expr: Option<String>,
    provider: Option<String>,
    enabled: Option<bool>,
) -> Result<String, String> {
    let kim_root = default_project_root();
    let python = find_python_interpreter(&kim_root)?;
    let args = build_update_args(
        &python,
        &id,
        task.as_deref(),
        expr.as_deref(),
        provider.as_deref(),
        enabled,
    );
    tokio::task::spawn_blocking(move || run_kimctl(&args, &kim_root))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// delete_scheduled_task
// ---------------------------------------------------------------------------

/// Build args for `kimctl schedule delete <id> --json`.
pub(crate) fn build_delete_args(python: &str, id: &str) -> Vec<String> {
    vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "delete".to_string(),
        id.to_string(),
        "--json".to_string(),
    ]
}

/// Delete a scheduled task by id. Returns `{"ok": true, "id": "..."}`.
#[tauri::command]
pub(crate) async fn delete_scheduled_task(id: String) -> Result<String, String> {
    let kim_root = default_project_root();
    let python = find_python_interpreter(&kim_root)?;
    let args = build_delete_args(&python, &id);
    tokio::task::spawn_blocking(move || run_kimctl(&args, &kim_root))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// list_due_scheduled_tasks
// ---------------------------------------------------------------------------

/// Build args for `kimctl schedule due --json [--as-of <ISO>]`.
pub(crate) fn build_due_args(python: &str, as_of: Option<&str>) -> Vec<String> {
    let mut args = vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "due".to_string(),
        "--json".to_string(),
    ];
    if let Some(ts) = as_of {
        args.push("--as-of".to_string());
        args.push(ts.to_string());
    }
    args
}

/// Return the JSON array of due scheduled tasks by calling
/// `python -m kimctl schedule due --json [--as-of <ISO>]`.
#[tauri::command]
pub(crate) async fn list_due_scheduled_tasks(
    as_of: Option<String>,
) -> Result<String, String> {
    let kim_root = default_project_root();
    let python = find_python_interpreter(&kim_root)?;
    let args = build_due_args(&python, as_of.as_deref());
    tokio::task::spawn_blocking(move || run_kimctl(&args, &kim_root))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// run_due_scheduled_task
// ---------------------------------------------------------------------------

/// Build the argument list for `kimctl schedule run-due --json [--dry-run]`.
pub(crate) fn build_run_due_args(python: &str, dry_run: bool) -> Vec<String> {
    let mut args = vec![
        python.to_string(),
        "-m".to_string(),
        "kimctl".to_string(),
        "schedule".to_string(),
        "run-due".to_string(),
        "--json".to_string(),
    ];
    if dry_run {
        args.push("--dry-run".to_string());
    }
    args
}

/// Run the next due scheduled task.
/// Returns the raw JSON object string (`RunDueResult` fields).
/// Passes `--dry-run` when `dry_run` is true.
#[tauri::command]
pub(crate) async fn run_due_scheduled_task(dry_run: bool) -> Result<String, String> {
    let kim_root = default_project_root();
    tokio::task::spawn_blocking(move || run_due_once(&kim_root, dry_run))
        .await
        .map_err(|e| format!("Executor error: {e}"))?
}

// ---------------------------------------------------------------------------
// schedule timer commands
// ---------------------------------------------------------------------------

/// Start an opt-in periodic schedule runner.
///
/// The loop runs `kimctl schedule run-due --json` immediately, then sleeps for
/// the clamped interval between ticks. It never auto-starts; callers must
/// invoke this command explicitly. Provider safety remains enforced in Python
/// by orchestrator/scheduled_runner.py.
#[tauri::command]
pub(crate) async fn start_schedule_timer(
    interval_seconds: Option<u64>,
    app_handle: AppHandle,
    state: State<'_, ScheduleTimerState>,
) -> Result<ScheduleTimerStatus, String> {
    let interval = clamp_timer_interval(interval_seconds.unwrap_or(MIN_TIMER_INTERVAL_SECONDS));
    let timer_state = Arc::clone(&*state);

    {
        let mut inner = timer_state.lock().await;
        if inner.handle.is_some() {
            return Ok(status_from_inner(&inner));
        }

        inner.interval_seconds = interval;
        inner.tick_count = 0;
        inner.last_result = None;
        inner.last_error = None;

        let loop_state = Arc::clone(&timer_state);
        let handle = tokio::spawn(async move {
            let kim_root = default_project_root();
            loop {
                let tick_result =
                    tokio::task::spawn_blocking({
                        let kim_root = kim_root.clone();
                        move || run_due_once(&kim_root, false)
                    })
                    .await
                    .map_err(|e| format!("Executor error: {e}"))
                    .and_then(|r| r);

                let tick_event = {
                    let mut guard = loop_state.lock().await;
                    guard.tick_count = guard.tick_count.saturating_add(1);
                    match tick_result {
                        Ok(json) => {
                            guard.last_result = Some(json);
                            guard.last_error = None;
                        }
                        Err(err) => {
                            guard.last_result = None;
                            guard.last_error = Some(err);
                        }
                    }
                    tick_event_from_status(&status_from_inner(&guard))
                };

                let _ = app_handle.emit("schedule-timer-tick", tick_event);

                tokio::time::sleep(std::time::Duration::from_secs(interval)).await;
            }
        });

        inner.handle = Some(handle);
        Ok(status_from_inner(&inner))
    }
}

/// Stop the opt-in periodic schedule runner if it is active.
#[tauri::command]
pub(crate) async fn stop_schedule_timer(
    state: State<'_, ScheduleTimerState>,
) -> Result<ScheduleTimerStatus, String> {
    let mut inner = state.lock().await;
    if let Some(handle) = inner.handle.take() {
        handle.abort();
    }
    Ok(status_from_inner(&inner))
}

/// Return the current opt-in schedule timer status.
#[tauri::command]
pub(crate) async fn get_schedule_timer_status(
    state: State<'_, ScheduleTimerState>,
) -> Result<ScheduleTimerStatus, String> {
    let inner = state.lock().await;
    Ok(status_from_inner(&inner))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- async shape --

    #[test]
    fn test_list_scheduled_tasks_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(list_scheduled_tasks(false));
        assert_is_async(list_scheduled_tasks(true));
    }

    #[test]
    fn test_add_scheduled_task_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(add_scheduled_task(
            "t".into(), "@daily".into(), None, false,
        ));
        assert_is_async(add_scheduled_task(
            "t".into(), "@daily".into(), Some("ollama".into()), true,
        ));
    }

    #[test]
    fn test_update_scheduled_task_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(update_scheduled_task(
            "id1".into(), None, None, None, None,
        ));
        assert_is_async(update_scheduled_task(
            "id1".into(), Some("new task".into()), None, None, Some(true),
        ));
    }

    #[test]
    fn test_delete_scheduled_task_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(delete_scheduled_task("id1".into()));
    }

    #[test]
    fn test_list_due_scheduled_tasks_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(list_due_scheduled_tasks(None));
        assert_is_async(list_due_scheduled_tasks(Some("2026-06-05T12:00:00+00:00".into())));
    }

    #[test]
    fn test_run_due_scheduled_task_is_async_fn() {
        fn assert_is_async<F: std::future::Future>(_: F) {}
        assert_is_async(run_due_scheduled_task(false));
        assert_is_async(run_due_scheduled_task(true));
    }

    #[test]
    fn test_clamp_timer_interval_enforces_minimum() {
        assert_eq!(clamp_timer_interval(0), 60);
        assert_eq!(clamp_timer_interval(59), 60);
        assert_eq!(clamp_timer_interval(60), 60);
        assert_eq!(clamp_timer_interval(300), 300);
    }

    #[test]
    fn test_status_from_inner_reports_idle_defaults() {
        let inner = ScheduleTimerInner::default();
        let status = status_from_inner(&inner);
        assert_eq!(status.running, false);
        assert_eq!(status.interval_seconds, 0);
        assert_eq!(status.tick_count, 0);
        assert_eq!(status.last_result, None);
        assert_eq!(status.last_error, None);
    }

    #[test]
    fn test_status_from_inner_carries_last_result_and_error() {
        let mut inner = ScheduleTimerInner::default();
        inner.interval_seconds = 120;
        inner.tick_count = 3;
        inner.last_result = Some("{\"ok\":true}".to_string());
        inner.last_error = Some("previous failure".to_string());
        let status = status_from_inner(&inner);
        assert_eq!(status.running, false);
        assert_eq!(status.interval_seconds, 120);
        assert_eq!(status.tick_count, 3);
        assert_eq!(status.last_result.as_deref(), Some("{\"ok\":true}"));
        assert_eq!(status.last_error.as_deref(), Some("previous failure"));
    }

    #[test]
    fn test_tick_event_from_status_uses_tick_result_and_error() {
        let status = ScheduleTimerStatus {
            running: true,
            interval_seconds: 60,
            tick_count: 7,
            last_result: Some("{\"ok\":true}".to_string()),
            last_error: Some("err".to_string()),
        };
        let event = tick_event_from_status(&status);
        assert_eq!(event.tick_count, 7);
        assert_eq!(event.result.as_deref(), Some("{\"ok\":true}"));
        assert_eq!(event.error.as_deref(), Some("err"));
    }

    // -- build_list_args --

    #[test]
    fn test_build_list_args_no_filter() {
        let args = build_list_args("python3", false);
        assert_eq!(args[4], "list");
        assert!(args.contains(&"--json".to_string()));
        assert!(!args.contains(&"--enabled-only".to_string()));
    }

    #[test]
    fn test_build_list_args_enabled_only() {
        let args = build_list_args("python3", true);
        assert!(args.contains(&"--enabled-only".to_string()));
        assert!(args.contains(&"--json".to_string()));
    }

    // -- build_due_args --

    #[test]
    fn test_build_due_args_without_as_of() {
        let args = build_due_args("python3", None);
        assert_eq!(args[4], "due");
        assert!(args.contains(&"--json".to_string()));
        assert!(!args.contains(&"--as-of".to_string()));
    }

    #[test]
    fn test_build_due_args_with_as_of() {
        let args = build_due_args("python3", Some("2026-06-05T12:00:00+00:00"));
        let idx = args.iter().position(|a| a == "--as-of").unwrap();
        assert_eq!(args[idx + 1], "2026-06-05T12:00:00+00:00");
    }

    // -- build_add_args --

    #[test]
    fn test_build_add_args_minimal() {
        let args = build_add_args("python3", "check disk", "@daily", None, false);
        assert_eq!(args[4], "add");
        assert_eq!(args[5], "check disk");
        assert_eq!(args[6], "@daily");
        assert!(args.contains(&"--json".to_string()));
        assert!(!args.contains(&"--provider".to_string()));
        assert!(!args.contains(&"--disabled".to_string()));
    }

    #[test]
    fn test_build_add_args_with_provider() {
        let args = build_add_args("python3", "t", "@hourly", Some("ollama"), false);
        let idx = args.iter().position(|a| a == "--provider").unwrap();
        assert_eq!(args[idx + 1], "ollama");
    }

    #[test]
    fn test_build_add_args_disabled_flag() {
        let args = build_add_args("python3", "t", "@daily", None, true);
        assert!(args.contains(&"--disabled".to_string()));
    }

    #[test]
    fn test_build_add_args_interpreter_first() {
        let args = build_add_args("/venv/bin/python", "t", "@daily", None, false);
        assert_eq!(args[0], "/venv/bin/python");
    }

    // -- build_update_args --

    #[test]
    fn test_build_update_args_id_and_json() {
        let args = build_update_args("python3", "abc123", None, None, None, None);
        assert_eq!(args[4], "update");
        assert_eq!(args[5], "abc123");
        assert!(args.contains(&"--json".to_string()));
        assert!(!args.contains(&"--enable".to_string()));
        assert!(!args.contains(&"--disable".to_string()));
    }

    #[test]
    fn test_build_update_args_task_and_expr() {
        let args = build_update_args(
            "python3", "id1", Some("new text"), Some("@weekly"), None, None,
        );
        let t_idx = args.iter().position(|a| a == "--task").unwrap();
        assert_eq!(args[t_idx + 1], "new text");
        let e_idx = args.iter().position(|a| a == "--expr").unwrap();
        assert_eq!(args[e_idx + 1], "@weekly");
    }

    #[test]
    fn test_build_update_args_enable() {
        let args = build_update_args("python3", "id1", None, None, None, Some(true));
        assert!(args.contains(&"--enable".to_string()));
        assert!(!args.contains(&"--disable".to_string()));
    }

    #[test]
    fn test_build_update_args_disable() {
        let args = build_update_args("python3", "id1", None, None, None, Some(false));
        assert!(args.contains(&"--disable".to_string()));
        assert!(!args.contains(&"--enable".to_string()));
    }

    #[test]
    fn test_build_update_args_provider() {
        let args = build_update_args("python3", "id1", None, None, Some("browser"), None);
        let idx = args.iter().position(|a| a == "--provider").unwrap();
        assert_eq!(args[idx + 1], "browser");
    }

    // -- build_delete_args --

    #[test]
    fn test_build_delete_args() {
        let args = build_delete_args("python3", "task-id-999");
        assert_eq!(args[4], "delete");
        assert_eq!(args[5], "task-id-999");
        assert!(args.contains(&"--json".to_string()));
    }

    #[test]
    fn test_build_delete_args_interpreter_first() {
        let args = build_delete_args("/venv/bin/python", "x");
        assert_eq!(args[0], "/venv/bin/python");
    }

    // -- build_run_due_args --

    #[test]
    fn test_build_run_due_args_no_dry_run() {
        let args = build_run_due_args("/usr/bin/python3", false);
        assert_eq!(args[0], "/usr/bin/python3");
        assert_eq!(args[4], "run-due");
        assert!(args.contains(&"--json".to_string()));
        assert!(!args.contains(&"--dry-run".to_string()));
        assert_eq!(args.len(), 6);
    }

    #[test]
    fn test_build_run_due_args_dry_run() {
        let args = build_run_due_args("/usr/bin/python3", true);
        assert_eq!(args.len(), 7);
        assert_eq!(args[6], "--dry-run");
    }

    #[test]
    fn test_build_run_due_args_interpreter_is_first() {
        let interp = "/custom/venv/bin/python";
        let args = build_run_due_args(interp, false);
        assert_eq!(args[0], interp);
    }

    #[test]
    fn test_build_run_due_args_always_includes_json_flag() {
        for dry in [false, true] {
            let args = build_run_due_args("python3", dry);
            assert!(args.contains(&"--json".to_string()), "dry_run={dry}");
        }
    }

    // -- ASCII cleanliness --

    #[test]
    fn test_source_is_ascii() {
        let src = include_str!("schedule_commands.rs");
        for (i, ch) in src.chars().enumerate() {
            assert!(
                ch.is_ascii(),
                "non-ASCII char {:?} at byte offset {}",
                ch, i
            );
        }
    }
}
