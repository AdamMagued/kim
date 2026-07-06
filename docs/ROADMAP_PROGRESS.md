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

---

## Phase 2 — Architecture & testability spine — DONE (2026-07-06, branch `feat/roadmap-to-10`)

Nine commits on top of Phase 1 (`5f0e4fe`):

1. **K2/A1/A3** `2ffba90` — `TaskSpec`/`EnvBuilder`/`SpawnSupervisor`; both spawn paths unified.
2. **K5/A2** `b2c9137` — event-manifest codegen owns the bracket-tag vocabulary.
3. **T5** `213f1cb` — `useChatStream` Vitest suite (25 tests).
4. **T4** `0f66614` — codex-proxy golden SSE tests (13 tests).
5. **T6/T7** `e3c0720` — offline E2E smoke through the real proxy + CI coverage gate.
6. **K3/T3** `38a8dd5` — injectable `PageDriver` seam + recorded-DOM contract tests.
7. **A4/T2(cli)** `617f41d` — `provider.rs` split (2252→588, 6 files) + `cli/tests/cli_flow.rs`.
8. **K5 followup** `5ce3808` — hold `utils.ts` under the size gate + refresh the src-tauri guide.

### K2 / A1 / A3 — spawn decomposition + dual-path unification (done)

- **`desktop/src-tauri/src/task_spec.rs`** (new, `pub`, 613 lines): the *pure*
  half. `TaskSpec` (program/args/cwd/envs/stdin/session/source/is_codex),
  `EnvBuilder` (ordered pair assembly: `orchestrator_base`, `permission_mode`,
  `webview_bridge`, `ollama`, `set_opt`), and three named builders —
  `chat_task_spec`, `codex_browser_spec`, `codex_direct_spec` — plus
  `promote_provider`, `is_browser_provider`, `run_id_for_session`,
  `ProviderRoute`. Nothing touches Tauri/tokio/fs; 11 unit tests.
- **`desktop/src-tauri/src/spawn_supervisor.rs`** (new, 150): the *effectful*
  half. `reserve_slot` (stale-pid recovery) → `spawn` (registers pid + stdin in
  `TaskRuntime`, releases slot on any failure) → `supervise` (stdout/stderr
  pumps through the one translator, pid-guarded `clear_if_pid`).
- **`send_task`** (`subprocess.rs`): **568 → 146 lines**, orchestrating
  `build_gui_chat_spec` / `build_gui_codex_spec` (async input resolution) →
  `spawn_supervisor`. The codex arm and Chrome-launch block are extracted
  helpers. **`/v1/task`** (`http_bridge.rs`) rebuilt on the SAME
  `chat_task_spec` builder + supervisor (detached supervision on the Tauri
  async runtime); its argv unit tests now call the real builder.
- **`TaskRuntime`**: the dual `gui_stdin`/`bridge_stdin` handles collapsed to
  one `stdin` (both paths now spawn a tokio child). `SpawnSource` moved to
  `task_spec` (pub) and re-exported.
- **`codex_route.rs`** (new, ~180): `configure_codex_direct_provider` +
  `selected_ollama_codex_model` extracted from `lib.rs`, now returning a pure
  `ProviderRoute` (`lib.rs` dropped below its baseline; Q6 gate green).
