# ROADMAP_PROGRESS.md — execution log for docs/ROADMAP_TO_10.md

This file is the handoff between phase implementers. Each phase appends a
section. Read `docs/ROADMAP_TO_10.md` first; this file tells you what has
actually landed and what the next implementer must know.

---

## Phase 0 — De-risk & clean — DONE (2026-07-06, branch `feat/roadmap-to-10`)

Commits (on `feat/roadmap-to-10`, based on `origin/main` @ 327bad2):

1. **K4/Q1/Q4** — delete dead `run_codex_subtask`; behavioral codex tests
2. **Docs consolidation** — 17 → 8 root `.md`; AGENTS.md pointers resolve
3. **A5/S6 + Q6** — relay/voice/dead-command decommission; file-size CI gate
4. this handoff

### K4 / Q1 / Q4 — dead code + env contradiction (done)

- `codex_engine/engine.py`: `run_codex_subtask` deleted (verified: zero
  runtime callers — the live path is `orchestrator/codex_bridge_service.py`,
  which imports only `_CodexProxy` + helpers and spawns codex itself).
  Also deleted `_drain_stderr_to` (only the dead function used it; the live
  path has its own inline drain) and its test file `test_codex_stderr_drain.py`.
- **The env contradiction is resolved**: the single surviving contract is the
  hardened minimal allowlist in `codex_bridge_service.py` (~line 456):
  `PATH/HOME/USER/TMPDIR/LANG + CODEX_HOME + per-run bearer token as
  CODEX_API_KEY/OPENAI_API_KEY + OPENAI_BASE_URL` (+ Windows passthrough).
- The 3 grep-test files are now **behavioral**:
  - `tests/codex_bridge_harness.py` — shared harness: writes a REAL fake
    `codex` binary (python script) that records argv+env+pid, then drives
    `codex_bridge_service._run_async` end-to-end with only `create_provider`,
    `_CodexProxy`, and the thread-state dir faked.
  - `tests/test_codex_env_scoping.py` — child env == allowlist exactly; parent
    secrets don't leak; API keys == proxy bearer token; parent environ not
    mutated.
  - `tests/test_codex_bridge_tool.py` — argv shape (`exec --json … -C cwd
    task`), bypass-flag gating (`KIM_CODEX_BYPASS_SANDBOX` must be exactly
    "1"), git-repo gate, non-browser-provider rejection, missing-binary path.
  - `tests/test_codex_process_cleanup.py` — exit-code propagation, timeout
    kills the child (no orphan; uses `codex_bridge.task_timeout_s` config),
    proxy stopped on every path, `_cleanup_sync` kill.
  - **This harness is the model for converting the remaining ~25 grep-tests
    (T1).** Pattern: fake binary records reality → assert on the recording.

### Docs consolidation (Dimension 6) (done)

- Root `.md` count: 17 → **8** (README, ARCHITECTURE, HOW_TO, SECURITY_NOTES,
  CHANGELOG, CLAUDE, AGENTS, ROADMAP).
- Moved to `docs/archive/`: DEEP_DIVE_AUDIT, EXECUTION_REPORT, REVIEW_GUIDE,
  MISSION_PROMPTS, AGENT_PROMPTS, repomap, IMPROVEMENT_PLAN, HARNESS_ROADMAP,
  PRODUCTION_ROADMAP, REFACTOR_ROADMAP. All in-repo references updated
  (README, CHANGELOG, CLAUDE.md, tool_tiers.py, useChatStream.ts,
  types/index.ts, one test docstring).
- New root `ROADMAP.md` = router to `docs/ROADMAP_TO_10.md` + provenance table
  for the 4 superseded backlogs.
- `AGENTS.md` rewritten: per-dir pointers now target the real `CLAUDE.md`
  files; tool count corrected 31 → 50 (verified `len(tool_registry.TOOLS)`).
- No CI doc-link-check was added (not in Phase 0 scope) — consider in Phase 2
  alongside T7.

### A5/S6 — dormant-subsystem adjudication (decision + rationale)

