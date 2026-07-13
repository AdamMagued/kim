# Team A — Orchestrator Core (Python) — Wave 1 findings

Territory: `orchestrator/` excluding `providers/`. Baseline: `integration/audit-fixes` @ HEAD.
Read-only hunt. Inherited findings (`inherited.md`) and the 22 fixed cobweb items are NOT re-reported.

Severity counts: High 2 · Medium 3 · Low 3.

---

## F-A-1: `/compact` on API providers is a complete no-op
- **File:** orchestrator/agent.py:565-566 (dispatch) → 2013-2021 (`_compact_api_provider`)
- **Severity:** High
- **Class:** bug
- **Evidence:** Each task runs as a fresh `python -m orchestrator.agent --task … --resume <id>` subprocess
  (desktop/src-tauri/src/task_spec.rs:chat_task_spec). In `run()` the compact-control check at line 565
  (`if task.strip().lower() in _COMPACT_CONTROL_TASKS: return await self._compact_and_reset_context()`)
  fires **before** the resume-load block at lines 580-605 that populates `self.memory` from disk. For
  non-browser providers `_compact_and_reset_context` calls `_compact_api_provider`, which reads
  `messages = list(self.memory._messages)` — always empty at this point — and returns
  `"NEED_HELP: There is no conversation to compact yet."`. The browser path (agent.py:1948) instead loads
  from disk via `SessionStore.load_session(...)`, so only the browser path works. Compounding it: even if
  memory were loaded first, `_compact_api_provider` only rewrites in-process memory and writes a
  `save_summary` sidecar — it never rewrites the session JSONL, so the next task's resume reloads the FULL
  pre-compact history from disk and the compaction has zero durable effect. `/compact` for Claude/OpenAI/
  Ollama/DeepSeek is therefore doubly ineffective.
- **Fix sketch:** move the compact-control dispatch to after the resume-load block (or have
  `_compact_api_provider` load from `SessionStore.load_session` like the browser path), AND persist the
  compacted history back to the session (truncate/rewrite the JSONL, or write a compaction checkpoint the
  resume path honors) so the next task actually sends the compacted set.
- **Cross-territory?** no — Team A.

## F-A-2: Resuming a long API session can send an assistant-first message list → Anthropic 400
- **File:** orchestrator/memory.py:123-165 (`_enforce_limits`) + 205-223 (`_fix_tool_boundary`); surfaces at orchestrator/providers/claude.py:69-92
- **Severity:** High
- **Class:** bug (contract with the Anthropic API)
- **Evidence:** On resume, `load_from_messages` calls `_enforce_limits`, which trims to a window and calls
  `_fix_tool_boundary`. When the first preserved user message is a tool result, the boundary walk-back
  intentionally decrements to include the preceding **assistant** tool_call, so `self._messages[0]` becomes
  an assistant message. `run()` then only *appends* the new task (`self.memory.add_user(...)`, agent.py:715),
  so `get_messages()` returns `[assistant(tool_call), user(tool_result), …, user(task)]`. The Anthropic API
  requires the first message to use the `user` role; `claude.py._to_claude_messages` passes the list through
  verbatim with no leading-role normalization, so the request 400s. Reachable whenever a resumed session
  exceeds `memory_max_messages` (default 40) and the trim lands mid tool_call/tool_result pair — common in
  real multi-tool sessions. Non-strict providers (Ollama/OpenAI/browser) tolerate it; Anthropic does not.
- **Fix sketch:** in memory (Team A), after trimming, drop a leading assistant message that has no following
  user turn to pair against, or prepend the summary/task so the window starts on `user`. Belt-and-suspenders:
  claude.py should drop/relabel a leading assistant message.
- **Cross-territory?** yes — root cause Team A (memory.py); provider-side guard belongs to Team B (claude.py).

## F-A-3: File-write diff reads the whole file twice with blocking sync IO on the async loop
- **File:** orchestrator/agent.py:1362-1372 (pre-write) and 1450-1461 (post-write)
- **Severity:** Medium
- **Class:** perf
- **Evidence:** In `_execute_tool`, for `_write_ops = {write_file, create_file, edit_file, append_file}` the
  before/after line counts use `sum(1 for _ in _f)` over the entire file with a synchronous `open(...)` inside
  the async agent loop. For a large generated file (multi-MB write, a plausible codex/agent output) this reads
  the whole file twice on the event-loop thread, stalling every other coroutine (UI event emission, the MCP
  session, cancellation checks) for the duration. The charter explicitly targets event-loop-blocking sync IO
  in async paths.
- **Fix sketch:** run the line-count in a thread (`asyncio.to_thread`), cap the bytes counted, or derive the
  diff from the tool result instead of re-reading the file.
- **Cross-territory?** no — Team A.

## F-A-4: codex-side `/compact` resume omits the proxy provider config → silently never works
- **File:** orchestrator/codex_appserver_transport.py:1017-1047 (`compact_codex_thread`)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** `compact_codex_thread` issues `thread/resume` with only `{"threadId": thread_id, "cwd": cwd}`,
  omitting the `approvalPolicy`/`sandbox`/`model`/`modelProvider: "kim-proxy"`/`config` overrides that the real
  turn path (`_resume_or_start`, lines 528-535) always supplies. Without `modelProvider`/config the resumed
  thread has no route to Kim's local proxy, and it is started with `build_appserver_env("kim-compact")` (a
  placeholder bearer, no real key), so the subsequent `thread/compact/start` cannot reach a model and fails.
  The call is best-effort (`except Exception: return False`, and codex_bridge_service only logs a status), so
  the failure is silent — the codex-transcript half of `/compact` (parity Part 2.4) never actually compacts.