- **`tests/task_spawn.rs`** (new, T2): spawn-path parity matrix (GUI vs bridge
  argv/HITL contract can't diverge) + a behavioral fake-recorder spawn test
  that proves a `TaskSpec` is directly executable and reproduces its declared
  argv/env.
- **Three intentional unifications** (were latent GUI/bridge divergences):
  (a) the bridge path now pipes stderr to `kim-agent-error` (was `inherit`);
  (b) direct codex CLI runs no longer receive `KIM_GEMINI_AUTHUSER` (unused
  there); (c) the GUI chat run-id base is the session id (`gui-<ts>` when the
  run is unnamed). None change observable task behavior.

### K5 / A2 — event-manifest codegen for the log vocabulary (done)

- `scripts/gen-events.js` now emits **named tag constants** from the same
  `legacyTags` manifest: TS `LogTags.<NAME>` (`events.gen.ts`) and Python
  `LOG_TAG_<NAME>` (`events_gen.py`). Single source of truth for the
  `[STATUS]/[TOOL]/[ANSWER]/[PLAN]/…/TASK_COMPLETE:` vocabulary.
- Emitters retargeted: `codex_engine/engine.py` + `codex_bridge_service.py`
  print sites reference `LOG_TAG_*`; frontend `chat/utils.ts` +
  `chat/parsers.ts` reference `LogTags.*` (all plain literals + the
  `[PLAN]{`/`[STEP]{`/`[DONE]{` compound prefixes).
- `tests/test_events_codegen.py`: pins the constants to the manifest and
  asserts the codex emitters use them (drift guard). CI "Events schema drift
  check" still passes (regen is a no-op).

### A6 / R-2 — typed-IPC flip (code-complete; live step outstanding)

- Ground truth: the default was **already `typed`** on this branch
  (`config.rs default_ipc_protocol()`, `config.yaml.example`, and the three
  config parse tests). The frontend already wires **17 typed `KimEventNames`
  listeners** in `useChatStream.ts`, and `forward_agent_stdout_line` decodes
  into the generated `KimEvent` enum. So the *code* flip is done — there was
  nothing left to flip. What is NOT possible from a worktree is the **live-app
  confirmation** (below).
- Bonus finding: `emit_diff` was **already wired** into `agent.py` (:1355) —
  the older map's "hand-parsed, un-emitted" note for `[DIFF]` was stale.

### K3 / T3 — injectable PageDriver + recorded-DOM contract tests (done)

- **`orchestrator/providers/browser/page_driver.py`** (new): `PageDriver`
  protocol — `async acquire() -> (PageLike|None, site|None)` — plus
  `PageLike`/`LocatorLike`/`ElementLike`/`KeyboardLike` sub-protocols
  documenting the exact page surface the driver loop uses. A real Playwright
  Page satisfies it structurally (no code change to the real path).
- `BrowserProvider.__init__(config, page_driver=None)` +
  `complete(..., page_driver=None)` (call-site wins). The downstream driver
  body was extracted **verbatim** into `_run_chat_flow(page, site, …)`, shared
  by the real-CDP and injected-driver branches; the driver path skips the
  playwright import / `_connect` / `_find_chat_page`. `MARKDOWN_SERIALIZER_JS`
  moved to **`markdown_scraper.py`** to keep provider.py under the size gate
  (1465 → 1464).
- **`tests/fixtures/dom/`**: 4 synthesized response snapshots (chatgpt
  tool-call + code-fences, gemini text + tool-call), realistic nesting.
- **`tests/test_browser_dom_contract.py`** (11 tests): html.parser DOM shim +
  a Python mirror of the serializer + a `FakePageDriver` state machine; drives
  `complete()` end-to-end over the fixtures covering the completion-hash and
  stop-button exit paths, fence reconstruction (both language sources),
  gemini bare-JSON tool-call survival, driver precedence, `(None,None)` →
  NEED_HELP, and the injection-verification hard gate.

### A4 / T2(cli) — provider.rs split + cli integration tests (done)

- `cli/src/provider.rs` **2252 → 588** (facade: types, `PROVIDERS`,
  `stream_kim_request` router, message/prompt helpers, re-exports). New
  `cli/src/provider/`: `bridge.rs` (333), `llm_stream.rs` (239), `sse.rs`
  (501, incl. `ThinkParser`), `codex_stream.rs` (539), `responses_proxy.rs`
  (135, owns `include_str!("../responses_proxy.py")`). Pure structural move;
  31 inline tests redistributed; public surface for agentic/commands/main
  unchanged.
- **`cli/tests/cli_flow.rs`** (5 tests): drives the real `kim` binary
  (`CARGO_BIN_EXE_kim`) end-to-end against a std-only `TcpListener` fake HTTP
  server — chat SSE (incl. `<think>`), in-stream error payload, HTTP 500,
  `browser:*` bridge routing + token header, and the bridge-down actionable
  message. The binary is copied into a temp cwd so `find_kim_repo_root` takes
  the plain `stream_kim_request` path, not the agentic Python path.

### T4 / T5 / T6 / T7 — testing payload (done)

- **T4** `tests/test_codex_proxy_golden.py` (13): canned browser reply →
  `_provider_response_to_responses_api` → `_make_sse_response`; exact frame
  sequences for tool-call and text replies, bash-fence salvage, DONE-marker
  stripping, non-stream JSON path. **First** coverage of the SSE framing.
- **T5** `desktop/src/hooks/__tests__/useChatStream.test.tsx` (25): mocked
  Tauri `listen`/`invoke`; HITL flow, 800ms/2000ms dedup windows, context
  meter, typed `kim:*` events, the `kim-agent-done` finalizer
  (success/failure/provider-error precedence), cancellation, rate-limit
  auto-clear.
- **T6** `tests/test_e2e_smoke.py` (3): a fake codex binary does REAL loopback
  HTTP against the real `_CodexProxy` — bearer auth, translation, and SSE
  framing all live; only `BrowserProvider` is canned. Covers text reply,
  tool-call (function_call frames), and 401 on a wrong bearer.
- **T7** `.github/workflows/ci.yml`: pytest now runs with `pytest-cov` over
  `orchestrator+mcp_server+codex_engine`, `--cov-fail-under=55` (measured
  baseline **63%**; the floor is a ratchet). Rust integration dirs
  (`task_spawn.rs`, `cli/tests/`) run under the existing cargo jobs.

### Suite status at Phase 2 exit (all green, 2026-07-06)

| Suite | Result |
|---|---|
| `venv/bin/python -m pytest tests/` (CI ignores) + coverage | **1579 passed, 39 subtests; coverage 63.40% (gate 55)** |
| `desktop`: `tsc --noEmit` + `npm run test` | **tsc clean; 185 vitest (13 files)** |
| `desktop/src-tauri`: `cargo test` + `clippy --all-targets -D warnings` | **107 passed (101 unit + 6 integration); clippy clean** |
| `cli`: `cargo test --test-threads=1` + `fmt --check` | **141 passed (136 + 5 integration); fmt clean** |
| pyright (pyrightconfig: providers + mcp_server + codex_engine) | **0 errors** |
| flake8 orchestrator + new test files | **clean** |
| events schema drift / file-size gate | **no drift / OK** |

### Exit criteria — all met (code)

- ✅ `send_task` < 150 lines (146), orchestrating named builders.
- ✅ One `TaskRuntime` owns both spawn paths (`spawn_supervisor`).
- ✅ `BrowserProvider.complete()` accepts an injected driver.
- ✅ `tests/fixtures/dom/` exists (4 snapshots) with passing contract tests.
- ✅ `useChatStream` has a Vitest suite.
- ✅ CI runs integration (rust dirs) + coverage gate.
- ✅ The typed-IPC flip is DONE in code (was already `typed`; verified end to
  end in config + frontend listeners).

### LIVE-APP VERIFICATION STILL NEEDED (cannot run the GUI from a worktree)

A separate live-test agent (Sonnet + the running `npm run tauri dev` app)
should confirm — none of these are code blockers, they are the "prove it in
the real app" mile:

1. **Typed IPC end to end (A6/R-2).** Run a real Chat-tab task with a browser
   provider and watch the activity feed populate from `kim:status`/`kim:tool`/
   `kim:plan`/`kim:step`/`kim:answer`/`kim:context` (NOT the legacy
   `kim-agent-output` text path). Confirm the plan pills, step advance, token
   meter, and final answer all render. Then a **kimctl** run (`/v1/task`) and
   confirm the SAME typed events reach the desktop UI (the #33 fix path).
2. **Both spawn paths after the K2 rewrite.** (a) GUI Chat task (ollama +
   browser:gemini), (b) GUI Code task (codex direct + codex browser-bridge),
   (c) `kimctl` `/v1/task`. For each: task starts, streams, HITL approval
   round-trips (approve / acceptForSession / decline), stop button cancels,
   and no "A task is already running" wedge after completion or cancel.
3. **HITL over the unified stdin.** With `permission_mode=ask_always`, trigger
   a gated tool from BOTH a GUI run and a `kimctl` run and confirm the approval
   prompt appears and the decision unblocks the agent (the single
   `TaskRuntime.stdin` now serves both).
4. **Steering (`steer_task`).** Mid-run steer message folds in (uses the same
   single stdin handle).
5. **Codex browser-bridge status lines** still render (the `[STATUS] Routing
   to Codex via Kim's browser provider …` emit now happens inside
   `build_gui_codex_spec`).

### Gotchas for the Phase 3 (app-server parity) implementer

1. **Spawn changes belong in `task_spec.rs` builders, never inline.** The
   whole point of K2 was that GUI and `/v1/task` can no longer diverge — Phase
   3's app-server transport should add a new named builder (e.g.
   `codex_appserver_spec`) and route it through `spawn_supervisor`, not fork a
   third spawn body. `SpawnSource` is the enum to extend if you need a third
   provenance.
2. **The approval channel is per-session, socket-based** (unchanged from Phase
   1): `KIM_APPROVAL_SOCK`/`KIM_APPROVAL_TCP`. If the app-server transport
   spawns codex with its own env, forward these two vars or codex-side
   approvals default-deny. Put them in the spec's `extra_envs`.
3. **`TaskRuntime.stdin` is now a single tokio handle.** The native-approvals-
   over-stdin work in Part 2 writes through `write_stdin_line` — it already
   works for both paths. Don't reintroduce a second handle.
4. **Typed Kim events (Part 3) extend K5.** Add new events to
   `events.schema.json` and rerun `npm run gen:events`; the new approval/plan/
   diff events should also get `LOG_TAG_*`/`LogTags.*` constants if they have a
   text-protocol form. The drift check + the new
   `test_generated_named_tag_constants_match_manifest` will keep you honest.
5. **The T6 smoke (`tests/test_e2e_smoke.py`) is the app-server integration
   template.** It already exercises the real proxy over loopback with a fake
   codex; Part 1's `app_server.py` fake-server tests can reuse the
   `codex_bridge_harness` + this fake-binary-does-real-HTTP pattern.
6. **`PageDriver` is the seam for "browser is now model-only" (Part 2/5).**
   When tool execution moves native, the browser's job shrinks to
   `_run_chat_flow`; contract-test any wait-heuristic changes against the
   `tests/fixtures/dom/` snapshots via `FakePageDriver` (Rb2) rather than live.
7. **pytest-cov is now a CI dep and a local dep.** The gate is
   `--cov-fail-under=55` (baseline 63%). If a Phase-3 refactor adds a lot of
   untested transport code, either test it or the gate fails — do not lower the
   floor to pass.
8. **CLI clippy is still not in CI** and still has ~5 pre-existing
   `config.rs`/`sessions.rs` lints (untouched). The A4 split added zero new
   lints; keep it that way if you touch `cli/src/provider/`.
