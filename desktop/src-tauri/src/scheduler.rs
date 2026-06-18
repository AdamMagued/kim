//! D6: minimal in-app scheduler tick loop.
//!
//! A tokio interval fires every 60s and runs any due scheduled tasks, but only
//! when (a) schedules are enabled in config and (b) no interactive agent task is
//! running — a scheduled run must never stomp an interactive one, so we skip the
//! tick instead. An AtomicBool guards against overlapping ticks.

use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Emitter, Manager};

static SCHEDULER_TICK_ACTIVE: AtomicBool = AtomicBool::new(false);

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
    // (b) never stomp an interactive run — skip this tick if one is active.
    if let Some(task_state) = app_handle.try_state::<crate::TaskState>() {
        let guard = task_state.lock().await;
        if guard.pid.is_some() || guard.starting {
            return;
        }
    }
    // Fire any due schedules and log the outcome to the status channel.
    match crate::schedule_commands::run_due_scheduled_task(false).await {
        Ok(json) => {
            let _ = app_handle.emit(
                "kim-agent-output",
                format!("[STATUS] Scheduler ran due tasks: {json}"),
            );
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
        assert!(!try_acquire_tick(), "second acquire must be blocked while held");
        release_tick();
        assert!(try_acquire_tick(), "acquire should succeed after release");
        release_tick();
    }
}