- **Fix sketch:** pass the same `common` block (`modelProvider`, inline `config` overrides, model, policies)
  on the resume in `compact_codex_thread`, mirroring `_resume_or_start`.
- **Cross-territory?** no — Team A (pairs with Team H CONTRACTS.md for the codex app-server seam).

## F-A-5: token-usage handler blocks the notification pump on a 30s compact request
- **File:** orchestrator/codex_appserver_transport.py:876-897 (`_on_token_usage`), called from `_pump` (596-604)
- **Severity:** Low
- **Class:** perf
- **Evidence:** `_handle_notification` is awaited serially inside `_pump`'s `async for msg in client.events()`.
  When token usage crosses the compact budget, `_on_token_usage` `await`s
  `client.request("thread/compact/start", …, timeout=30.0)` inline, so the pump stops draining notifications
  for up to 30s. Any `turn/completed`, assistant-message deltas, command output, or approval requests that
  arrive during that window are not translated/emitted until the compact request returns — a visible UX stall
  and, for an approval that codex is blocking on, a delayed prompt.
- **Fix sketch:** fire the compact request as a background task (`asyncio.create_task`) instead of awaiting it
  in the notification handler.
- **Cross-territory?** no — Team A.

## F-A-6: memory vs compaction disagree on tool-result detection (leading whitespace)
- **File:** orchestrator/memory.py:167-182 (`_is_tool_result`) vs orchestrator/compaction.py:133-152
- **Severity:** Low
- **Class:** bug (latent)
- **Evidence:** compaction.py’s `_is_tool_result` uses `content.lstrip().startswith("[Tool result:")` (and the
  same for the text-item branch), while memory.py’s uses a bare `content.startswith("[Tool result:")`. If a
  tool result ever carried leading whitespace, the memory trim path would fail to recognize it and would not
  walk the boundary back to its tool_call, orphaning the pair — while the compaction path would handle it —
  producing divergent trims from the "single source of truth" the two are supposed to share. Currently latent
  because agent.py always writes results as `f"[Tool result: {tool}]\n{result_text}"` with no leading space.
- **Fix sketch:** make memory.py’s `_is_tool_result` `lstrip()` first, matching compaction.py exactly.
- **Cross-territory?** no — Team A.

## F-A-7: fresh session-id collision probe is TOCTOU across processes
- **File:** orchestrator/session_store.py:67-93
- **Severity:** Low
- **Class:** race
- **Evidence:** For a fresh session the id is chosen by probing `find_session_file(candidate)` for
  non-existence, but the `.jsonl` file is not created until the first `append_*`. Two `SessionStore`
  instances constructed concurrently in different agent processes can each pick the same 8-hex candidate
  (neither has written a file yet) and then interleave appends into one file, merging unrelated transcripts —
  the residual cross-process hole in the birthday-collision fix (inherited 4.2, which only guarded against
  existing files). Astronomically unlikely given random 8-hex + the narrow window, but the invariant the fix
  claims ("cannot silently append this session into an unrelated one") is not actually guaranteed across
  processes.
- **Fix sketch:** claim the id by atomically creating the empty `.jsonl` with `O_CREAT|O_EXCL` during
  `__init__`, retrying on `FileExistsError`.
- **Cross-territory?** no — Team A.

## F-A-8: an explicit provider `input=0` resets the context gauge to zero mid-session
- **File:** orchestrator/context_meter.py:98,108-144 (`observe_usage`/`add_input`) with `_usage_int` at 334-343
- **Severity:** Low
- **Class:** bug
- **Evidence:** `_usage_int` returns `max(0, int(value))`, so a provider reporting `input_tokens: 0` yields
  `0`, not `None`. `observe_usage` then skips the fallback (0 is not None) and calls `add_input(0)`, whose
  non-accumulate branch does `self.cumulative_input = tokens` — i.e. sets the running window fill to 0. A
  single turn where the provider reports a zero/absent-but-present input count momentarily zeroes the gauge
  and drops the phase from warn/critical back to ok, then it jumps back up on the next real count.
- **Fix sketch:** treat a 0 input count as "no signal" (fall back to the estimate) or ignore a 0 that would
  lower a non-zero cumulative in the stateless (non-accumulate) branch.
- **Cross-territory?** no — Team A.

---

## Dead code
`vulture orchestrator/ --min-confidence 80 --exclude providers` (vulture 2.16) reports **zero** hits.
At `--min-confidence 60` every hit is a verified false positive: `_hitl_risk_threshold`, `should_compact`,
`compare_providers`, `CronStore.delete`/`list_tasks`, and the `events_gen.py` constants are all exercised by
`tests/` or `kimctl/__main__.py`; `set_ui_bridge`, `_screenshot_signature`, `_signatures_similar`,
`_is_retryable` are public/test-facing wrappers. One genuinely-unused-but-harmless field:
`_StdinDecisionPump._eof` (codex_appserver_transport.py:268,292) is assigned but never read (EOF is signaled
via the `_EOF` sentinel on the queue) — safe to drop, not worth a fix on its own.
