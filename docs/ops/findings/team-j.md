# TEAM J — Concurrency, Resources & Performance (Wave 1, cross-cutting)

Baseline: `integration/audit-fixes`. Read-only hunt. Cross-references: F-D-5 (stdout
forwarder back-pressure), F-C-6 (code.py no process-group kill), F-C-5 (unclamped
run_python/run_node/web_wait timeouts), F-B-8 (non-idempotent browser retries),
F-F-11 (whole-history re-render), F-A-3 (event-loop-blocking file re-read),
F-I-4 (unauth CDP 9222).

Status: COMPLETE. 6 findings (F-J-1..6). Process-census (Appendix A), timeout
table (Appendix B), and clean/well-handled notes (Appendix C) below.

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

## F-J-5: `run_update` runs `git pull` + `pip install` with NO timeout as blocking `.output()` on the async runtime — a stalled network hangs the update forever and blocks a tokio worker
- **File:** desktop/src-tauri/src/run_history.rs:231, 271, 307, 339 (inside `pub async fn run_update`, :218)
- **Severity:** Medium
- **Class:** perf | leak (thread) | timeout-gap
- **Evidence:** `run_update` is an `async fn` but calls `std::process::Command::...output()` **synchronously** four times (`git remote get-url`, `git pull`, a verify step, `pip install`) with no `tokio::task::spawn_blocking` and no timeout. Two problems compound: (1) a blocking `.output()` on a tokio worker thread parks that worker for the entire git/pip duration — under Tauri's default multi-thread runtime this starves other async work sharing the pool; (2) `git pull` against a black-hole network (dropped packets, captive portal) stalls at the transfer with no git-level low-speed timeout, and `pip install` can likewise wedge on a slow index — the update then hangs **indefinitely** with the UI stuck "updating" and no cancel path. No `--timeout`/`GIT_HTTP_LOW_SPEED_*` is set.
- **Fix sketch:** Run each command via `spawn_blocking` (or `tokio::process` with `tokio::time::timeout`); pass `git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30` and `pip --timeout=30`; surface a cancel/timeout error to the UI.
- **Cross-territory?** yes — Team D/E owns desktop/src-tauri.

## F-J-6: Scheduled-agent wall-clock reaper only runs while the Tauri app is open — a wedged scheduled agent (+ its MCP server + chromium) runs unbounded whenever the app is closed
- **File:** orchestrator/scheduled_runner.py:299-352 (`reap_orphaned_agents`), driven by desktop/src-tauri/src/scheduler.rs:47 (60s tokio interval) / schedule_commands.rs opt-in timer
- **Severity:** Low
- **Class:** leak
- **Evidence:** Scheduled agents are spawned as **detached** `subprocess.Popen` process-group leaders (scheduled_runner.py:568, `start_new_session=True`) so they survive the app. The only thing that enforces `_AGENT_MAX_WALL_SECONDS` (1h) and reaps a wedged/runaway agent is `reap_orphaned_agents`, which fires on each *runner tick*. The runner ticks are produced by the Rust scheduler loop (scheduler.rs 60s interval) and the opt-in schedule timer — **both live inside the Tauri process**. So when the user quits Kim while a scheduled agent is mid-run (or the agent wedges), nothing reaps it until the app is relaunched: the 1h wall-clock cap is silently not enforced during app-closed time, and a hung agent holding a chromium/MCP subtree can run for hours/days. (`_boot_ref` correctly keeps such an entry reapable after relaunch within the same OS boot — but only *if* the user relaunches.) This is the app-closed complement to the in-app orphan protections that are otherwise solid.
- **Fix sketch:** Have the detached agent self-enforce its own wall-clock deadline (a watchdog thread/`SIGALRM` in `orchestrator.agent`), independent of any external reaper, so the cap holds even with the app closed.
- **Cross-territory?** yes — Team A owns orchestrator/scheduled_runner.py + agent.py.

---

# Appendix A — Process census (every spawn site, all three languages)

Columns: **who spawns → what → reaper / kill path → on parent (Kim) crash**.

