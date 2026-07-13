> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Kim — Agent Harness Roadmap

Prioritized roadmap for the Python orchestrator, MCP server, and Codex bridge
as a coherent agent execution harness.  Code-quality refactors are tracked in
`IMPROVEMENT_PLAN.md`.  This file is specifically about *agent runtime
capabilities*: hardening, new features, and platform direction.

---

## Standing constraint (never relax)

> Code tab must NEVER use OpenAI auth or gpt-5.5.
> Only ollama cloud model or browser provider.

---

## Tier 1 — Immediate hardening (completed this session)

These landed on branch `kim-improvement` and are pending commit.

| Area | Fix | File |
|------|-----|------|
| Shell injection | Added `\n` and `\r` to `_CHAIN_METACHAR_RE` — POSIX shell treats newline as command separator | `mcp_server/tools/shell.py` |
| Gemini import | Lazy `google-generativeai` import with `_GENAI_AVAILABLE` flag; `genai.configure()` deferred to call time | `orchestrator/providers/gemini.py` |
| AgentTermination | Typed enum (7 values) + `make_run_result()` with `"termination"` field in return dict | `orchestrator/agent_states.py` |
| Session durability | `SessionStore.flush()` no-op sync barrier so callers can always call flush() safely | `orchestrator/session_store.py` |
| Ollama schema | `_tool_result_message()` uses `"tool_call_id"` not `"tool_name"` — correct OpenAI-compatible spec | `orchestrator/providers/ollama.py` |
| Codex subprocess | `finally` block in `run_codex_subtask` now calls `kill()` + `await asyncio.wait_for(process.wait(), 5)` on all non-normal exits | `mcp_server/tools/codex_bridge.py` |

All covered by 221 Python tests (5 skipped). These patches are Python-only;
the branch also contains pre-existing frontend changes (App.tsx, ChatView.tsx,
parsers.ts, parsers.test.ts, config.rs) that are not part of this hardening batch.

---

## Tier 2 — Medium features (next 2–4 sessions)

### 2a. Codex stderr surfacing ✅ COMPLETE

**Implemented:** `_drain_stderr_to()` in `mcp_server/tools/codex_bridge.py`
reads Codex subprocess stderr in real time, appends the full line to the
non-zero-exit summary buffer, and immediately prints each non-empty line as:

`[STATUS] codex: {line}`

Printed lines are truncated for UI safety, while the accumulated stderr buffer
keeps full untruncated lines for exit summaries.  Covered by
`tests/test_codex_stderr_drain.py`.

### 2b. Typed agent termination surfacing to Tauri ✅ COMPLETE (transport slice)

**Implemented:**

`make_run_result()["termination"]` carries a typed `AgentTermination` value
(`"task_complete"`, `"cancelled"`, `"max_iterations"`, `"stuck"`,
`"provider_failed"`, `"need_help"`, `"conversational_loop"`).  `cli.py` now
prints two end-of-run records before the final summary:

1. Structured JSON for typed IPC:
   `{"type":"run_done","termination":"...","success":true|false}`
2. Legacy human-readable status:
   `[STATUS] run ended: {termination}`

`desktop/src-tauri/src/subprocess.rs` parses `run_done` into
`KimEvent::RunDone` and emits `kim:run-done` with
`{ termination, success }`, while preserving the existing boolean
`kim-agent-done` event for backward compatibility.  `useChatStream.ts`
captures the typed termination reason in a ref and `parseAgentLine()` silently
drops the raw lifecycle JSON from the legacy `kim-agent-output` stream so it
does not leak into the activity feed.

**UI state:** the typed termination reason is captured in `useChatStream.ts` and
is used on failed runs to choose a more specific retry banner when no provider
error code is available.  Full per-termination visual treatment can still be
expanded later, but the generic `agent-error` fallback is no longer the only UI
path.

### 2c. MCP tool timeout coverage audit ✅ COMPLETE

**Audit result (all 31 tools inspected):**

- `shell.py`, `git.py`, `search.py`, `code.py`, `codex_bridge.py` — explicit
  `asyncio.wait_for` + `kill/wait` guards already in place. ✓
