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
