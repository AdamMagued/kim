> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Handoff — Kim bug-hunt & fixes (dev branch)

_Last updated: 2026-07-03. Branch `dev`, latest commit `c0525b4` (pushed to `origin/dev`)._

## 1) Goal
Do a deep, read-only bug hunt across the Kim app (Tauri desktop, Python orchestrator, MCP server, CLI), file every finding as a GitHub issue on `AdamMagued/kim`, fix them with tests, and keep all four test suites green. Then extend the hunt into the highest-blast-radius areas (tool-safety gates, browser bridge, agent loop) and file whatever's left so a follow-on contributor can continue.

## 2) Current State
- **Issues #22–#45 (24 bugs): FIXED, committed in `c0525b4`, pushed to `origin/dev`, and CLOSED** with per-issue verification comments. Covered ollama sign-in, packaged-app PATH, TaskRuntime cancel races, typed-IPC failure text, CLI HITL, codex proxy, and the medium/low cluster. `#43` was closed as NOT-A-BUG (proven safe by a test).
- **Gates all green** at `c0525b4`: `cd desktop/src-tauri && cargo test` = 106; `cd cli && cargo test` = 130; `./venv/bin/python -m pytest tests/` = 1256 passed / 19 skipped; `cd desktop && npx tsc --noEmit && npm run test` = 160. Clippy clean on all touched files.
- **Issues #46–#48: FILED, NOT yet fixed** (found in a manual shell/sandbox hunt).
- **Issues #49–#50: FILED, NOT yet fixed** (browser findings, CONFIRMED against code).
- **Issue #51: tracker** — plausible browser findings needing live-DOM verification + the four audit scopes a Fable agent team could not finish (agents were repeatedly cut off by an account session limit before delivering findings; only the browser agent completed).
- **Working tree is CLEAN** at `c0525b4` except this new `handoff.md` (untracked). No uncommitted fixes.

## 3) Active Files
Open issues point at these. Nothing here is edited yet unless noted.
- **#46 shell env-injection** — `mcp_server/tools/shell.py` (`_check_single_segment` / `_check_blocked`, `_DANGEROUS_ENV_VARS`).
- **#47 shell redirection over-block** — `mcp_server/tools/shell.py` (`_CHAIN_METACHAR_RE`, `_REDIR_OP_RE`, `_REDIR_PREFIX_RE`).
- **#48 credentials glob** — `mcp_server/config.py` (`_SENSITIVE_GLOBS`); mirrored in `mcp_server/checkpoints.py::_resolve_safe_path`.
- **#49 webview-bridge tool-JSON guard** — `orchestrator/providers/browser/bridge_client.py:19-31,216,289`; `provider.py:419-423`; `response_parser.py:79`.
- **#50 web_wait_for selector sniff** — `mcp_server/tools/web.py:1507-1524`.
- **#51 tracker** — `desktop/src-tauri/src/bridge.js`, `browser_bridge.rs` (plausible), plus the four un-audited scopes listed there.

## 4) Changes Made
All in commit `c0525b4` on `dev` (see `git show c0525b4` for the full diff). Highlights:
- **New file** `desktop/src-tauri/src/env_path.rs` — `fix_gui_path()` repairs the minimal launchd PATH at startup (#23).
- `ollama.rs` — `ollama whoami` sign-in probe (#22); CONTEXT-column parse by header offset (#30); trimmed fabricated cloud models (#32).
- `task_runtime.rs` — `clear_if_pid()`; all cancel/waiter clears are pid-guarded (#24).
- `subprocess.rs` / `http_bridge.rs` — stale-pid recovery + wait-error cleanup (#25); kimctl bridge forwards typed `kim:*` events (#33); permission_mode doc + aliases (#37).
- `lib.rs` — clipboard restore after browser text-send (#44).
- `cli/*` — `KIM_TAURI_MODE=1` for CLI HITL (#28); proxy tempfile + ownership (#35); explicit error on bridge image attachments (#36); OpenAI chat-model filter (#45); typed `tool` event parsing (#33a).
- `orchestrator/*` — `emit_activity` failure text (#26/#27); ollama daemon-check order (#31), tool-call index (#38), FIFO tool-result pairing (#40); recap regex (#39); `responses_proxy.py` forwards `instructions` (#29).
- Tests added/updated across `tests/test_ollama_provider.py`, `test_browser_split.py`, `test_cli_termination_output.py`, and Rust unit tests in `env_path.rs` / `task_runtime.rs` / `ollama.rs` / `provider.rs` / `agentic.rs`.

## 5) Failed Attempts / Dead Ends
- **Fable agent team (twice): did NOT work for 4 of 5 scopes.** Ten background Fable agents were dispatched (two rounds). All but one were terminated early by an **account-wide session limit** before emitting a findings report — their token counts (58–439) confirm they barely started. Only the **web/browser-bridge** agent completed and returned real findings (now #49/#50/#51). Do NOT assume the other four scopes were audited — they were not. Re-dispatch after the limit resets, or audit them manually.
- **`#43` (CLI ThinkParser partial marker) was a FALSE POSITIVE** — a char-by-char streaming test proved the 14-char tail hold-back never leaks a partial `assistantfinal`. Closed as not-a-bug. Lesson: verify PLAUSIBLE findings against the real code path before filing; don't trust an audit claim (agent or self) on its word.
- **GitHub MCP server is read-only here** (its token can't create issues). All issues were filed with the authenticated `gh` CLI instead — use `gh issue create`.

## 6) Next Steps (for the friend)
Priority order:
1. **Fix #46 first** — the shell inline-env-assignment bypass (`LD_PRELOAD=… cmd`) defeats an explicit security control and is reachable without HITL in default `full_auto` mode. Then #47 (unblock `2>&1` / `>/dev/null`) and #48 (broaden the `credentials*` glob).
2. **Fix #49** — thread `tools`/`known_tools` through `complete_via_webview_bridge` into both `parse_response` calls (mirror `provider.py:423`); add the regression test in the issue. Then **#50** (stop sniffing punctuation on `web_wait_for(text=…)`).
3. **Verify then fix #51-A1** (completion-hash echo) in the live Claude/grok DOM — potentially high-impact. #51-A2 (same-context spoof) is likely accepted-risk; decide.
4. **Finish the un-audited scopes in #51-B** — OS-control tools, agent loop + memory/compaction, non-ollama LLM providers + codex, and the Rust desktop remainder (scheduler/secrets/data_io/kimctl). Read-only deep audit each; file findings with file:line + CONFIRMED/PLAUSIBLE.
5. **Workflow reminders:** branch is `dev`; after any `.rs` change restart `tauri dev` (no hot-reload); run all four suites before pushing (`cargo test` x2, `pytest`, `tsc --noEmit && npm run test`); `events.gen.ts` is generated — edit `events.schema.json` + `npm run gen:events`; never weaken `validate_path` globs; Code tab must never use OpenAI/gpt-5.5. After pushing, confirm CI green with `gh run list --limit 1`.
