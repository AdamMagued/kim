# Wave-2 Continuation Handoff (C' + A')

Two Wave-2 fix teams were stopped mid-flight. Their branches + worktrees exist with committed
progress; fresh Fable agents pick up on the SAME branches and keep appending commits.
Source of truth for assignments: `docs/ops/WAVE2_PLAN.md`. Evidence: `docs/ops/findings/team-*.md`.

## Environment conventions (all Wave-2 agents)
- Repo: `/Users/adammaged/Desktop/kimFork/kim-pro`, integration branch `integration/audit-fixes`.
- Each team works in its OWN git worktree on branch `ops/w2-<team>` (already created for C'/A').
- Run tests from the worktree with the shared venv:
  `/Users/adammaged/Desktop/kimFork/kim-pro/venv/bin/python -m pytest tests/ -q`
- Do NOT set a global `KIM_FAKE=1` (forces the fake provider, breaks contract tests). Per-test only.
- One pre-existing failure is UNRELATED and expected in this env — ignore it:
  `test_github_create_repo.py::...test_private_visibility_unresolved_fails_compactly`
  (Playwright/CDP browser-env; needs a local browser). Everything else must stay green.
- `advisor` tool is unavailable (errors) — do not call it; use judgment.
- RESILIENCE: the account session limit kills agents mid-run. Commit after EVERY finding
  (fix + its test together). If killed, a fresh agent resumes on the branch after the last commit.
- Get `pyright` to 0 errors on touched files before declaring done:
  `/Users/adammaged/Desktop/kimFork/kim-pro/venv/bin/python -m pyright <touched files>`