## Python
| Site | Spawns | Reaper / kill path | On parent crash |
|---|---|---|---|
| orchestrator/scheduled_runner.py:568 | detached agent (`Popen`, new session/pgroup) | `reap_orphaned_agents` wall-clock (1h) via `_kill_process_tree`/killpg, boot_ref-guarded | Reaper runs only on next in-app tick → **orphan window while app closed (F-J-6)** |
| orchestrator/codex_bridge_service.py:815 | codex-exec (`create_subprocess_exec`, new session/pgroup) | `_kill_process_tree` on timeout(1800s)/EOF + module `atexit` handler | atexit may not run on SIGKILL; pgroup leader → OS cleans on tty close |
| orchestrator/codex_appserver_transport.py:940 | codex app-server probe | `proc.kill()`+`wait()` after `communicate(timeout=15)` | short-lived; single-PID kill (not pgroup) |
| orchestrator/providers/browser/provider.py:911 | **visible Chrome + CDP port, detached** | **NONE** — `_chrome_proc` never killed/waited (**F-J-3**) | **Orphaned by design; zombie if spawner lives** |
| orchestrator/providers/ollama.py:587 | `ollama ps` (`subprocess.run`) | synchronous, `timeout=10` | n/a (completes inline) |
| orchestrator/mcp_client.py:175 | MCP servers (`stdio_client`) | `AsyncExitStack` close on normal exit; stdin-EOF | agent pgroup kill reaps them; else stdin close → child EOF |
| mcp_server/tools/code.py, git.py | run_python/run_node/git (`create_subprocess_exec`) | awaited `communicate` + `wait_for` timeout, killed on timeout | child of MCP server → reaped with it |

## Rust (desktop)
| Site | Spawns | Reaper / kill path | On parent crash |
|---|---|---|---|
| spawn_supervisor.rs:56 | agent/codex task (tokio, `process_group(0)`) | `cancel_task` SIGTERM→SIGKILL to `-pid` (killpg); `supervise` awaits `wait()` + `clear_if_pid` | pgroup leader → OS reaps tree on exit |
| lib.rs:970 | Chrome CDP (`StdCommand`) | stored in `CDP_CHROME_CHILD`; `kill_cdp_chrome()` on `RunEvent::Exit`; prior child reaped before overwrite (M-PROC-4) | Exit handler skipped on hard crash → Chrome lingers |
| run_history.rs:231/271/307/339 | git/pip (`.output()`) | synchronous, **no timeout** (**F-J-5**) | blocks a tokio worker; no kill |
| ollama.rs:197/344/638 | ollama probes (tokio `.output()`) | awaited output; some with `tokio::time::timeout` | short-lived |
| lib.rs osascript/pbcopy/pbpaste, session_commands.rs open/explorer/xdg-open, codex_projects.rs | OS helpers | fire-and-forget short-lived | n/a |
| task_runtime.rs:351/383 (`cat`) | **test-only** (`#[tokio::test]`) | n/a | n/a |

## Rust (cli)
| Site | Spawns | Reaper / kill path |
|---|---|---|
| cli/src/agentic.rs:250, commands.rs:1085, repl_turn.rs | agent/slash subprocesses | `kill_on_drop(true)` (+ SIGTERM-then-SIGKILL graceful path in repl_turn) |

**Census takeaways:** the two spawn sites *without* a robust reaper are **F-J-3** (Python CDP Chrome — none at all) and **F-J-6** (scheduled agent — reaper is in-app only). The Rust CDP Chrome (lib.rs) and the tokio task child (spawn_supervisor) are both correctly reaped. `atexit`/`RunEvent::Exit` reapers do not fire on SIGKILL, but pgroup-leader spawns let the OS clean the tree.

---

# Appendix B — Timeout table (network / subprocess / wait calls)