**Ground truth correction:** the roadmap's "~25/82 Rust commands with no UI
caller" was stale. Actual survey: **67 registered commands, 8 with zero
frontend `invoke` refs, only 4 truly dead** on every surface (frontend, cli/,
http_bridge, internal Rust).

Decisions taken (all acted on):

| Subsystem | Decision | Rationale |
|---|---|---|
| `relay_server/` + Dockerfile + railway.toml + `relay.rs` + PaneRelay/PairingModal UI + relay.css | **DELETE (full decommission)** | Never enabled (RELAY_ENABLED=false since inception); the Python server was independently deployable attack surface (S6). A hidden UI pane + 535-line Rust module + 1200-line server carried real maintenance cost for zero users. Git history preserves it; resurrection must be a deliberate redesign. `test_invariants.py` II-J flipped from "code preserved" to "stays deleted". Bonus: deleting PairingModal.tsx resolved the long-standing `qrcode.react` missing-types issue. |
| Voice scaffold | **DELETE Rust surface, keep config flag** | `voice_config.rs` (2 commands) had zero callers anywhere; `requirements-voice.txt` + `config.yaml.example` voice block removed. `mcp_server/config.py` `VOICE_ENABLED` (default False) kept — pinned by invariant test, harmless, and the eventual voice runtime is a Python concern. |
| Dead Tauri commands | **DELETE the 4 dead, demote 2, keep 2** | Deleted: `delete_session` (+ `base_dir`/`delete_session_files` helpers + their tests — the whole session-delete surface had no caller), `read_voice_config`, `write_voice_config`, `list_due_scheduled_tasks` (+ `build_due_args` + tests; the schedule timer uses `run_due_once` directly). Demoted to plain fns (registration + `#[tauri::command]` removed, logic kept): `session_browser_meta_read` (used by `session_browser_url_commit`), `clear_account` (used by `reset_onboarding`). Kept: `set_privacy_pause`/`get_privacy_pause` (invoked from Rust `speed_access.rs`, not JS). |
| `pythonExperimentTool/claw-code` (vendored) | **KEEP for now** | Referenced by compaction code comments as design provenance; zero runtime coupling; deleting it buys nothing Phase 0 needs. Revisit in Phase 2 if it confuses tooling. |

### Q6 — file-size CI gate (done)

- `scripts/check_file_size_gate.py` + new CI job `file-size-gate` (first job
  in `.github/workflows/ci.yml`).
- Policy: a **new** source file (.py/.rs/.ts/.tsx/.js/.jsx) > 800 lines fails;
  a **changed** file > 800 lines fails **only if it grew** vs base. This lets
  the known giants (`agent.py` 2225, `http_bridge.rs` 2189, `provider.rs`
  2246, `subprocess.rs` 1507, `web.py` 1561, `useChatStream.ts` 794…) keep
  taking fixes until their scheduled K2/Q2/Q3/A4/Q5 splits — but they cannot
  grow, and nothing new can be born oversized. Codegen (`*.gen.ts`,
  `*.gen.rs`, `*_gen.py`, `.d.ts`) exempt.
- Verified both directions (clean run passes; a 900-line new file fails).

### Suite status at Phase 0 exit (all green, run 2026-07-06)

| Suite | Result |
|---|---|
| `venv/bin/python -m pytest tests/` (CI ignore flags) | **1449 passed, 19 skipped** |
| `cd desktop && npx tsc --noEmit && npm run test` | **tsc clean; 160 passed (12 files)**; `npm run build` OK |
| `cd desktop/src-tauri && cargo test` (+ clippy -D warnings) | **87 passed; clippy clean** |
| `cd cli && cargo test` | **136 passed** |
| pyright | **0 errors**; flake8 orchestrator clean |

### Gotchas for the Phase 1 implementer

1. **Venv location:** the worktree has no venv; use the main checkout's
   `kim-pro/venv/bin/python`. Desktop `node_modules` needs `npm ci` per
   worktree.
