# TEAM J — Concurrency, Resources & Performance (Wave 1, cross-cutting)

Baseline: `integration/audit-fixes`. Read-only hunt. Cross-references: F-D-5 (stdout
forwarder back-pressure), F-C-6 (code.py no process-group kill), F-C-5 (unclamped
run_python/run_node/web_wait timeouts), F-B-8 (non-idempotent browser retries),
F-F-11 (whole-history re-render), F-A-3 (event-loop-blocking file re-read),
F-I-4 (unauth CDP 9222).

Status: PRELIMINARY PASS (resilience checkpoint) — deep sweep in progress; more
findings and the process-census + timeout appendices follow below as they are
confirmed.

---

## F-J-1: `logs/scheduled_runs/` accumulates one log file per scheduled run forever — no retention
- **File:** orchestrator/scheduled_runner.py:638-646 (`_make_run_log_path`), mcp_server/logger.py:256-281 (`apply_log_retention`)
- **Severity:** Medium
- **Class:** leak
- **Evidence:** Every scheduled-agent spawn opens `logs/scheduled_runs/{task_id}_{ts}.log` (scheduled_runner.py:548-552) and nothing ever deletes these files. `apply_log_retention()` (called from cli.py:99 with keep_days=7) globs only `kim_*.jsonl` in `logs/` — it never descends into `scheduled_runs/`. On this very machine the directory already holds **1,155 log files** (dating back to 2026-06-07). A user with a 5-minute cron task generates ~288 files/day, unbounded. Directory-entry bloat also slows the reaper's registry scans and any `ls`/backup of `logs/`.
- **Fix sketch:** Extend `apply_log_retention` (or the scheduled runner's reap pass) to prune `logs/scheduled_runs/*.log` older than keep_days; keep the last N per task id.
- **Cross-territory?** yes — Python territory (Team A/fix owner for orchestrator + mcp_server).

## F-J-2: `runHistory` grows unbounded in-session and the FULL array is re-serialized over IPC + rewritten to disk after every run (O(n²))
- **File:** desktop/src/hooks/useChatStream.ts:119, 832-850
- **Severity:** Medium
- **Class:** perf | leak (memory growth)
- **Evidence:** `runHistory` is a plain `useState` array (line 119) — unlike `liveHistory`, which uses `useCappedState(MAX_ACTIVITY_ITEMS)`, it has **no cap**. Each completed run appends `{ activity: activitySnapshot, ... }` where `activitySnapshot` is up to 300 `ActivityItem`s including full tool-output text (line 831-833). Then `invoke('save_run_history', { ..., runs: next })` serializes the **entire cumulative array** across the Tauri IPC boundary and rewrites the whole file after every run (line 849). A long working session with dozens of runs → megabytes held in React state, megabytes re-crossed and re-written per run: cumulative O(n²). This compounds F-F-11 (whole-history re-render) — the same array is also render input.
- **Fix sketch:** Cap in-memory runHistory (e.g. last 50 runs); make `save_run_history` append-only (send only `newRunEntry`, Rust appends) or persist deltas.
- **Cross-territory?** yes — frontend (Team F) + Rust command (Team D/E).

## F-J-3: Auto-launched Chrome with an open CDP debug port is detached and has NO kill path anywhere — orphan by design, forever
- **File:** orchestrator/providers/browser/provider.py:904-911 (`_chrome_proc = subprocess.Popen(args, **popen_kwargs)` with `start_new_session=True`)
- **Severity:** Medium
- **Class:** leak | security-adjacent
- **Evidence:** `_launch_chrome_for_signin` spawns a **visible Chrome with `--remote-debugging-port`** detached (`start_new_session=True`) so it survives the short-lived bridge — intentional for session reuse. But `_chrome_proc` is stored and then never referenced again: grep shows exactly two mentions (init at :156, assign at :911) — no `.kill()`, no `.wait()`, no shutdown hook in the provider, the bridge service, or the Tauri shell. Consequences: (1) the Chrome instance with an **unauthenticated CDP port** (see F-I-4) outlives Kim entirely — quitting the app leaves it listening; (2) on POSIX, if the spawning process stays alive (long-lived codex_bridge_service), an exited Chrome becomes a zombie since nobody `wait()`s the Popen handle; (3) repeated launches on different ports can stack multiple debug Chromes.
- **Fix sketch:** Track the CDP-Chrome PID in a registry file; offer/perform reaping on app quit (at minimum kill when Kim shuts down and the user didn't open that window manually); `Popen.poll()` before relaunch.
- **Cross-territory?** yes — browser provider (Team B) + security overlap (Team I).

## F-J-4: Every session append does a synchronous `os.fsync()` on the async agent event loop — latency sibling to F-A-3
- **File:** orchestrator/session_store.py:111-139 (`_append_line`), called by `append_message`/`append_checkpoint`/`append_run_started` etc. from the `async def run()` loop in orchestrator/agent.py (:719, 740, 794, 916, 925, 960, 977, 990, 1009)
- **Severity:** Medium
- **Class:** perf
- **Evidence:** `_append_line` does `open() → write → flush → os.fsync(fh.fileno())` (session_store.py:136-139) and additionally `stat()`s the file for a rotation check on every call. All the `append_*` methods that wrap it are plain (non-async) and are invoked directly — **not** via `asyncio.to_thread`/`run_in_executor` — from inside the async agent loop (verified: 0 `to_thread`-wrapped appends in agent.py). `os.fsync` forces a disk flush and blocks the calling thread for potentially tens of ms (far more under I/O contention, e.g. Spotlight indexing, Time Machine, a slow/encrypted volume). Because it runs on the event loop, each append stalls **everything** cooperatively scheduled there: stdout→IPC forwarding to the UI, HITL approval reads, and steer-inbox polling. With several appends per tool iteration this is a systematic per-turn latency tax on the send-task→first-token and inter-step paths. This is exactly the class F-A-3 flagged ("one event-loop-blocking file re-read — find siblings"): here it is the write side, amplified by fsync.
- **Fix sketch:** Offload `_append_line` via `asyncio.to_thread` from the async callers, or batch/coalesce appends and drop the per-line `fsync` to periodic (JSONL append durability rarely needs fsync-per-line). Cache the size for the rotation check instead of `stat()`-ing every append.
- **Cross-territory?** yes — Team A owns orchestrator/session_store.py + agent.py.