| Call | File:line | Timeout | Clamped? |
|---|---|---|---|
| codex-exec task run | codex_bridge_service.py:856 | 1800s (config `codex_bridge.task_timeout_s`) | config-bounded |
| codex pipe-close wait | codex_bridge_service.py:880 | 30s (`_EXEC_WAIT_TIMEOUT_S`) | fixed |
| codex app-server probe | codex_appserver_transport.py:940 | 15s | fixed |
| HITL decision wait | codex_bridge_service.py:321 | 120s | fixed |
| scheduled-agent wall clock | scheduled_runner.py:57 | 3600s | fixed (in-app only — F-J-6) |
| runner cross-proc lock | scheduled_runner.py:56 | 30s | fixed |
| MCP session init | mcp_client.py:198 | 30s | fixed |
| Gemini OAuth request | gemini.py:248 | 180s | fixed |
| Ollama chat stream | ollama.py:448 | 600s read / 10s connect | fixed |
| Ollama probes | ollama.py:282/349/601 | 10–20s | fixed |
| Browser CDP connect (py) | provider.py:260 | 15s | fixed |
| Bridge send | bridge_client.py:144 | 30s | fixed |
| Bridge result poll | bridge_client.py:215/286 | `_BRIDGE_TIMEOUT_S` | fixed |
| `run_shell` | shell.py:504 | `_clamp_shell_timeout`, cap 600s | **clamped** ✓ |
| **`run_python`/`run_node`** | code.py:285/350 | `int(args.get("timeout", CODE_TIMEOUT))` | **UNCLAMPED — Team C F-C-5** |
| `run_command` (code.py shell) | code.py:412 | `int(args.get("timeout", SHELL_TIMEOUT))` | **UNCLAMPED — F-C-5 sibling** |
| **`web_wait_for`/`_for_url`** | navigation.py:215/237 | `int(args.get("timeout_ms",10000))` | **UNCLAMPED — F-C-5** |
| git tool | git.py:60 | `SHELL_TIMEOUT` (default) | model can override, see F-C-5 |
| gh CLI | github.py:96 | 15–45s | fixed |
| web actions (click/fill/nav) | web/actions.py, navigation.py | 5–25s | fixed |
| Rust CDP TcpStream connect | lib.rs:936 | OS default (blocking connect) | none (short local) |
| Rust CDP connect_over_cdp | web/browser.py:375 | `_CDP_CONNECT_TIMEOUT_MS` (10s env) | env-bounded |
| **Rust `run_update` git/pip** | run_history.rs:231/271/307/339 | **NONE** | **UNBOUNDED — F-J-5** |

**Timeout takeaways:** the user-facing calls lacking any effective bound are code.py's `run_python`/`run_node`/`run_command` and `web_wait_for` (all F-C-5 — a model-supplied `timeout` of 10^9 wedges the MCP call for days), plus Rust `run_update` (F-J-5). `run_shell` in shell.py is the one path that *does* clamp — the fix pattern to copy.

---

# Appendix C — Clean / well-handled (do not re-flag)
- **Memory arrays are bounded:** `ConversationMemory` (max_messages=40, memory.py), `useCappedState`/`MAX_ACTIVITY_ITEMS=300` for activity + liveHistory, `context_meter._tools_token_cache` (single-entry, cleared before insert). The one *un*capped array is `runHistory` (**F-J-2**).
- **Bridge maps GC'd:** `WEBVIEW_BRIDGE_RESULTS/PROGRESS/WAS_HIDDEN` have a running TTL sweeper (`start_bridge_gc_sweeper`, lib.rs:1147) keyed off `WEBVIEW_BRIDGE_ENTRY_TIMES`.
- **PID registry is self-pruning + PID-reuse-safe** via `_boot_ref` + atomic replace (scheduled_runner.py).
- **Screenshots are in-memory base64** (`handle_take_screenshot` → data URI; no orchestrator disk temp to accumulate).
- **Session files rotate** at `_MAX_SESSION_BYTES` (session_store.py); daily `kim_*.jsonl` logs have `apply_log_retention` (7d) — but **not** `scheduled_runs/*.log` (**F-J-1**).
- **Reserve-slot race closed** (spawn_supervisor `reserve_slot` atomic check-and-set); **scheduler overlap** guarded by `SCHEDULER_TICK_ACTIVE` AtomicBool + TaskRuntime lock.
- **Already filed elsewhere (referenced, not re-filed):** F-D-5 (stdout forwarder no back-pressure), F-C-6 (code.py no pgroup kill), F-C-5 (unclamped code/web timeouts), F-B-8 (non-idempotent browser retries), F-F-11 (whole-history re-render — compounds F-J-2), F-I-4 (unauth CDP 9222 — compounds F-J-3).
