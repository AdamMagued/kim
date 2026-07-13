# Inherited findings — cobweb-plumbing hunt, re-validated (Wave 0)

**Source:** read-only "cobweb plumbing" hunt of 2026-07-06 on `feat/roadmap-to-10` @ `cf319b7`
(config, MCP bootstrap/dispatch, API providers, persistence, lifecycle, logging/policy).
The report was delivered in-session, never committed; recovered from the session transcript
(`~/.claude/projects/-Users-adammaged-Desktop-kimFork/7229a478-*.jsonl`). 24 findings total
(1.1–1.6, 2.1–2.6, 3.1–3.8, 4.1–4.5, 5.1–5.3, 6.1–6.6).

**Status correction to the master plan:** the plan (§1 "Known UNFIXED debt") says these were
"REPORTED ONLY, never fixed". That is stale. Branch `fix/cobweb-plumbing` (commits `4bf088d`,
`85df30b`, tests `a8e49ae`/`bb6f05a`/`7d93d6b`) fixed **22 of 24** findings with regression
tests, was merged via `integration/waveB` (`029ac47`), and is in current main. Verified on
`integration/audit-fixes` @ `6f69adb`: all 84 cobweb regression tests pass
(`tests/test_cobweb_plumbing.py`, `test_config_hardening.py`, `test_shell_timeout_clamp.py`,
`test_mcp_dispatch_hardening.py`, `test_provider_finish_reason.py`).

**Fixed (do not re-hunt):** 1.1–1.5, 2.1–2.5, 3.1, 3.4, 3.8, 4.1–4.3, 5.1, 5.3, 6.1–6.6.
Notably: scalar `allowed_paths` → `/` escape (1.1), null-config-section boot crash (1.2),
client/server tool-timeout double-execution (2.1) — the three headline examples — are all fixed.
Also fixed since by other campaigns: 3.5 (assistant narration alongside tool calls is now kept —
"H2" fix in `claude.py`/`openai_provider.py`).

**Resolved by documented design decision:** 5.2 (runner lock held across the 10 s `_preflight`) —
`orchestrator/scheduled_runner.py:538` now carries an explicit comment justifying preflight
inside the lock so a failed check never advances `next_run_at`. No action.

---

## Survivors (re-validated against `6f69adb`, 2026-07-11) — 8 findings

## F-INH-1: Gemini OAuth token frozen at process spawn; long sessions die non-retryably
- **File:** orchestrator/providers/gemini.py:61-82
- **Severity:** Medium
- **Class:** bug
- **Evidence:** `EnvOAuthAccessTokenProvider` re-reads `os.environ` per call, but a running
  process's env is fixed at spawn — Tauri injects `KIM_GOOGLE_ACCESS_TOKEN` (+
  `KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT`) once. Within 60 s of expiry the provider hard-raises
  `EnvironmentError` (line 79-80), classified non-retryable auth. Any session/process outliving
  the ~1 h token dies mid-task even though the shell holds a valid refresh token. The
  stateful-thread work has made processes longer-lived, raising exposure. (Was 3.3, SUSPECTED.)
- **Fix sketch:** refresh channel — stdin control line or token-file re-read — instead of
  spawn-time env; or have the Rust side re-inject via the bridge.
- **Cross-territory?** yes — Team B (provider) + Team D (Tauri injects the token)

## F-INH-2: `max_tokens` param breaks newer OpenAI models
- **File:** orchestrator/providers/openai_provider.py:63,79
- **Severity:** Low
- **Class:** bug
- **Evidence:** always sends `max_tokens`; o-series/GPT-5-family endpoints reject it in favor
  of `max_completion_tokens` (HTTP 400 → non-retryable). Fine on default gpt-4o; breaks the
  moment a user configures a newer model. (Was 3.7, unchanged.)
- **Fix sketch:** send `max_completion_tokens` for models that require it (or retry-once on the
  specific 400).
- **Cross-territory?** no — Team B

## F-INH-3: Malformed tool-call JSON still coerced to `{}` — model never learns
- **File:** orchestrator/providers/openai_provider.py:172-183; orchestrator/providers/ollama.py:680-689
- **Severity:** Low (was Medium-Low; downgraded — now logged)
- **Class:** bug
- **Evidence:** the "M9" fix added a `logger.warning` with the raw args, but both providers
  still substitute `{}` and proceed. For all-optional-schema tools the call runs with defaults
  and the agent gets plausible-but-wrong output; the warning is server-side only — the model
  gets no signal to re-emit. (Was 3.2, partially fixed.)