2. **The behavioral harness patches `os.environ` with a plain dict**
   (`tests/codex_bridge_harness.py`). If `_run_async` ever reads env through
   something that caches `os.environ` at import time, tests will diverge from
   reality — keep env reads late-bound.
3. **`test_invariants.py` now asserts relay is GONE.** Don't "helpfully"
   restore relay files from git history.
4. **The size gate compares against `origin/main`** on new branches. A
   long-lived feature branch that legitimately grows a legacy giant file will
   fail — that is intentional; extract into a new module instead.
5. **`session_browser_meta_read` / `clear_account` are no longer Tauri
   commands.** If future UI needs them, re-register deliberately.
6. **K1 (Phase 1) hook points, verified during this work:** the MCP server
   dispatch is `mcp_server/server.py:call_tool` (~line 122); risk classifier
   `mcp_server/tool_risk.py`; the HITL stdin round-trip to reuse is
   `subprocess.rs:hitl_respond_approval` ↔
   `codex_bridge_service._await_hitl_decision` (~line 203). The new
   `tests/codex_bridge_harness.py` pattern (fake binary, assert observed
   behavior) is what policy tests should look like — no source grepping.
7. **Do not weaken** the secret-file globs (`mcp_server/config.py`), CSS
   import order (`desktop/src/index.css` — relay.css was *removed*, order
   untouched), f-string brace doubling, or the Code-tab provider constraint.

---

## Phase 1 — Security spine — DONE (2026-07-06, branch `feat/roadmap-to-10`)

The MCP server is now the single enforcement chokepoint. Every tool call —
from the agent, the CLI, the codex bridge, or any future connector — passes
through `policy.enforce()` before dispatch, by construction.

### K1/S1 — `mcp_server/policy.py` + `server.py:call_tool` enforcement (done)

- New **`mcp_server/policy.py`**: `enforce(name, args) -> PolicyDecision`
  with three outcomes — `allow` / `deny` / `approve`. Called as the FIRST
  thing in `server.py:call_tool` (before `_DISPATCH.get`). Reuses
  `orchestrator.tool_risk.classify_tool_risk` for the base risk level, then
  refines it with argv analysis. **Fail-closed**: any internal error denies.
- **Deny** (never dispatches): a path-typed arg or shell argument that
  resolves to a sensitive/out-of-sandbox path (S3); a shell command whose
  *resolved real binary* is in the denylist (defeats symlink-rename); hard
  argv rules (`find -delete/-exec`, `curl`); malformed quoting.
- **Approve** (blocks on a human, default-deny on timeout): risk ≥ the
  configured `hitl_risk_threshold`, OR an escalation argv rule fired
  regardless of threshold — inline interpreter exec (`python -c`, `node -e`,
  `bash -c`), a non-allowlisted or **untrusted-location** binary (a copied
  `rm` renamed to `ls`), destructive git flags (`push --force`,
  `reset --hard`, `clean -f`), inline env assignments (`PATH=/tmp x`),
  `sudo`/`doas`.
- **Allow**: everything else. Read-only shell binaries (`ls`, `grep`, `git
  status`…) are risk-downgraded to `low` so a `high` threshold does not prompt
  for them.

### S2 — shell allowlist + argv rules replacing the basename denylist (done)

- Positive model in `policy.py`: `_SAFE_READONLY` (allowed + low-risk),
  `_ALLOWED_MUTATING` (allowed, threshold-gated), config extensions via
  `shell.safe_extra` / `shell.allowlist_extra`. The old basename denylist in
  `shell.py` is kept as **defense-in-depth** (still blocks `rm`, metachars).
- **Real-binary resolution** (`_resolve_binary`): plain names go through
  `shutil.which` + `realpath` (a symlink `ls`→`rm` classifies as `rm`);
  explicit paths are `realpath`'d and trusted only if the real file lives in a
  system bin dir (`/usr/bin`, `/bin`, homebrew…). A copied binary in the
  project dir is never trusted → escalated. `python3.13`/`pip3` normalize to
  `python`/`pip` for allowlist lookup.

### S3 — path-validate EVERY path-typed arg (done)