- `github.py` — all `gh` CLI subprocess calls carry explicit `timeout=` args
  (15–45 s). ✓
- `web.py` (Playwright browser tools) — every `page.goto`, `page.click`,
  `locator.fill`, `page.wait_for_load_state`, `page.wait_for_url`,
  `page.go_back` passes explicit Playwright `timeout=` in milliseconds. ✓
- No raw HTTP client (`aiohttp`, `httpx`, `requests`) exists in any MCP tool.
  Kim does **not** implement a `web_fetch` tool — the name only appears in the
  Codex tool allowlist. ✓

**One gap fixed:** `_connect_over_cdp()` in `mcp_server/tools/web.py` called
`chromium.connect_over_cdp(url)` without an explicit `timeout=`.  Playwright's
implicit 30 s default, combined with the `_ensure_browser()` retry loops
(up to 12 call sites × 2 hosts × 30 s), created a worst-case ~720 s
(12-minute) hang during browser initialisation.  Fixed by adding
`timeout=_CDP_CONNECT_TIMEOUT_MS` (default 10 000 ms, overridable via
`KIM_CDP_CONNECT_TIMEOUT_MS`).  Covered by `tests/test_web_cdp_timeout.py`.

### 2d. Session replay integrity ✅ COMPLETE

**Implemented:** `SessionStore.append_run_result(result, cwd=None)` writes a
typed final JSONL record on every run completion path:

`{"type":"run_result","session_id":"...","completed_at":"...","success":...,"termination":"...","summary":"...","had_screenshot":false,"cwd":"..."}`

`agent.run()` routes all terminal return paths through `_complete_run()`, which
persists the result and guards against session-store I/O errors so the caller
still receives the result.  The CLI no longer appends a duplicate result record.
`ConversationMemory.load_from_messages()` skips non-message records without a
`role`, so resumed sessions ignore `run_result` safely.

Covered by `tests/test_session_store.py` static and round-trip tests, including
the no-tools early exit path.

### 2e. Provider-level error normalization ✅ COMPLETE (transport slice)

**Current state (partially landed):**

`orchestrator.providers.base` now defines `ProviderError(code, message,
retryable)` plus `classify_provider_error(error)`.  The classifier preserves
existing provider exception messages while producing stable codes such as
`"auth"`, `"rate_limit"`, `"server_error"`, `"timeout"`, `"network"`,
`"invalid_request"`, and `"unknown"`.

The agent retry boundary now delegates retry decisions to this classifier,
emits `[STATUS] provider error: {code}` for the legacy stream, and also emits
structured JSON when provider calls fail after retry exhaustion:
`{"type":"provider_error","code":"rate_limit","retryable":false}`.

`desktop/src-tauri/src/subprocess.rs` parses this as
`KimEvent::ProviderError` and emits `kim:provider-error` with
`{ code, retryable }`.  `useChatStream.ts` captures the most recent provider
error code and uses it to render a specific retry banner for auth, rate limit,
server, timeout, network, invalid-request, and unknown provider failures.
`parseAgentLine()` drops the raw JSON from the legacy stream.

This fixes a real bug where `PermissionError` (an `OSError` subclass) was
retried as a network error, causing auth failures like "Sign in to Ollama" to
waste retry cycles instead of surfacing the actionable fix immediately.

**Remaining UI gap:** provider error codes use the existing retry banner slot.
A richer future UI could add dedicated icons/actions per code (for example,
directing auth failures to provider sign-in), but basic user-facing surfacing is
now present.

---

## Tier 3 — Platform direction (bigger, requires design confirmation)

### 3a. Durable agent traces (from OpenAI Agents SDK direction) ✅ COMPLETE (first slice)

**Implemented:** Lifecycle and tool-call trace methods added to `SessionStore`:

- `append_run_started(task, cwd=None)` — writes `{"type": "run_started", session_id,
  started_at (ISO UTC), task, cwd}` as the first record of a run.
- `append_run_result(result, cwd=None)` — existing method, extended with a `cwd`
  field so both bookend records carry location context.
- `append_tool_event(tool_name, phase, arg_keys=None, duration_ms=None, error=None,
  cwd=None)` — writes `{"type":"tool_call", ...}` records for tool execution.
  Only argument keys are persisted, never argument values or tool output.
