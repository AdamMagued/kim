# TEAM E — Wave 1 findings (Rust CLI `cli/` + `kimctl/`)

Baseline: `integration/audit-fixes`. Read-only hunt. Format per OPERATION_GOOGLE_LEVEL §3.
Status: IN PROGRESS — preliminary batch committed early for resilience; findings are
appended (and re-severity-sorted at final pass) as the hunt proceeds.

## F-E-1: `kim doctor` always exits 0, even when every check fails
- **File:** cli/src/main.rs:289-297, cli/src/commands.rs:385-458
- **Severity:** Medium
- **Class:** bug | contract
- **Evidence:** `doctor()` builds human-readable status lines (`python: NOT FOUND`,
  `Ollama server: unreachable`, `Kim desktop bridge: not running`, model "not in the
  known list", etc.) and always returns `CommandOutcome::Message(...)`. `main()` prints
  it and falls through to `Ok(())` → exit 0. Concrete trigger: `kim doctor` on a machine
  with no python/codex/bridge → prints failures, exits 0. Install scripts and CI
  (`kim doctor && ...`) cannot gate on it. The unexpected-outcome arm
  (`other => eprintln!(...)`) also exits 0.
- **Fix sketch:** have `doctor()` return a pass/fail bit (or scan for known failure
  markers); `std::process::exit(1)` when any required check fails. Keep 0 when only
  optional checks (cargo, provider-specific) fail, or add `--strict`.
- **Cross-territory?** no

## F-E-2: unknown flags/subcommands silently start a fresh REPL session
- **File:** cli/src/main.rs:85-132 (`parse_cli_args`)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** anything that isn't `chat|code|doctor|--help|--version|--resume` falls
  into `CliCommand::Repl { resume_id: None }`. Concrete triggers: `kim resume latest`
  (missing dashes), `kim --continue`, `kim --resum latest` (typo), `kim login` — all
  silently open a brand-new interactive session instead of erroring. This is the same
  bug class as the fixed #6 (bare trailing `--resume`), one level up: the parser has no
  "unknown argument" rejection at all. A typo'd `--resume` loses the user's intent to
  resume and scatters a new session file. Also: `kim chat --resume abc` treats
  `--resume abc` as prompt text and sends it to the model.
- **Fix sketch:** reject any unconsumed `--flag` (and any non-subcommand bare word)
  with a UsageError (exit 2), mirroring the existing `--resume`-without-value handling.
- **Cross-territory?** no

## F-E-3: every session save rewrites all message timestamps to "now"
- **File:** cli/src/sessions.rs:125-143 (`save_session_messages_in`)
- **Severity:** Low
- **Class:** bug
- **Evidence:** the whole transcript is re-serialized on every save with a single
  `timestamp_ms = now` stamped onto every record. A 2-hour conversation saved after its
  last turn shows every message created at the final save instant; original per-message
  times are unrecoverable. Any future feature (or the desktop app reading these files)
  that trusts `timestamp_ms` gets fiction.
- **Fix sketch:** carry an optional `timestamp_ms` on `UiMessage`, set at push time,
  preserved on load and re-save; only stamp `now` for messages that lack one.
- **Cross-territory?** no

## F-E-4: one-shot `kim chat`/`kim code` exits 0 on FAILED agent runs and on Ctrl-C
- **File:** cli/src/main.rs:365-371 (`run_oneshot`), cli/src/agentic.rs:75-80,116,333-343, cli/src/repl_turn.rs:150-193
- **Severity:** High
- **Class:** bug | contract
- **Evidence:** three converging holes in the one-shot exit-code contract
  (`run_oneshot` only exits 1 when the LAST message has `MessageRole::Error`):
  1. The orchestrator ends a failed run with `{"type":"run_done","success":false}`
     then `[FAILED] <summary>` (orchestrator/cli.py:113-140). `parse_agent_line`
     maps `[FAILED] …` to `AgentLine::Answer` — identical to `[SUCCESS]` — so it
     becomes a normal Assistant `TextChunk`. The `success:false` in `run_done` is
     parsed and then explicitly discarded (`AgentLine::Done(_success)`,
     `let _ = saw_done;`). The python process exits 0, so no `Err` is emitted
     anywhere → `kim chat "do X"` on a run the agent itself declared FAILED
     exits 0.
  2. Ctrl-C during a one-shot run: `run_cancel_interrupt` keeps the partial
     answer, prints "(cancelled)", pushes NO Error message → exit 0 for an
     interrupted, incomplete run. (The F5 fix added an Error push for the
     git-decline cancel; the Ctrl-C cancel path was missed.)
  3. A child that dies after emitting output with exit 0 but no answer prints
     "Kim: (no response)" and exits 0.
  Desktop drift: the desktop uses the same `run_done` event to drive its
  kim:run-failed banner, so the SAME run shows as failed in the app and as
  success (exit 0) in scripts/CI using the CLI.
- **Fix sketch:** thread `run_done.success` into an `AppEvent` (e.g.
  `Done{success}`); have `run_oneshot` exit non-zero on `success=false`, on
  cancellation, and on no-response. Render `[FAILED]` answers with the Error role.
- **Cross-territory?** no (orchestrator emit side already correct)

## F-E-5: Ctrl-C in chat-mode agentic runs is SIGKILL-only — no graceful shutdown, session state and child cleanup skipped
- **File:** cli/src/main.rs:1194-1207 (pid slot is code-mode only), cli/src/repl_turn.rs:171-187, cli/src/agentic.rs:250 (`kill_on_drop`)
- **Severity:** Medium
- **Class:** bug | leak
- **Evidence:** the graceful interrupt ladder (SIGTERM → 5s drain →
  kill_on_drop SIGKILL) exists only for code mode: `stream_repl_turn` creates
  the `child_pid` slot only when `code_mode` is true. Chat-mode agentic runs
  (`python -m orchestrator.agent`) have `child_pid = None`, so Ctrl-C goes
  straight to `handle.abort()` → tokio `kill_on_drop` → SIGKILL. SIGKILL is
  untrappable: the orchestrator cannot flush its own session JSONL/checkpoint,
  cannot run provider cleanup, and its own children (MCP server subprocess,
  Playwright-launched Chrome for browser providers) are orphaned rather than
  shut down (stdio MCP servers exit on stdin EOF; a Playwright Chrome does not).
  Charter checklist item: "Ctrl-C mid-run must clean up child processes and
  write session state" — the CLI-side transcript is saved, the agent-side state
  is not.
- **Fix sketch:** populate the pid slot for the agentic path too and reuse
  `run_cancel_interrupt`'s SIGTERM+grace ladder (the orchestrator already
  handles SIGTERM as cancel — termination "cancelled").