- `_PATH_ARGS` table maps each tool to its path-typed args; all go through
  `config.validate_path`. Shell command *arguments* are also scanned
  (`_scan_path_tokens`) — closing the `cp ~/.ssh/id_rsa /tmp` gap that the old
  redirect-only check missed. `/dev/null` & friends whitelisted.

### S4 — minimal-allowlist env for ALL subprocess tools (done)

- New **`os_utils.minimal_subprocess_env()`** — positive allowlist
  (PATH/HOME/locale/display plumbing), NO parent-env inherit. Retired
  `shell.py:_filtered_env`'s full-inherit (it now delegates here). Applied to
  every spawn: `shell.py`, `git.py`, `github.py` (+ GH_TOKEN passthrough),
  `search.py`, `ui_observe.py`, `windows.py`, `web.py` browser launches.
  `code.py:_minimal_env` was already correct and is unchanged.
- Behavioral proof: `tests/test_tool_env_minimal.py` spawns real children and
  asserts planted secrets (`OPENAI_API_KEY`, `GITHUB_TOKEN`, …) never appear.

### K1 approval flow (server-side HITL) — the plumbing (done)

The gate **moved out of `agent.py:_execute_tool` down into the MCP server**:

```
call_tool → policy.enforce → (approve) → approvals.request_approval
  → [unix socket / TCP] → orchestrator ApprovalBroker
  → KimAgent resolver → emit hitl_approval_request on stdout
  → UIBridge.decide_action → stdin hitl_approve line → decision back
```

- **`mcp_server/approvals.py`**: connects to the broker over
  `KIM_APPROVAL_SOCK` (unix) / `KIM_APPROVAL_TCP` (Windows), sends
  `tool_approval_request` (id, tool, risk, reason, preview, args), awaits a
  `decision`. Every failure mode (no channel, connect error, bad reply,
  timeout) → `decline`. Session cache: `acceptForSession` remembers the
  decision signature for the process lifetime (== one Kim session).
- **`orchestrator/approval_broker.py`**: loopback listener started by
  `mcp_client.mcp_session_context` BEFORE the server spawns; injects the env
  vars into the server's env; torn down with the session (2 s bounded
  `wait_closed`). `KimAgent` registers the resolver only when it owns a
  UIBridge (else declines — fail closed).
- **Decision vocabulary** (`accept` | `acceptForSession` | `decline`)
  standardized end-to-end:
  - Rust `crate::hitl::build_hitl_approve_line` writes
    `{"type":"hitl_approve","approved":bool,"decision":…,"id":…}`;
    `hitl_respond_approval` (Tauri cmd) + `/v1/task/approve` +
    `useChatStream.hitlRespond(approved, decision?)` all carry it.
  - CLI `agentic.rs` writes the same line.
  - Python `ui_bridge.normalize_decision` + `approvals._normalize_decision`
    understand both the new vocabulary AND the legacy `{"approved":bool}`.

### K2 start (minimum decomposition K1 required) (done)

- Extracted a new **`desktop/src-tauri/src/hitl.rs`** module holding
  `hitl_respond_approval`, `build_hitl_approve_line`, and
  `approve_line_from_body`. This is the minimal spawn/stdin decomposition K1's
  approval flow needed — and it kept `subprocess.rs` (−9) and `http_bridge.rs`
  (−12) *below* their baselines so the Q6 size gate stays green. The full
  `TaskSpec`/`EnvBuilder`/`SpawnSupervisor` decomposition remains Phase 2.

### Tests (behavioral, no source-grepping)

- `tests/test_policy_enforce.py` (63) — allow/deny/approve matrix, the argv
  rule table, the three headline attacks (renamed `rm` symlink+copy, `python
  -c`, `cp ~/.ssh/id_rsa /tmp`), and a chokepoint test that patches
  `enforce`→deny and proves NO handler runs for ANY registered tool.
- `tests/test_policy_approval_flow.py` (20) — full-stack round-trip over a
  real socket for BOTH caller vocabularies (GUI `{approved}` and CLI
  `{decision}`), acceptForSession caching, and fail-closed (timeout / no
  channel / no resolver all decline).