- `append_llm_event(phase, provider=None, attempt=None, message_count=None,
  tool_count=None, duration_ms=None, usage=None, error_code=None, cwd=None)` —
  writes `{"type":"llm_turn", ...}` records for provider attempts.  Prompt text,
  tool schemas, and model output are intentionally omitted; only counts,
  timings, compact usage metadata, and normalized error code are kept.
- `load_trace_events(session_id, event_type=None, tool_name=None, phase=None)` —
  reads typed trace records without replaying the conversation and supports
  filtering by record type, tool name, and tool phase.
- `iter_trace_events(event_type=None, tool_name=None, phase=None, limit=None)` —
  scans session JSONL files across date directories, newest date first, and
  annotates records with source `session_id` / `date` when missing.
- `summarize_trace_events(session_id=None)` — returns compact counts by trace
  type, tool phase/name, LLM phase/provider/error code, and final run outcome
  for one session or all sessions.

Events are written to the existing session JSONL (`kim_sessions/<date>/<id>.jsonl`)
alongside conversation messages — **no separate trace file**.  `run_result` is
already written at every termination path via `_complete_run()`; adding
`run_started` gives the session file full lifecycle bookends without duplication.

`agent.run()` calls `append_run_started(task)` right after the compact-control
guard, wrapped in try/except so a trace-write failure never aborts the run.
`_execute_tool()` writes `tool_call` records for `started`, `completed`, and
`errored` phases, including `duration_ms` on terminal records.  Trace writes are
defensive: a session-store failure logs a warning but does not break the tool
call.
`_call_with_retry()` writes `llm_turn` records for provider-attempt `started`,
`completed`, and `errored` phases.  Completed records include compact provider
usage when available; errored records include the normalized provider error
code.
`load_from_messages` already skips records without a `"role"` key (line 73 of
`orchestrator/memory.py`), so resumed sessions ignore `run_started`,
`tool_call`, `llm_turn`, and `run_result` records.

Covered by `tests/test_session_store.py` (run_started JSONL shape, required
fields, ISO timestamp, no screenshot bytes, cwd field in run_result, tool_call
shape, arg-key-only persistence, error truncation, llm_turn shape, compact usage
sanitizing, per-session and cross-session trace filtering, missing lookup
behavior, trace summaries, resume skipping, and agent static scans).

**Remaining scope** (for a later session):
- A persistent index is still optional future work if JSONL scans become too
  slow at large session counts.

### 3b. Sandboxed shell execution (from OpenAI Agents SDK direction) ✅ COMPLETE (first slice)

**Implemented:** `mcp_server/tools/shell.py` now supports opt-in sandboxed
execution for `run_command` and `run_powershell`.

Enable globally with config:

```yaml
shell:
  sandbox_mode: true
```

or via env override:

```bash
export KIM_SHELL_SANDBOX_MODE=true
```

Tool calls can also pass `sandbox_mode: true` for per-call opt-in.  When enabled,
commands run in a fresh temporary directory with a minimal environment and
restricted `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`).  The requested `cwd` is
ignored in sandbox mode, so relative writes land in the temporary sandbox and
are removed after the command completes.  Default behavior is unchanged when
sandbox mode is unset.

This is not a filesystem namespace or container: absolute paths are still
visible to the process.  It is a low-risk first slice that isolates cwd and env
while preserving the existing deny-list and chaining guards.  Covered by
`tests/test_shell_command_blocking.py` sandbox tests.

### 3c. Skills / progressive tool disclosure (from Odysseus and Claude Code) ✅ COMPLETE (first slice)

**Implemented:**

- `mcp_server/tool_tiers.py` — pure-stdlib filtering module.  `parse_enabled_tiers`
  normalises the env-var value; `filter_tools` maps tier names → tool name sets
  (unknown tiers log a WARNING and contribute nothing); `get_active_tool_names`
  reads `KIM_ENABLED_TOOL_TIERS` and returns a frozenset or `None` (all tools).
- `mcp_server/tool_registry.py` — `TIER_DISPATCH` constant (11-entry dict) maps
  each granular tier name to the existing `_*_DISPATCH` dict whose keys are that
  tier's tool names.  `TOOLS` and `DISPATCH` exports are unchanged (default
  behaviour preserved).