- **Fix sketch:** return a synthetic tool-error result ("arguments were not valid JSON") so the
  model can re-emit.
- **Cross-territory?** no — Team B

## F-INH-4: Ollama still pays 2 HTTP round-trips per LLM turn
- **File:** orchestrator/providers/ollama.py:176-184,281-290,344-352
- **Severity:** Low (was Low; half-fixed)
- **Class:** perf
- **Evidence:** the "M13" fix caches the context-limit (`ollama ps` subprocess + `/api/show`)
  per model, but every `complete()` still calls `_ensure_daemon_running()` (`/api/version`) and
  `_fetch_tags()` (`/api/tags`). Latency tax on the default provider, per turn. (Was 3.6.)
- **Fix sketch:** cache daemon-alive/tags for the session with invalidate-on-error.
- **Cross-territory?** no — Team B

## F-INH-5: `project_root` default differs between orchestrator and MCP server
- **File:** orchestrator/mcp_client.py:99-103 vs mcp_server/config.py:106-112
- **Severity:** Low
- **Class:** contract
- **Evidence:** with no `PROJECT_ROOT` env and no `project_root` config key, the client
  resolves `Path.cwd()` (and spawns the server with that cwd) while the server's config
  resolves the config-file directory (`_PROJECT_DIR`). A CLI run from an arbitrary directory
  gets two different roots; only the inherited env var keeps them consistent by accident.
  (Was 1.6, unchanged.)
- **Fix sketch:** client passes its resolved root as `PROJECT_ROOT` in the spawned server's
  env (it already builds `merged_env`).
- **Cross-territory?** no — Team A (client side) / Team C (server side) — one-line either way

## F-INH-6: Tool errors indistinguishable from tool output at the MCP protocol level
- **File:** mcp_server/server.py:113-155
- **Severity:** Low
- **Class:** contract
- **Evidence:** unknown tools, policy denials, and handler exceptions all return ordinary
  `TextContent` with `isError` unset; the agent relies on string prefixes (`ERROR:`,
  `HITL_DENIED:`, …). Works because both ends are in-repo; brittle seam. (Was 2.6, unchanged.)
- **Fix sketch:** set `isError=True` (SDK supports it) and keep prefixes for back-compat;
  belongs with Team H's CONTRACTS.md work.
- **Cross-territory?** yes — Team C (server) + Team A (agent parsing) + Team H (contract doc)

## F-INH-7: Interval schedules drift later forever
- **File:** orchestrator/cron_store.py:488-497 (record_run); orchestrator/scheduled_runner.py (timer path)
- **Severity:** Low
- **Class:** bug
- **Evidence:** `record_run` computes `next_run_at = ran_at + interval` with `ran_at` defaulting
  to wall-clock now, so every tick adds scheduler latency (up to 60 s tick + lock waits) to the
  period — "@daily 09:00" creeps later without bound. (Was 4.4, unchanged.)
- **Fix sketch:** anchor to the previous `next_run_at` when it exists.
- **Cross-territory?** no — Team A

## F-INH-8: `list_sessions` full-parses every line of every session file
- **File:** orchestrator/session_store.py:727-774
- **Severity:** Low
- **Class:** perf
- **Evidence:** every listing call JSON-parses all lines of all historical JSONL files (files
  can reach tens of MB) just to compute `message_count`. CLI/agent resume paths pay it. (Was
  4.5, unchanged.)
- **Fix sketch:** sidecar count / cheap line count / cache keyed on (path, mtime, size).
- **Cross-territory?** no — Team A

---

## Pre-triage summary

| ID | Sev | One-liner | Suggested owner |
|---|---|---|---|
| F-INH-1 | Medium | Gemini token frozen at spawn → mid-task auth death | B (+D) |
| F-INH-2 | Low | `max_tokens` 400s on newer OpenAI models | B |
| F-INH-3 | Low | malformed tool JSON → `{}`; model gets no signal | B |
| F-INH-4 | Low | 2 Ollama round-trips per turn | B |
| F-INH-5 | Low | project_root default mismatch client vs server | A/C |
| F-INH-6 | Low | MCP errors are plain text, string-prefix contract | C (+A, H) |
| F-INH-7 | Low | interval schedule drift | A |
| F-INH-8 | Low | list_sessions O(total transcript bytes) | A |

No High/Critical survivors. The three headline inherited examples in the master plan (1.1, 1.2,
2.1) are already fixed and regression-tested on main — Wave 1 teams should not re-hunt them.
