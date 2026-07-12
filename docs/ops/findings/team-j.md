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