- `mcp_server/server.py` — calls `get_active_tool_names(TIER_DISPATCH)` at startup
  to filter `_TOOLS` / `_DISPATCH` before connectors are merged.  Unset env var
  → all tools exposed as before.

**Tiers:**

| Granular | Tools |
|----------|-------|
| `file` | read_file, write_file, list_dir, delete_file |
| `shell` | run_command, run_powershell |
| `screen` | take_screenshot, get_screen_info, observe_ui, click_ui, take_annotated_screenshot |
| `web` | web_open, web_observe, web_resolve, web_click, web_fill, web_press, web_text, web_screenshot, web_wait_for, web_wait_for_url, web_back, web_close |
| `mouse` | click, double_click, right_click, drag, scroll |
| `keyboard` | type_text, hotkey, key_press |
| `windows` | get_windows, focus_window, resize_window, open_url |
| `git` | git_status, git_diff, git_add, git_commit, git_log, git_checkout, github_create_repo |
| `code` | run_python, run_node, lint_file |
| `search` | search_in_files, find_files |
| `memory` | write_memory, read_memory |

**Compound aliases** (expand to component tiers):

| Alias | Expands to |
|-------|-----------|
| `core` | file, shell, search |
| `ui` | screen, mouse, keyboard, windows |
| `browser` | web |

Example: `export KIM_ENABLED_TOOL_TIERS=core,git` exposes file + shell + search + git tools only.

Covered by `tests/test_tool_tiers.py` (44 tests: parse, filter, alias expansion,
env-var isolation, static registry/server/tiers source scans, 4 live-import
coverage tests skipped in Python 3.9 test env).

### 3d. Persistent agent memory (from Odysseus) ✅ COMPLETE

**Implemented:** `mcp_server/tools/memory.py` — two MCP tools:

- `write_memory(key, value, cwd=None)` — store a named finding
- `read_memory(key=None, cwd=None)` — retrieve one key or list all

Storage: `kim_memory/<basename>-<hash8>.json` under `PROJECT_ROOT`.  Each
file is a plain JSON dict (human-readable, no embeddings, no vector DB).
Scoped by `cwd`: different project directories produce different files; the
filename embeds the cwd's last path component + an 8-char MD5 hex for
collision resistance (e.g. `kim-pro-a1b2c3d4.json`).  Values are capped at
16 384 chars.  Writes are atomic (temp-file + `os.replace`).

Both tools wired into `mcp_server/tool_registry.py` (`_MEMORY_TOOLS`,
`_MEMORY_DISPATCH`).  Covered by `tests/test_memory_tools.py` (25 tests:
write/read roundtrip, accumulation, overwrite, validation, cwd scoping,
filename stability, listing, truncation, registry static scan).

### 3e. Scheduled / cron agent actions (from Odysseus) -- COMPLETE (first slice)

Odysseus supports notes/tasks with reminders and scheduled agent actions.

**Implemented:** `orchestrator/cron_store.py` -- pure data model and inspectable
JSON store for scheduled agent tasks.

- `ScheduledTask` dataclass: `id`, `task`, `schedule_expr`, `provider` (optional),
  `enabled`, `created_at`, `updated_at`.  Serialises/deserialises cleanly via
  `to_dict()`/`from_dict()`.
- `CronStore` -- CRUD store backed by a single atomic-write JSON file
  (`kim_schedules.json` at the Kim root), keyed by task id for O(1) lookup.
  Methods: `add()`, `get()`, `update()`, `delete()`, `list_tasks(enabled_only=)`.
  Corrupt or missing file returns an empty store and logs a warning rather
  than raising.
- `parse_schedule_expr(expr) -> timedelta` -- validates on `add()`/`update()`.
  Supported: `@hourly`, `@daily`, `@weekly`, `@every <N>m/h/d`.  Raises
  `ValueError` with a clear message for unrecognised patterns or `@every 0` etc.
  `add()` and `update()` store the stripped expression so surrounding whitespace
  is never persisted.  `enabled` must be a literal `bool`; non-bool values
  (e.g. the string `"false"`) are rejected with a clear error.