- `tests/test_tool_env_minimal.py` (9) — S4 secret-leak proofs.
- `hitl.rs` (4) — decision-line builder table.
- Rewrote `tests/test_hitl_approval.py` (the old agent-gate tests now target
  the resolver seam) and updated `test_approval_preview.py` (preview builder
  moved to `policy.build_approval_preview`), `test_stdin_approval_bridge.py`
  (bridge now attaches without a threshold), `test_shell_command_blocking.py`
  (`_filtered_env` is an allowlist now).

### Exit criteria — all met

- ✅ Every MCP tool call passes through `policy.enforce` (chokepoint test).
- ✅ Renamed `rm` (symlink → deny; copy → approve), `python -c` (approve),
  `cp ~/.ssh/id_rsa /tmp` (deny) each blocked/gated by tests.
- ✅ No tool inherits full parent env (behavioral secret-leak tests).
- ✅ Approval round-trip works from BOTH GUI and CLI callers (both tested).

### Suite status at Phase 1 exit (all green, 2026-07-06)

| Suite | Result |
|---|---|
| `venv/bin/python -m pytest tests/` (CI ignores) | **1551 passed, 39 subtests** |
| `desktop`: `tsc --noEmit` + `npm run test` | **tsc clean; 160 vitest** |
| `desktop/src-tauri`: `cargo test` + `clippy -D warnings` | **91 passed; clippy clean** |
| `cli`: `cargo test` + `fmt --check` | **136 passed; fmt clean** |
| pyright (orchestrator/providers + mcp_server + codex_engine) | **0 errors** |
| flake8 orchestrator | **clean** |
| events schema drift / file-size gate | **clean / OK** |

### Gotchas for the Phase 2 implementer

1. **`server.py` no longer rebinds `builtins.print` at import.** It was moved
   into `_protect_stdio_pipe()`, called from `main()` only — importing the
   server in-process (tests/tooling) used to globally redirect `print()` to
   stderr and silently broke capsys in later tests. Keep it in `main()`.
2. **Two test files stubbed `mcp` unconditionally** (`test_hitl_approval.py`,
   `test_agent_checkpoint_integration.py`); both now stub only when the real
   package is absent (matching conftest). If you add a test that imports
   `mcp_server.server`, do NOT reintroduce an unconditional `mcp` stub — it
   replaces `mcp.server` for the rest of the session.
3. **The approval channel is per-session, socket-based.** The broker binds a
   unix socket (POSIX) / loopback port (Windows) and injects
   `KIM_APPROVAL_SOCK`/`KIM_APPROVAL_TCP`. If Phase 3's app-server transport
   spawns codex with its own env, forward these two vars or codex-side
   approvals will default-deny.
4. **`hitl.rs` exists now** (the K1 spawn/stdin seam). When you do the full K2
   `TaskSpec`/`EnvBuilder`/`SpawnSupervisor` split, fold `hitl.rs` in or leave
   it as the approval-transport module — it is already the single owner of the
   `hitl_approve` line format for both the Tauri command and `/v1/task/approve`.
5. **Escalation rules fire even with no `hitl_risk_threshold` set** (full-auto
   mode still gates `python -c`, non-allowlisted binaries, `sudo`, force-push).
   This is intentional. If a Phase-2 change needs a genuinely ungated path, add
   it to the allowlist / `safe_extra`, don't bypass `enforce`.
6. **Pre-existing, machine-specific fixes made in passing (noted for
   provenance):** (a) `tests/codex_bridge_harness.py` now tolerates macOS's
   injected `LC_CTYPE`; (b) 3 clippy lints in `schedule_commands.rs` test code
   (Phase-0 commit, flagged only by newer local clippy) fixed. Neither touches
   Phase-1 logic. The CLI crate has ~9 *unfixed* newer-clippy lints in
   `config.rs`/`sessions.rs` — CI does not run clippy on the CLI crate, so they
   are out of scope; leave them for a dedicated cleanup or the CLI's own split.