- **Cross-territory?** no

## F-E-6: subprocess stdout line reads are unbounded — one oversized line buffers fully into RAM
- **File:** cli/src/provider/codex_stream.rs:200, cli/src/agentic.rs:272
- **Severity:** Low
- **Class:** perf | bug
- **Evidence:** both streaming loops use `BufReader::lines()` with no
  max-line-length cap. A single newline-less line from the child — e.g. a typed
  event carrying a base64 screenshot, a runaway tool output, or a corrupted
  stream — is accumulated entirely in memory before `next_line()` returns.
  There is no backpressure or truncation; a multi-hundred-MB line stalls the
  turn (no spinner-visible progress) while memory grows. Contrast:
  `preview_for_session` (sessions.rs) was already fixed to cap reads at 64 KiB
  for exactly this class of problem (F17).
- **Fix sketch:** read via a length-capped line reader (e.g. `read_until` with a
  cap, discarding/truncating past ~4 MiB) and surface "line too long" as an Err.
- **Cross-territory?** no

## F-E-7: `kimctl send --session <id>` reports instant success from a STALE `TASK_COMPLETE` in the session history
- **File:** kimctl/__main__.py:366-439 (`cmd_send` blocking poll)
- **Severity:** High
- **Class:** bug
- **Evidence:** the completion poll starts with `last_offset = 0` and scans the
  WHOLE session JSONL from the beginning. When `--session` resumes an existing
  session whose previous task already ended with `TASK_COMPLETE: …`, the very
  first poll (0.5s after POST /v1/task) matches the OLD completion line and
  exits 0 with the OLD summary — the new task has barely started. Every
  scripted "send follow-up task to the same session and wait" flow gets a wrong
  result. Same poll also matches `NEED_HELP:` from history → spurious exit 1.
- **Fix sketch:** initialize `last_offset` to the file's current size before
  (or right after) POSTing the task; only scan records appended afterwards.
- **Cross-territory?** no

