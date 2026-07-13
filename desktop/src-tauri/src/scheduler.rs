//! D6: minimal in-app scheduler tick loop.
//!
//! A tokio interval fires every 60s and runs any due scheduled tasks, but only
//! when (a) schedules are enabled in config and (b) no interactive agent task is
//! running — a scheduled run must never stomp an interactive one, so we skip the
//! tick instead. An AtomicBool guards against overlapping ticks.

use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Emitter, Manager};

static SCHEDULER_TICK_ACTIVE: AtomicBool = AtomicBool::new(false);

/// D19: user-facing pause switch for the BUILT-IN 60s loop.
///
/// Before this flag existed, SchedulePane's Stop button only aborted the
/// separate opt-in timer (`stop_schedule_timer`) while this loop kept firing
/// due tasks every 60s and injecting their output into the open chat — the
/// pane said "Stopped" but scheduled execution never stopped.
/// `stop_schedule_timer` now sets this flag and `start_schedule_timer`
/// clears it, so Stop genuinely halts ALL scheduled execution. In-memory
/// only: an app restart returns to the `schedules_enabled` config default.
static SCHEDULER_PAUSED: AtomicBool = AtomicBool::new(false);

pub(crate) fn set_scheduler_paused(paused: bool) {
    SCHEDULER_PAUSED.store(paused, Ordering::Release);
}

pub(crate) fn is_scheduler_paused() -> bool {
    SCHEDULER_PAUSED.load(Ordering::Acquire)
}

/// Try to claim the tick slot. Returns true if claimed (caller must release),
/// false if a tick is already in flight and this one should be skipped.
pub(crate) fn try_acquire_tick() -> bool {
    SCHEDULER_TICK_ACTIVE
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_ok()
}

pub(crate) fn release_tick() {
    SCHEDULER_TICK_ACTIVE.store(false, Ordering::Release);
}

/// Spawn the 60s scheduler loop.
pub(crate) fn start_scheduler(app_handle: tauri::AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
        interval.tick().await; // consume the immediate first tick
        loop {
            interval.tick().await;
            if !try_acquire_tick() {
                continue; // a previous tick is still running — skip
            }
            scheduler_tick(&app_handle).await;
            release_tick();
        }
    });
}

async fn scheduler_tick(app_handle: &tauri::AppHandle) {
    // (a) master switch (default on).
    let enabled = app_handle
        .try_state::<crate::config::AppConfig>()
        .map(|c| c.schedules_enabled)
        .unwrap_or(true);
    if !enabled {
        return;
    }
    // (a2) D19: the user pressed Stop in the schedule pane — honor it.
    if is_scheduler_paused() {
        return;
    }
    // (a3) When the opt-in schedule timer is running it already fires due
    // tasks on its own cadence; ticking here too would double-run them.
    if let Some(timer) = app_handle.try_state::<crate::schedule_commands::ScheduleTimerState>() {
        if timer.lock().await.is_running() {
            return;
        }
    }
    // (b) never stomp an interactive run — skip this tick if one is active.
    // H-PROC-1: the guard must read TaskRuntime, the store both spawn paths
    // (GUI send_task and bridge /v1/task) actually populate. The old check on
    // the legacy `TaskState` managed state was dead code — nothing ever set
    // its pid/starting, so scheduled runs could stomp live interactive tasks.
    {
        let mut rt = crate::task_runtime::task_runtime().lock().await;
        // Recover from a stale dead pid so a missed clear can't disable the
        // scheduler forever (same recovery reserve_slot performs).
        if let Some(pid) = rt.pid {
            if !crate::subprocess::process_exists(pid) {
                rt.clear();
            }
        }
        if rt.is_occupied() {
            return;
        }
    }
    // Fire any due schedules and log the outcome to the status channel.
    match crate::schedule_commands::run_due_scheduled_task(false).await {
        Ok(json) => {
            // L-PROC-7: only surface ticks that actually launched something —
            // the idle result `{"ok":true,"launched":false,...}` fires every
            // 60s and would spam the agent chat channel.
            let launched = serde_json::from_str::<serde_json::Value>(&json)
                .ok()
                .and_then(|v| v.get("launched").and_then(|l| l.as_bool()))
                .unwrap_or(true);
            if launched {
                let _ = app_handle.emit(
                    "kim-agent-output",
                    format!("[STATUS] Scheduler ran due tasks: {json}"),
                );
            }
        }
        Err(e) => {
            let _ = app_handle.emit("kim-agent-output", format!("[STATUS] Scheduler error: {e}"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overlap_guard_blocks_second_acquire_until_release() {
        release_tick(); // ensure a clean slate
        assert!(try_acquire_tick(), "first acquire should succeed");
        assert!(
            !try_acquire_tick(),
            "second acquire must be blocked while held"
        );
        release_tick();
        assert!(try_acquire_tick(), "acquire should succeed after release");
        release_tick();
    }

    #[test]
    fn scheduler_pause_flag_round_trips() {
        // D19: Stop sets this so scheduler_tick early-returns; Start clears it.
        let original = is_scheduler_paused();
        set_scheduler_paused(true);
        assert!(is_scheduler_paused(), "Stop must pause the built-in loop");
        set_scheduler_paused(false);
        assert!(!is_scheduler_paused(), "Start must un-pause it");
        set_scheduler_paused(original); // leave the global as we found it
    }
}