- Territory discipline: edit ONLY your team's globs; cross-territory need = handoff note.
- Every fix ships a failing→passing test. Commit sign-off:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Dispatch cadence (for the conductor)
- Run at most ~2 Fable agents at once (session-limit ceiling; 6-wide and 3-wide both overshot).
- Merge order for the whole wave: `hotfix-crit (DONE) → C' → A' → D' → B' → E' → F'` (G' residual
  folds into C'/D'/A'). Merge each after it's green: from `kim-pro` on `integration/audit-fixes`,
  `git merge --no-ff ops/w2-<team>`, run pytest, then `git worktree remove ../kim-pro-wt-<team> --force`.
- Still-unstarted teams after C'/A': D' (`ops/w2-desktop`), B' (`ops/w2-providers`),
  E' (`ops/w2-cli`), F' (`ops/w2-frontend`), G' residual (`ops/w2-satellites`) — see WAVE2_PLAN.md.

---

## WORKER PROMPT — C' (MCP server & tools) continuation

> You are Wave-2 team C' (MCP server & tools) for Operation Google-Level on the Kim repo, CONTINUING
> a partially-done branch. Work in the existing worktree on the existing branch:
> ```
> cd /Users/adammaged/Desktop/kimFork/kim-pro-wt-mcp   # branch ops/w2-mcp
> git log --oneline integration/audit-fixes..HEAD      # see what's already done
> ```
> ALREADY COMMITTED (do NOT redo): F-C-1/2/3 (argv policy mirror in policy.py + sed /regex/ residual +
> gh auth token), F-C-4/5/6 (SSRF subresource guard, code/web timeout clamps, code pgroup kill),
> F-H-4 + F-INH-6 (required-args BAD_ARGS + isError protocol flag). Continue appending commits.
>
> READ FIRST: docs/ops/WAVE2_PLAN.md ("C' — MCP server & tools" section + Wave-2 rules), and the
> "Environment conventions" in docs/ops/WAVE2_HANDOFF.md. Evidence in docs/ops/findings/team-c.md,
> team-g.md (F-G-4), team-l.md (F-L-10), inherited.md (F-INH-6).
>
> REMAINING ACCEPTED FINDINGS to close (commit each with its test):
> - F-C-7 (guc_cms/guc_mail dead stub connectors — the finding's recommendation).
> - F-G-4 (DELETE the dead `shell.blocked_commands` config key + document the deny-list as
>   CODE-OWNED. Do NOT wire it into the deny-set — that's REJECTED per triage / CLAUDE.md invariant).
> - F-L-10 (introduce a shared `tool_error` helper and route the ~20 bare `return f"ERROR: {e}"`
>   sites across mcp_server/tools/ through it, preserving the plain-text error contract).
> - Finish the safety-gate regression test pack (if not already complete from the earlier commits).
>
> PYRIGHT — clean these before declaring done (introduced by the committed work):
> - tests/test_mcp_error_contract.py (~10 errors): CallToolResult is inferred as `list[Unknown]`,
>   so `.content`/`.isError` access fails. Annotate/cast the result to the real MCP CallToolResult
>   type (or assert the shape) so attribute access type-checks.
> - tests/test_browser_web_fixes.py:128 (assign None to `_FakePage.unroute_all` → make it an AsyncMock
>   or proper async stub) and :273 (Object of type None cannot be called → assert-not-None / real callable).
> - mcp_server/tools/web/browser.py:538 `import playwright.async_api` unresolved — guard consistently
>   with how the codebase already handles the optional playwright import (TYPE_CHECKING / local import
>   / matching `# type: ignore[import]`).
> - Remove the ★ unused locals/helpers flagged in test_policy_enforce.py, code.py, navigation.py,
>   test_code_web_timeout_clamp.py (or use them).
>
> TERRITORY (edit ONLY): mcp_server/** + MCP tests under tests/. Keep the deny-list code-owned.
> DONE = remaining findings closed with tests, pyright 0 on touched files, `pytest tests/` green
> (except the known github/playwright failure), diff inside mcp_server + tests. Leave the worktree in
> place. Reply: findings closed (IDs) + test count + confirm ops/w2-mcp ready to merge.
> Sign-off: Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>

---

## WORKER PROMPT — A' (Orchestrator core) continuation

> You are Wave-2 team A' (Orchestrator core) for Operation Google-Level on the Kim repo, CONTINUING a
> partially-done branch. Work in the existing worktree on the existing branch:
> ```
> cd /Users/adammaged/Desktop/kimFork/kim-pro-wt-orch   # branch ops/w2-orchestrator
> git log --oneline integration/audit-fixes..HEAD       # see what's already done
> ```
> ALREADY COMMITTED (do NOT redo): F-H-2 (codex bridge emits typed run-done on every exit path).
> Continue appending commits, one per finding.
>
> READ FIRST: docs/ops/WAVE2_PLAN.md ("A' — Orchestrator core" section + Wave-2 rules), the
> "Environment conventions" in docs/ops/WAVE2_HANDOFF.md, docs/CONTRACTS.md (seam definitions), and
> evidence in team-a.md, team-h.md, team-i.md, team-j.md, team-l.md, inherited.md.
>
> REMAINING ACCEPTED FINDINGS, in priority order (commit each with its test):
> 1. ROOT-CAUSE lifecycle/run-identity (downstream D'/B'/F' depend on these — do first):
>    - F-H-1 (put run-lifecycle CLEAR events — kim-agent-done/-cancelled/-error — on the typed schema
>      + run/session envelope).
>    - F-H-8 (spawn spec must export KIM_RUN_ID/KIM_SESSION_ID so codex-browser runs carry identity —
>      pairs with F-H-2 already done; this is the root cause of frontend F-F-2 bleed & F-F-5 spinner).
> 2. Correctness: F-A-1 (/compact no-op — fix dispatch-before-resume-load ordering; make API-provider
>    compaction durable), F-A-2 (memory trim producing assistant-first list — root here), F-A-3
>    (offload the event-loop-blocking file re-read), F-A-4 (codex-side /compact), F-A-5, F-A-6, F-A-7, F-A-8.
> 3. Contracts/security/perf: F-H-7 (codex-proxy schema loss), F-H-3 (silent chat-stdout drop),
>    F-I-2 (KIM_CODEX_BYPASS_SANDBOX spawn side — coordinate w/ C' shell layer), F-I-3 (session_store
>    JSONL world-readable → tighten perms like token/keystore), F-J-1 (scheduled_runs retention),
>    F-J-4 (fsync offload), F-J-6 (agent self-watchdog), F-INH-5/7/8, F-L-1 (memory.py screenshot-
>    retention values → make code correct + handoff the right numbers to L' for the README).
>
> PYRIGHT — clean before done (introduced by the F-H-2 commit): codex_bridge_service.py — the
> statically-false-condition warnings at ~777/811 and unused `_LOCAL_PROXY_KEY` (~92) / `_frame` (~157).
>
> TERRITORY (edit ONLY): orchestrator/** EXCLUDING orchestrator/providers/**, + tests/. Providers=B',
> Rust spawn-identity export=D', frontend render=F' → handoff notes. Preserve invariants:
> [STATUS]/[PLAN]/[STEP]/[DONE]/[CONTEXT]/[UI] stdout protocol; HITL hard-block; codex CLI text protocol.
> DONE = remaining findings closed with tests, pyright 0 on touched files, `pytest tests/` green (except
> the known github/playwright failure), diff inside orchestrator + tests. Leave the worktree in place.
> Reply: findings closed (IDs) + test count + the run-identity/lifecycle summary (downstream needs it) +
> handoffs + confirm ops/w2-orchestrator ready to merge.
> Sign-off: Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