- `next_run_after(expr, after=None) -> datetime` -- pure interval calculation
  (`after + timedelta`), always UTC-aware.  No calendar alignment (midnight
  snapping etc.) -- that is an executor concern.

No background runner is included in this slice: Tauri enforces one-task-at-a-time
and has no timer/cron loop; `task_queue.py` is dormant.  Execution is deferred
to the next slice once the Tauri wiring is designed.

**Scheduling state slice (second slice, also complete):** `ScheduledTask`
extended with three optional execution-state fields (`run_count`, `last_run_at`,
`next_run_at`) defaulting to never-run values; `from_dict` is backward-compatible
with old JSON entries that lack these fields.  Two new `CronStore` methods:

- `record_run(task_id, ran_at=None) -> ScheduledTask | None` -- increments
  `run_count`, sets `last_run_at`, computes and stores `next_run_at` as
  `last_run_at + interval`.  Validates the entry upfront; returns None and does
  not modify the file if the entry is corrupt or the schedule_expr is invalid.
- `due_tasks(as_of=None) -> list[ScheduledTask]` -- returns enabled tasks whose
  effective due time is <= as_of.  Due-time policy: tasks with `next_run_at`
  set use it directly; never-run tasks use `created_at + interval` (i.e. first
  due one full interval after creation, not immediately).  Disabled and corrupt
  entries are skipped.  Results are ordered by due time ascending, ties broken
  by task id for determinism.

Also in this slice: `_parse_utc_iso` (parse ISO string -> UTC-aware datetime,
naive treated as UTC) and `_effective_next_run` (compute due datetime for one
task) as internal module helpers.

Covered by `tests/test_cron_store.py` (93 tests: all prior coverage plus
backward-compat load with defaults, record_run semantics, naive ran_at, corrupt
entry no-save guarantee, persistence across store instances, first-run boundary
test at T+59m59s/T+60m, next_run_at due/not-due, disabled exclusion, corrupt
skip in due_tasks, ascending ordering, id tiebreak, naive as_of; UTC
normalisation: _parse_utc_iso aware non-UTC->UTC, record_run aware non-UTC
ran_at stored as +00:00, due_tasks aware non-UTC as_of; run_count bool guard).

**Slice 3 -- kimctl management surface -- COMPLETE**

`kimctl schedule` subcommands (Python, `kimctl/__main__.py`).  All local
file ops; no bridge required; output is structured JSON with `--json` flag,
matching the existing kimctl `chats`/`show` pattern.

Subcommands:
- `schedule list [--enabled-only] [--json]`
- `schedule add <task> <expr> [--provider NAME] [--disabled] [--json]`
- `schedule update <id> [--task TEXT] [--expr EXPR] [--provider NAME] [--enable|--disable] [--json]`
- `schedule delete <id> [--json]`
- `schedule due [--as-of ISO_DATETIME] [--json]`
- `schedule record-run <id> [--at ISO_DATETIME] [--json]`