## F-E-8: `kimctl send` poll advances the read offset past partially-written JSONL lines — completion can be permanently missed
- **File:** kimctl/__main__.py:399-414
- **Severity:** Medium
- **Class:** race
- **Evidence:** each poll does `f.seek(last_offset); new_data = f.read();
  last_offset = f.tell()`. If the read lands mid-write (the orchestrator's
  line is half-flushed), the partial line fails `json.loads` and is skipped —
  but `last_offset` already points past it. The next poll reads only the second
  half of the line, which also fails to parse. If that line was the
  `TASK_COMPLETE` record, the poll spins until `--timeout` and exits 2 for a
  task that succeeded.
- **Fix sketch:** only advance `last_offset` past the last newline-terminated
  line (`new_data.rfind('\n')`); keep the tail for the next poll.
- **Cross-territory?** no

## F-E-9: corrupt `cli-config.json` is silently reset to defaults, then clobbered — stored API keys destroyed without warning
- **File:** cli/src/config.rs:74-79 (`load_from`)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** any parse failure (hand-edit typo, truncated write from an old
  version, disk corruption) returns `Self::default()` with no message. The user
  now silently runs provider=ollama, and the next config save — `/theme`,
  `/model`, any login — atomically overwrites the corrupt file, permanently
  discarding every stored API key that was still recoverable in it. The user
  discovers this only when a later request fails auth.
- **Fix sketch:** on parse failure, rename the corrupt file to
  `cli-config.json.bak-<ts>` and print a one-line warning before using defaults.
- **Cross-territory?** no

## F-E-10: `/login ollama` and the model picker ignore `ollama_base_url` — remote-Ollama configs probe the wrong host
- **File:** cli/src/commands.rs:592-612 (`ollama_models` via `ollama list`), 785-808 (`ollama_server_models` hardcodes `http://127.0.0.1:11434`)
- **Severity:** Low
- **Class:** bug
- **Evidence:** config has `ollama_base_url` and doctor was fixed (A8,
  `ollama_models_at(base)`) to respect it — but the login path still probes the
  hardcoded localhost URL, and the `/model` picker shells out to the local
  `ollama list` daemon. A user pointing Kim at a remote/nonstandard-port Ollama
  gets "server is not running" from `/login ollama` and an empty/wrong model
  list, while doctor and actual chat requests work.
- **Fix sketch:** thread `config.ollama_base_url` into `ollama_server_models`
  and replace the `ollama list` shell-out with `ollama_models_at(base)`.
- **Cross-territory?** no

## F-E-11: `/login <provider>` key validation has no HTTP timeout — REPL hangs indefinitely on a stalled connection
- **File:** cli/src/commands.rs:984-1029 (`validate_api_key`)
- **Severity:** Low
- **Class:** bug
- **Evidence:** every other HTTP call in the file sets `.timeout(...)`
  (800ms–3s), but the four validation requests in `validate_api_key` set none,
  and the default reqwest client has no total timeout. A blackholed connection
  (captive portal, firewalled egress) leaves the user stuck after typing their
  key, with no spinner and no Ctrl-C-friendly path (rpassword prompt already
  returned; the await blocks the command).
- **Fix sketch:** add `.timeout(Duration::from_secs(10))` to the four requests;
  treat timeout as "validation skipped", not key-rejected.
- **Cross-territory?** no

## F-E-12: kimctl failure exits inconsistently — `cancel`/`browser` report ❌ but exit 0; `browser` with bridge down shows a raw traceback
- **File:** kimctl/__main__.py:465-477 (`cmd_cancel`), 944-971 (`cmd_browser`)
- **Severity:** Low
- **Class:** bug | contract
- **Evidence:** kimctl defines a real exit-code vocabulary (OK/NEED_HELP/
  TIMEOUT/TRANSPORT) and `send`/`status` honor it, but: (1) `cmd_cancel` prints
  `❌ <message>` on `ok:false` and falls off the end → exit 0; (2) `cmd_browser`
  does the same on `ok:false`, and unlike every other bridge command it has no
  try/except around `_bridge_request`, so `kimctl browser show` with the
  desktop closed dumps an httpx `ConnectError` traceback instead of the
  friendly transport error.
- **Fix sketch:** `sys.exit(EXIT_TRANSPORT)` on `ok:false`/connection error in
  both; wrap `cmd_browser`'s requests in the same try/except as `cmd_status`.
- **Cross-territory?** no