Provider constraint visible at the CLI: `add` warns on stderr if provider
is openai/* or gpt*; enforcement is the executor's responsibility.

Store path isolation: `cmd_schedule` passes `KIM_SCHEDULES_FILE` env var
(or None) directly to `CronStore(store_file=...)`, so the env-var override
used in tests falls through to CronStore's built-in default when unset --
no duplicated path logic.

Covered by `tests/test_kimctl_schedule.py` (34 tests: Namespace-based tests
for all six subcommands -- list/add/update/delete/due/record-run -- covering
happy path, error exits, --json output, --enabled-only, provider warning,
enable/disable cycle, conflict detection, invalid-input exits; plus
parser-wiring tests for each subcommand that call build_parser().parse_args()
and verify argparse dest names match handler getattr calls).

**Slice 4 -- background executor foundation -- COMPLETE**

`orchestrator/scheduled_runner.py` -- pure Python executor module:

- `is_allowed_provider(provider)` -- allowlist guard: empty (→ ollama), `ollama`,
  `ollama-cloud`, `browser`, `browser:<site>` are allowed; all other providers
  (openai, gpt*, claude, gemini, deepseek, …) are refused with a structured error.
  This is stricter than the per-`add` warning: a denylist at add time plus an
  allowlist at execution time is the correct defence-in-depth approach.

- `RunDueResult` dataclass (`task_id`, `task_text`, `launched`, `recorded`,
  `skipped`, `skip_reason`, `error`) with `to_dict()` for JSON output.

- `run_next_due_task(store_file, dry_run, kim_root, as_of, session_dir)` --
  discovers the first due task, applies the provider allowlist, spawns
  `orchestrator.agent` via `subprocess.Popen` (same binary path as Tauri
  `send_task`), and calls `store.record_run()` immediately on successful spawn
  (not gated on exit) to advance `next_run_at` and prevent re-firing.
  Spawn failures return a `RunDueResult` with `error` set; record_run is skipped.

`kimctl schedule run-due [--dry-run] [--json]` -- new subcommand wired into
`kimctl/__main__.py`.  `--dry-run` shows what would run without spawning.
`--json` returns structured `{"ok": bool, ...RunDueResult fields...}`.

`desktop/src-tauri/src/schedule_commands.rs` (NEW) -- Tauri IPC commands:

- `list_due_scheduled_tasks(as_of: Option<String>) -> Result<String, String>` --
  shells out to `python -m kimctl schedule due --json [--as-of ...]`, returns
  raw JSON array string to the frontend.  All due-time logic stays in Python.

- `run_due_scheduled_task(dry_run: bool) -> Result<String, String>` --
  shells out to `python -m kimctl schedule run-due --json [--dry-run]` and
  returns the raw JSON result object string (`RunDueResult.to_dict()` fields:
  `task_id`, `task`, `launched`, `recorded`, `skipped`, `skip_reason`,
  `error`).  On non-zero exit, prefers stderr then stdout before a generic
  fallback so JSON-format errors from kimctl surface correctly.

  Pure helper `build_run_due_args(python, dry_run) -> Vec<String>` extracted
  for unit-testability.  The inner sync helper `run_due_with_interpreter` is
  separated from the async Tauri wrapper so tests do not need a runtime.

Both commands registered in `generate_handler![]` in `lib.rs`.

Provider policy document:

| Provider hint | Allowed for scheduled execution | Notes |
|---|---|---|
| empty / None | ✓ | executes as ollama |
| ollama | ✓ | |
| ollama-cloud | ✓ | |
| browser | ✓ | requires Kim app + bridge running |
| browser:gemini / browser:chatgpt / etc. | ✓ | requires Kim app + bridge running |
| openai, openai-* | ✗ | refused at spawn; never relayed |
| gpt-*, GPT-* | ✗ | refused; covers gpt-5.5 specifically |
| claude, gemini, deepseek, anthropic, … | ✗ | refused; not replicated by this executor |

Limitation: `run_next_due_task` fires `orchestrator.agent` (Kim Chat-tab path),
not Tauri `send_task`.  It does not replicate browser-bridge env injection or
Google OAuth env setup.  "browser" provider scheduled tasks therefore require
the Kim app to be running with the bridge active; ollama/empty are the fully
standalone-safe defaults.  This is intentional for the foundation slice.

Interpreter resolution: `find_interpreter(kim_root)` mirrors Tauri's
`find_python_interpreter` preference order — `venv/bin/python`,
`.venv/bin/python`, Windows `Scripts/python.exe` equivalents, then `python3`/
`python` on PATH.  The function is called before preflight so tests can inject
a specific interpreter via `_interpreter_override` without touching the
filesystem.

Preflight: `_preflight(python, kim_root, env)` runs
`python -c "import mcp; import orchestrator.agent"` with a 10 s timeout
before any `Popen` or `record_run` call.  A `ModuleNotFoundError` (e.g.
system Python lacking project deps), `TimeoutExpired`, or interpreter
`OSError` all return a descriptive error string; `run_next_due_task` returns
the result with `error` set and `launched=False`.  `record_run` is never
called on preflight failure.

Covered by `tests/test_scheduled_runner.py` (47 tests: allowlist parametrize,
find_interpreter venv/dot-venv preference, no-due-tasks, provider refused,
dry-run, preflight failure + no-record-run, preflight timeout, preflight
OSError, spawn failure + no-record-run, successful launch + run-count advance,
resolver used by Popen + preflight, empty-provider→ollama default, browser
allowed, to_dict shape); `tests/test_kimctl_schedule.py` +2 parser-wiring
tests for `run-due`; Rust test in `schedule_commands.rs`.

Test counts: Python 568 passed, 9 skipped; Rust 39 passed (all tests).

**Slice 4b -- full schedule management Tauri bridge -- COMPLETE**

`schedule_commands.rs` now exposes all six schedule management operations as
Tauri IPC commands.  All registered in `generate_handler![]` in `lib.rs`.

New commands added in this slice:
- `list_scheduled_tasks(enabled_only: bool)` -- `schedule list [--enabled-only] --json`
- `add_scheduled_task(task, expr, provider?, disabled)` -- `schedule add ... --json`
- `update_scheduled_task(id, task?, expr?, provider?, enabled?)` -- `schedule update ... --json`;
  `enabled: Some(true)` -> `--enable`, `Some(false)` -> `--disable`, `None` -> omitted
- `delete_scheduled_task(id)` -- `schedule delete <id> --json`

Shared `run_kimctl(args, kim_root)` helper deduplicates the subprocess +
error-handling pattern across all four new commands and `run_due_scheduled_task`.

Pure arg-builder helpers for all commands:
`build_list_args`, `build_add_args`, `build_update_args`, `build_delete_args`,
`build_run_due_args` -- each unit-tested for shape, flags, and interpreter
position without a subprocess.

ASCII self-check test: `test_source_is_ascii` uses `include_str!` to verify
no non-ASCII characters are ever introduced to this file.

+18 Rust tests (39 now, was 21): async-shape (4), build_list (2), build_add (4),
build_update (5), build_delete (2), ascii self-check (1); existing run-due (4)
and async-shape tests (2) preserved.

**Slice 4c -- desktop schedule management UI -- COMPLETE**

`desktop/src/components/settings/SchedulePane.tsx` adds a compact Settings pane
for scheduled tasks, wired to the Tauri IPC bridge:

- Lists scheduled tasks with enabled state, schedule expression, provider,
  run count, and next-run status.
- Adds tasks with task text, schedule expression, allowlisted provider select,
  and "start disabled" toggle.
- Enables/disables tasks through `update_scheduled_task`.
- Deletes tasks through `delete_scheduled_task`.
- Runs the next due task or dry-runs it through `run_due_scheduled_task`, with
  structured result/error display.

`RevampSettings` now includes a "Schedules" nav entry and renders the pane.
`App.tsx` and `RevampSidebar` settings-pane unions include `schedule`.

Covered by `desktop/src/components/settings/__tests__/SchedulePane.test.ts`
(7 pure helper tests for JSON parsing, invoke error extraction, and timestamp
formatting).  Desktop Vitest now has 20 passing tests; production build passes.

**Slice 4d -- opt-in periodic schedule timer foundation -- COMPLETE**

`schedule_commands.rs` now has an opt-in Tauri timer foundation:

- `start_schedule_timer(interval_seconds?)` starts a single periodic loop.
  It clamps intervals below 60 seconds to 60 seconds and refuses duplicate
  loops by returning the existing status.
- `stop_schedule_timer()` aborts the active loop if present.
- `get_schedule_timer_status()` returns `running`, `interval_seconds`,
  `tick_count`, `last_result`, and `last_error`.
- The loop calls the existing `kimctl schedule run-due --json` path through
  `run_due_once`, so provider safety stays enforced by
  `orchestrator/scheduled_runner.py` and scheduled tasks cannot use OpenAI/gpt
  variants.
- The timer is not auto-started; callers must explicitly start it.

Registered in `lib.rs` with managed `ScheduleTimerState`.

Covered by Rust unit tests for interval clamping, idle status defaults, and
status result/error carrying.  Rust lib tests now have 42 passing tests.

**Remaining scope (next slice):**
**Slice 4e -- timer controls in Schedules pane -- COMPLETE**

`SchedulePane.tsx` now exposes the opt-in timer controls:

- Interval input with 60-second minimum guidance from the backend clamp.
- Start, Stop, and Status actions wired to `start_schedule_timer`,
  `stop_schedule_timer`, and `get_schedule_timer_status`.
- Shows running/stopped state, interval, tick count, last error, and last
  successful tick summary.

`parseTimerStatus` adds a typed frontend guard for timer command responses.
Covered by two additional Vitest helper tests.  Desktop Vitest now has 22
passing tests; production build passes.

**Slice 4f -- timer tick event/toast surface -- COMPLETE**

The opt-in schedule timer now emits `schedule-timer-tick` after every tick.
The payload includes `tick_count`, raw `result`, and `error`.  `App.tsx`
listens globally and surfaces only meaningful events:

- Timer errors are shown as error toasts.
- Scheduled task launch events are shown as success toasts.
- No-op ticks such as "no tasks due" stay quiet.

Covered by an additional Rust unit test for tick event shape.  Rust lib tests
now have 43 passing tests.  Desktop Vitest remains at 22 passing tests and
production build passes.

**Slice 4g -- timer persistence policy -- COMPLETE**

Timer persistence is explicit opt-in:

- `Settings.schedule_timer` stores `{ enabled, interval_seconds }` in the
  existing `kim-settings` localStorage blob.
- Defaults keep the timer off (`enabled: false`, interval 300 seconds).
- On app launch, `App.tsx` starts the timer only when the persisted flag is
  enabled.
- In the Schedules pane, Start persists enabled+interval; Stop disables the
  saved preference.

This keeps the scheduler from becoming a surprise daemon while still letting
users opt into restart-persistent scheduled work.

**Slice 4h -- safe provider comparison foundation -- COMPLETE**

Kim now has `orchestrator.compare.compare_providers`, a deliberately scoped
comparison harness:

- Runs the same task through providers sequentially.
- Gives each run its own MCP server subprocess, avoiding shared stdio state.
- Captures success, summary, termination, duration, and provider errors.
- Persists comparison results in `kim_comparisons/` for later inspection.

This is the safe first slice for Odysseus-style side-by-side model comparison.
The next slice can expose it through CLI/UI after choosing credential and
provider-selection UX.

---

## What Kim can learn from Odysseus without copying code

Odysseus is local-first and privacy-first.  Its design choices relevant to Kim:

| Odysseus pattern | Kim application |
|-----------------|-----------------|
| Explicit host exposure controls — agents can't silently phone home | Kim's `shell.py` blocking is correct; extend to provider config: warn if a non-local provider is used in a task that touches private files |
| Model comparison side-by-side | Kim already has multiple providers; `orchestrator.compare.compare_providers()` now provides a safe sequential foundation, with CLI/UI exposure as the next step |
| Deep research mode (web + synthesis) | Kim has `web_fetch` + browser provider; a `research_task(query)` orchestrator mode that chains web fetches → synthesis → summary is achievable within the current architecture |
| Memory/skills import-export | See 3d above; the key Odysseus insight is that memory should be portable and inspectable, not hidden in embeddings |
| Mobile/PWA parity via relay | Kim's relay server already exists; Odysseus-style mobile parity means making the relay path a first-class product surface, not just an experimental feature |

---

## What NOT to take from Odysseus

- Calendar/email integrations — Kim is an engineering agent platform; general
  productivity integrations are scope creep.
- Document editor — Kim is not a notes app; the session store + JSONL traces
  are the right persistence primitive.
- Embedding-based semantic search — adds a dependency (vector DB) with
  unclear benefit over simple grep-based tool search in an engineering context.

---

## Relationship to IMPROVEMENT_PLAN.md

| This file | IMPROVEMENT_PLAN.md |
|-----------|---------------------|
| Agent harness capabilities | Code quality + architecture |
| New runtime behaviors | Refactors of existing code |
| Platform direction / features | Phase gates (-1 through 7) |

Tier 1 items in this file correspond to Python test coverage added in Phases
5c/5d of IMPROVEMENT_PLAN.md.  Tier 2 items are best executed after Phase 5
(test harness in place) and before Phase 6 (typed IPC, which several depend
on).  Tier 3 items require Phase 6 and 7 to be complete.
