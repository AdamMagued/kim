> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Kim — Improvement Plan v3
## 5/10 → 10/10

---

## ⚠️ BLOCKING DECISION: Relay Product Boundary

**Answer this before Phase 0 begins.**

The relay system is NOT dead code. Evidence:
- `railway.toml:6` — deploys `relay_server/` as a live service
- `Dockerfile:28,45` — packages relay_server into the container
- `desktop/src-tauri/src/relay.rs` — Tauri commands wired to relay
- `SettingsPanel.tsx:46,1173–1264` — full Phone Relay settings pane with `read_relay_config`/`write_relay_url` invoke calls
- `RevampSettings.tsx` — has **zero** relay UI

**Option A — Desktop only (relay deprecated):**
- `relay_server/` moves to `archive/`
- Phone Relay settings pane removed
- `relay.rs` Tauri commands removed
- `orchestrator/relay_worker.py` deleted (not just archived)

**Option B — Keep relay as a product surface (plan default):**
- `relay_server/`, `relay.rs`, Phone Relay settings pane stay
- `orchestrator/relay_worker.py` archived as "legacy tray relay worker" (the PC-side poller for the old tray architecture — not currently wired to the Tauri app, but removing it under Option B would be an inconsistent half-product)
- Phase 1 must port the Phone Relay section into `RevampSettings.tsx` before `SettingsPanel.tsx` is deleted

**This plan assumes Option B unless you say otherwise.**

---

## What "10/10" means (measurable exits)

| Rating | Criteria |
|--------|----------|
| **7.0** | Build clean; zero process artifacts at root; single UI implementation per surface; `lib.rs` ≤5500 lines |
| **7.5** | `ChatView.tsx` ≤500 lines; single config source of truth; integration test harness running |
| **9.0** | `kim-agent-output` demultiplexed into typed Tauri sub-events; zero regex parsing of agent output in TypeScript |
| **10.0** | Codex bridge consolidated (≤3 layers); full critical-path test coverage; CI green on all platforms |

---

## Constraints preserved in every phase

- Code tab never uses OpenAI auth or gpt-5.5 — only ollama cloud or browser provider
- `## FILE:` / `## CMD:` output format is untouched (browser extension depends on exact format)
- `BrowserProvider` `[END_OF_RESPONSE_{hash}]` sentinel is untouched
- `[UI] SCREENSHOT_FLASH` → hide window; `[UI] SHOW` → restore window protocol untouched
- Codex bridge: subprocess `env=` dict scoping never changed to `os.environ` mutation
- `tauri dev` restart required after any `lib.rs` change

---

## Phase -1 — Baseline & Worktree Protection

**Rating impact:** None (safety gate)
**Risk:** Zero
**Effort:** 30 minutes

1. **Clean the worktree.** `git status` — commit or stash all modified tracked files and untracked artifacts. A clean baseline commit is required so any phase can be surgically reverted.

2. **Confirm the build passes now.** Run `cargo build` from `desktop/` and `npm run build`. Record exact warning counts. New warnings introduced by any phase are a regression.

3. **Record baseline line counts:**
   - `wc -l desktop/src-tauri/src/lib.rs` — ~7476
   - `wc -l desktop/src/components/ChatView.tsx` — ~2773
   - `wc -l desktop/src/components/kim-ui/RevampSettings.tsx` — ~2022
   - `wc -l desktop/src/components/SettingsPanel.tsx` — ~1491

4. **Create a feature branch.** All phases work on a branch, not main. Each phase gets a logical commit so rollback is surgical.

### Acceptance criteria
- `git status` is clean
- `cargo build` exits 0
- `npm run build` exits 0
- Baseline warning count recorded

---

## Phase 0 — Artifact Cleanup & Doc Consolidation

**Rating impact:** 5/10 → 6/10
**Risk:** Low
**Effort:** 1 session

### 0a — Root process artifacts (safe to delete, not referenced anywhere)

```
contextOfChat.txt
full_update_files.txt
full_update_log.txt
full_update_report.md
kim_browser_bridge_ui_fixes.patch
kim_browser_final_answer_fixes.patch
kim_browser_stale_scrape_fixes.patch
kim-plan-implementation-bundle.zip
Kim Logo Demo.html
KimLogo.tsx                           ← stale root copy; real one in desktop/src/components/
Kim_Test/                             ← contains only wallpaper.txt
```

### 0b — Agent handoff archives

```
agent-handoff-browser-signin-detection/
agent-handoff-browser-signin-detection.zip
agent-handoff-context-budget/
agent-handoff-context-budget.zip
agent-handoff-gemini-google-oauth/
agent-handoff-gemini-google-oauth.zip
agent-handoff-kim-google-browser-link/
agent-handoff-kim-google-browser-link.zip
agent-handoff-kimtools-oauth/
agent-handoff-kimtools-oauth.zip
```

Before deleting: scan each for any decision or constraint not recorded elsewhere. Move any surviving fact to `ARCHITECTURE.md`. Then delete.

### 0c — Dead Python modules

**Delete:**
```
orchestrator/run_codex_relay.py       ← dead relay launcher (not run_codex_bridge.py)
tray/                                 ← Tkinter tray, fully superseded by Tauri
```

**Archive to `archive/` (not delete — Option B consistency):**
```
orchestrator/relay_worker.py          ← legacy tray relay worker (PC-side poller for old tray architecture,
                                         not wired to Tauri app; removing it would leave relay_server
                                         with no matching client under Option B)
```

`relay_server/` itself stays. `relay.rs` stays.

### 0d — Design mocks: surgical CSS extraction before deletion

`desktop/src/design-mocks/tokens.css` is imported by `main.tsx:3` — it is a **live CSS dependency**. Deleting the directory without extracting it first breaks the build.

Correct sequence:
1. Move `tokens.css` → `desktop/src/styles/design-tokens.css`
2. Check whether `styles.css` from design-mocks is imported anywhere; if yes, move it too
3. Update `main.tsx:3` import path to new location
4. `npm run build` — must pass before continuing
5. Delete the design-mocks `.tsx` files, `index.ts`, `tsconfig.json`, `README.md`
6. Delete the now-empty `design-mocks/` directory

The `.tsx` mock components (AppLaunchEmpty, ChatPlanCollapsible, ChatStreamHybrid, FullWindowShell, NewCodeSession, SettingsAbout, SettingsAccount, SettingsAi, SettingsAppearance, SettingsData, SettingsFeedback, SettingsMcp, SettingsPaths, SettingsVoice) are not imported anywhere in production — safe to delete after CSS is out.

### 0e — Doc consolidation

Currently 22 `.md` files at root.

**Keep and maintain:**
```
CHANGELOG.md
SECURITY_NOTES.md
claude.md            ← rename to CLAUDE.md (canonical project instructions)
repomap.md           ← update content to reflect current actual file tree
ARCHITECTURE.md      ← rewrite to describe current Tauri 2 architecture (currently stale Phase 1–6 planning)
IMPROVEMENT_PLAN.md  ← this file
```

**Move to `docs/archive/` (do not delete — may have decision history):**
```
AI_EDIT_GUIDE.md
AI_RESTRUCTURE_BASELINE.md
AI_RESTRUCTURE_FINAL_REPORT.md
COPILOT_HANDOFF.md
GEMINI_MODES.md
KIM_BROWSER_RELIABILITY_PATCH_NOTES.md
KIM_PROJECT_KNOWLEDGE_BASE.md
SECOND_PATCH_NOTES.md
kim_PRD.md
```

**Delete (pure stale task lists with no surviving decision value):**
```
BUGS_PENDING.md       ← superseded by git issues
CONTRACTS.md
SOURCE_OF_TRUTH.md    ← contradicts current state
STATE_FLOWS.md
TEST_MATRIX.md
TO_BE_DONE.md
```

**Update ARCHITECTURE.md** — rewrite to describe: Tauri 2 desktop app structure, Rust module layout, Python orchestrator + MCP server, IPC event protocol, codex bridge flow, browser provider, config sources. This becomes the canonical technical reference.

### Rollback note

Phase 0 is not purely deletion. It also **edits** these files:
- `main.tsx` — import path update for tokens.css
- `ARCHITECTURE.md` — content rewrite
- `repomap.md` — content update
- `claude.md` — rename to `CLAUDE.md`

Rollback = `git revert`. All deletions and moves are preserved in git history, and edits are also reversible.

### Acceptance criteria
- `find kim-pro -maxdepth 1 -name "*.patch"` → 0 results
- `find kim-pro -maxdepth 1 -name "agent-handoff*"` → 0 results
- `npm run build` exits 0 after tokens.css move
- `find kim-pro/tray` → "No such file"
- `find kim-pro -maxdepth 1 -name "*.md" | wc -l` → ≤8
- `cargo build` still exits 0

---

## Phase 1 — UI Deduplication (Parity Audit Before Any Deletion)

**Rating impact:** 6/10 → 6.5/10
**Risk:** Medium — visible UI change
**Effort:** 2–3 sessions

Two parallel implementations of every major surface exist side-by-side. The Revamp variants are actively rendered; the legacy variants are never reached at runtime.

| Component | Legacy | Revamp | Action |
|-----------|--------|--------|--------|
| Sidebar | `Sidebar.tsx` (867 lines) | `RevampSidebar.tsx` (1079 lines) | Audit, port, delete legacy |
| Settings | `SettingsPanel.tsx` (1491 lines) | `RevampSettings.tsx` (2022 lines) | **Audit first** — confirmed feature gaps |

### Phase 1a — Feature parity audit (mandatory before touching any file)

Read both `SettingsPanel.tsx` and `RevampSettings.tsx` side by side. Produce a section matrix: every `NavSection` in SettingsPanel vs RevampSettings. Mark each as **Present / Missing / Different**.

**Confirmed missing from RevampSettings (must be ported):**
- Phone Relay section — lines 1173–1264 in SettingsPanel, 21 relay references, full `invoke('read_relay_config')` / `invoke('write_relay_url')` integration (Option B only)

**To verify during audit:**
- Google account controls depth (OAuth scopes, account display, sign-out)
- Gemini OAuth integration details
- Any MCP server management features

### Phase 1b — Port missing features

For each feature marked Missing or Different in the audit:
1. Port into RevampSettings with matching Tauri invoke calls
2. Test each section renders and invokes correctly
3. Do not delete legacy until all Missing features are ported and verified

### Phase 1c — Delete legacy files

After verified parity:
```
delete: desktop/src/components/SettingsPanel.tsx
delete: desktop/src/components/Sidebar.tsx
```

Update all imports across the codebase. Run:
```
grep -rn "SettingsPanel" desktop/src/
grep -rn "from.*['\"].*Sidebar['\"]" desktop/src/
```
Both must return 0 results before this phase closes.

### Phase 1d — WorkedForPill: leave it alone

`WorkedForPill` is actively imported and rendered by `ChatView.tsx` at lines 10, 1692, 2690, 2693. It is NOT a deletion candidate.

### Acceptance criteria
- `grep -rn "SettingsPanel" desktop/src/` → 0 results
- `grep -rn "RevampSidebar\|from.*Sidebar" desktop/src/` → only canonical import
- Phone Relay section renders correctly in RevampSettings (Option B)
- Google/Gemini OAuth settings functional
- TypeScript compiles clean, zero new errors

### Rollback
Git revert. Legacy files preserved in history.

---

## Phase 2 — `lib.rs` Decomposition

**Rating impact:** 6.5/10 → 7.0/10
**Risk:** Medium (Rust; `tauri dev` restart required after each extraction)
**Effort:** 2 sessions

`lib.rs` is currently ~7476 lines. Already-extracted modules are correct and untouched.

### Extraction plan

**`http_bridge.rs`** (~400 lines)
The internal axum/tokio HTTP bridge: `/v1/send` and `/v1/result` handlers. Self-contained, no Tauri command macros. Extracted from ~lib.rs:2400–2800 area.

**`subprocess.rs`** (~600 lines)
`_run_agent_subprocess()`, `find_python_executable()`, `find_code_backend()`, `find_kim_md()`, process kill/cancel, process group management. Core agent lifecycle.

**`window_manager.rs`** (~200 lines)
`set_task_active_mode()`, `show_main_window()`, `hide_main_window()`, screenshot flash window management.

**`updater.rs`** (~150 lines)
`check_for_updates()` and auto-update logic.

**`bridge.js` (extracted via `include_str!`)** (~887 lines removed from lib.rs)
`PERSISTENT_BRIDGE_JS` spans lines 1358–2245 — ~887 lines of raw JavaScript embedded in a Rust string literal. Extract to `desktop/src-tauri/src/bridge.js` and replace the inline literal with:
```rust
const PERSISTENT_BRIDGE_JS: &str = include_str!("bridge.js");
```
This is the single biggest reduction and carries essentially zero logic risk — it's a string substitution.

### Honest extraction math

| Extraction | Estimated lines removed |
|------------|------------------------|
| `http_bridge.rs` | ~400 |
| `subprocess.rs` | ~600 |
| `window_manager.rs` | ~200 |
| `updater.rs` | ~150 |
| `bridge.js` via `include_str!` | ~887 |
| **Total** | **~2237** |

7476 − 2237 = **~5239 lines remaining**

**Phase 2 target: `lib.rs` ≤ 5500 lines** (honest, achievable with the above)

A second decomposition pass (Phase 2b, future) targeting ≤2000 lines would require extracting the pairing state machine, additional inline JS blocks (lines 2579–3546 area), and further command groupings. That scope is separate and not included here.

### Process per extraction

1. Identify all functions/types for the new module
2. Move to new file with `pub(crate)` visibility as needed
3. Add `mod new_module;` to `lib.rs`
4. `cargo check` after each move — fix visibility/circular issues immediately
5. `cargo build` after all moves — zero new warnings
6. Smoke test: `send_task`, `cancel_task`, `list_sessions`, `get_settings` all callable

### Acceptance criteria
- `wc -l desktop/src-tauri/src/lib.rs` → ≤5500
- `cargo build` exits 0, zero new warnings
- All Tauri commands callable from frontend (smoke test)

### Rollback
Module extraction is additive — any module can be inlined back if needed. Full `git revert` available.

---

## Phase 3 — `ChatView.tsx` Split

**Rating impact:** 7.0/10 → 7.3/10
**Risk:** Medium-high — UI regression risk
**Effort:** 2 sessions

`ChatView.tsx` is ~2773 lines. **Partially done:** `desktop/src/components/chat/utils.ts` and `desktop/src/components/chat/types.ts` already exist. Read both before starting Phase 3 to understand what is already extracted. Do not re-extract what already lives there.

### Phase 3a — `chat/parsers.ts` (pure functions first, no React)

Extract all remaining parsing logic from ChatView into a plain TypeScript module with no React dependencies. Pure functions in, typed data out — no hooks, no state, no side effects.

Functions to move here (verify against current ChatView state):
- Remaining `[STATUS]` / `[PLAN]` / `[STEP N]` / `[DONE N]` / `[CONTEXT]` / `[UI]` line parsing (if not already in `utils.ts`, or extend `utils.ts`)
- `parsePlanFromActivity()` (if still in ChatView)
- `buildThinkingTrace()` (if still in ChatView)
- Codex JSONL event parsing: `item.completed`, `agent_message`, `reasoning`, `local_shell_call` type discrimination

This module is fully unit-testable without a React environment (Phase 5 benefit).

Wire `chat/parsers.ts` to call any existing utilities in `chat/utils.ts` rather than duplicating them.

### Phase 3b — `hooks/useChatStream.ts` (React hook, calls parsers)

A React hook that:
- Subscribes to `kim-agent-output`, `kim-agent-error`, `kim-agent-done`, `kim-agent-cancelled`, `kim-agent-code-session` Tauri events
- Calls `chat/parsers.ts` functions to transform raw strings into typed structures
- Returns: `TraceItem[]`, `PlanStep[]`, `ActivityEntry[]`, `lastStatus`, `contextUsage`, `isDone`, `isCancelled`

Keeps the hook narrow: React lifecycle + event wiring only. All parsing logic lives in `parsers.ts`, not here.

### Phase 3c — `hooks/useSessionScroll.ts`

Scroll-to-bottom logic, auto-scroll-on-new-message, manual scroll override detection. ~80 lines, self-contained.

### Phase 3d — `components/chat/StreamRenderer.tsx`

Pure rendering component: receives `TraceItem[]` and `ActivityEntry[]` from the hook, renders the visual feed. No local state, no Tauri calls.

### Phase 3e — ChatView as orchestrator

After extraction, ChatView:
- Imports and wires `useChatStream`, `useSessionScroll`
- Holds only task submission state (input value, loading flag, cancel handle)
- Renders `<StreamRenderer>`, `<ThinkingWithPlan>`, `<WorkedForPill>`, `<CancelWidget>`
- No parsing logic

**Target: `ChatView.tsx` ≤ 500 lines.**

### Acceptance criteria
- `wc -l desktop/src/components/ChatView.tsx` → ≤500
- `chat/parsers.ts` calls existing `chat/utils.ts` utilities, no duplication
- Plan cards, step cards, screenshot flash, context ring, WorkedForPill all render identically
- TypeScript compiles clean

---

## Phase 4 — Config Consolidation

**Rating impact:** 7.3/10 → 7.5/10
**Risk:** Low-medium
**Effort:** 1 session

Config currently spans 6 locations. Python is mostly fine — `mcp_server/config.py` already reads `config.yaml` (confirmed at line 15). Don't re-architect what works.

**What actually needs doing:**

### Rust: create `config.rs`

Define `AppConfig` struct with `serde::Deserialize`. At startup, read `config.yaml` → deserialize → store in Tauri managed state. Commands that currently hardcode values read from managed `AppConfig` instead.

Move out of `lib.rs`:
- Default model name strings
- Bridge timeout values
- Screenshot flash duration constant
- Max iterations default

**Security constraint:** API keys stay in `.env` only. `AppConfig` must not expose secrets. Only non-sensitive runtime settings go into managed state.

### `config.yaml.example`

Update to be comprehensive — every supported key documented with inline comments. This becomes the canonical operator reference.

### What to leave alone
- `tauri.conf.json` — app identity and window config, not runtime-tunable
- `mcp_server/config.py` — already correct
- `.env` / `.env.example` — secret storage stays as-is

### Acceptance criteria
- No bare string literals for model names or timeout values remain in `lib.rs`
- `AppConfig` struct deserializes cleanly from `config.yaml.example`
- Changing `config.yaml` values affects behavior without code change
- No API key accessible via any Tauri command

---

## Phase 5 — Integration Test Harness

**Rating impact:** 7.5/10 → 7.5/10 (quality gate, prerequisite for Phase 6)
**Risk:** Additive — zero regression risk
**Effort:** 2 sessions

Current state: one Rust unit test in `lib.rs`. `tests/` exists but near-zero critical path coverage. This phase creates the safety net Phase 6 requires before starting.

### Phase 5a — Framework setup (required first)

**Frontend — Vitest is not installed.** Before writing any TypeScript tests:
```bash
npm install --save-dev vitest @vitest/ui jsdom @testing-library/react
```
Add `"test": "vitest"` to `desktop/package.json` scripts. Configure `vitest.config.ts` with jsdom environment.

**Python — pytest is available.** Add `pytest-asyncio` if missing. Create `tests/conftest.py` with shared fixtures.

### Phase 5b — TypeScript tests for `chat/parsers.ts`

Possible only after Phase 3 extracts parsers into a testable module:

```
test_parsePlanFromActivity — given [PLAN]{json} lines, assert correct PlanStep[] output
test_buildThinkingTrace    — all item types produce correct trace shape
test_codex_jsonl_parse     — item.completed events → correct message shapes
test_status_line_variants  — [STATUS], [STEP N], [DONE N], [CONTEXT] each parsed correctly
```

### Phase 5c — Python tests

```
tests/test_session_store.py       — write JSONL, read back, assert messages identical
tests/test_context_meter.py       — charge/budget/overflow edge cases
tests/test_codex_env_scoping.py   — subprocess env= dict scoping, not os.environ mutation (regression)
tests/test_agent_plan_parsing.py  — mock stdout lines → correct plan structure
tests/test_ollama_provider.py     — mock HTTP, verify tool call normalization
```

### Phase 5d — Rust tests

```rust
// subprocess.rs:
#[test] fn test_find_python_finds_venv()
// session_commands.rs:
#[test] fn test_session_load_roundtrip()
// config.rs (Phase 4):
#[test] fn test_config_load_from_yaml()
```

### Coverage target

Aim to **protect behavior first**. Don't gate the phase on a specific coverage percentage — that leads to spending sessions arguing with `coverage.py` rather than protecting critical paths. Once tests are stable and passing, add coverage reporting to CI as an informational check (not a hard gate until coverage is naturally ≥60%).

### Acceptance criteria
- `pytest tests/` passes with all new tests green
- `npm run test` passes for `chat/parsers.ts` tests
- CI (`ci.yml`) runs both test suites on push
- These tests must be passing before Phase 6 begins

---

## ⚠️ DECISION REQUIRED — Phase 6: Typed IPC Migration

**Threshold: ~7.5/10 → ~9/10. Confirm before starting.**

### What actually needs migrating

Three of the five Tauri events are already typed:
- `kim-agent-done` — `boolean` (already typed)
- `kim-agent-code-session` — `SessionInfo` (already typed)
- `kim-agent-cancelled` — `boolean` (already typed)

The migration is specifically about **demultiplexing `kim-agent-output`** — a single event carrying raw string lines that ChatView parses with `startsWith` and regex — into typed sub-events.

**Current:**
```
Python:    print("[STATUS] message")              → stdout
Python:    print(f"[PLAN]{json.dumps(plan)}")     → stdout
Python:    print(f"[STEP {n}]:{json.dumps(s)}")   → stdout
Python:    print("[UI] SCREENSHOT_FLASH")         → stdout

Rust:      reads line → if line.starts_with("[STATUS]") ...
Rust:      app.emit("kim-agent-output", raw_line) → TypeScript

TypeScript: listen("kim-agent-output") → startsWith / regex parsing
```

**Target:**
```
Python:    print(json.dumps({"type":"status","message":...}), flush=True)

Rust:      serde_json::from_str::<KimEvent>(line)?
Rust:      match event.type {
             "status" => app.emit("kim:status", &payload)?,
             "plan"   => app.emit("kim:plan",   &payload)?,
             "step"   => app.emit("kim:step",   &payload)?,
             "done"   => app.emit("kim:done",   &payload)?,
             ...
           }

TypeScript: listen<KimStatusEvent>("kim:status", e => { ... })
            listen<KimPlanEvent>("kim:plan",    e => { ... })   // zero regex
```

### Migration strategy: dual-emit during cutover

This is a **one-way door** once the legacy path is removed. Use a dual-emit period:

1. Add `ipc_protocol: "legacy" | "typed"` to `AppConfig` (Phase 4 struct). Default: `"legacy"`.
2. In `typed` mode, Rust emits both `kim-agent-output` (old) AND the new typed sub-events simultaneously.
3. TypeScript adds new typed listeners alongside the existing `kim-agent-output` listener.
4. Phase 5 tests verify typed listeners produce the same result as legacy string parsing.
5. Flip default to `"typed"`. Delete legacy path after confidence period.

### Scope

**Python (agent.py):** Replace every `print(f"[TYPE] ...")` with `print(json.dumps({...}), flush=True)`. Same stdout transport — only wire format changes.

**Rust (subprocess.rs / lib.rs):** Replace `starts_with` line matching with `serde_json::from_str::<KimEvent>`. Define `KimEvent` enum. Emit typed Tauri events.

**TypeScript (useChatStream / ChatView):** Add per-event typed listeners. Each has a TypeScript interface matching the Rust struct. `chat/parsers.ts` from Phase 3 still handles Codex JSONL events (those come through a different path).

### Cost
- **3–4 sessions**
- **High coordination** — touches Python, Rust, and TypeScript simultaneously
- Requires Phase 5 test suite as safety net before starting
- Session replay of old-format JSONL sessions must still work after migration

**→ Say "confirm Phase 6" to proceed.**

---

## ⚠️ DECISION REQUIRED — Phase 7: Codex Bridge Consolidation

**Threshold: ~9/10 → 10/10. Confirm before starting.**

### Current architecture (5 layers)

```
Tauri (lib.rs / subprocess.rs)
  → python -m orchestrator.run_codex_bridge     [launcher script — thin wrapper]
    → mcp_server/tools/codex_bridge.py           [proxy setup + codex spawn]
      → _CodexProxy (aiohttp, ephemeral port)    [OpenAI-format interceptor]
        → codex exec --json ...                  [Codex CLI subprocess]
          → BrowserProvider.complete()           [browser LLM via CDP]
```

**Problems:**
- 5 independent failure points; any layer can hang/crash without notifying the outer layers
- Cancellation doesn't propagate: killing `run_codex_bridge` doesn't guarantee `_CodexProxy` or the codex process die
- `_CodexProxy` ephemeral port leaks if outer process crashes abnormally
- Codex CLI stderr is swallowed by the proxy and never surfaced to the user

### Target: 3 layers (conservative merge, not a rewrite)

**Do NOT attempt to eliminate the aiohttp proxy or call BrowserProvider directly from Tauri.** That would require reimplementing what Codex CLI does. The proxy stays.

Merge the two thin Python launcher files into one coherent module:

```
Tauri (lib.rs / subprocess.rs)
  → orchestrator/codex_bridge_service.py         [merged launcher + proxy, single entry point]
    → _CodexProxy (aiohttp, ephemeral port)      [same proxy, improved lifecycle]
      → codex exec --json ...                    [Codex CLI — unchanged]
```

**Changes:**
1. Merge `orchestrator/run_codex_bridge.py` and `mcp_server/tools/codex_bridge.py` into `orchestrator/codex_bridge_service.py`
2. `run_codex_bridge.py` was a thin launcher calling `run_codex_subtask()` — no behavior lost in the merge
3. Implement `atexit` + `signal.SIGTERM` handler: guarantee proxy server shutdown and codex process kill on exit
4. Surface codex subprocess stderr back through the IPC protocol (currently silently dropped)
5. Use a named `tempfile.TemporaryDirectory` with `__exit__` cleanup, not ad-hoc temp file creation

**What this does NOT do:**
- Does not change the Codex CLI invocation
- Does not change BrowserProvider behavior
- Does not eliminate the aiohttp proxy

### Cost
- **2–3 sessions**
- **Medium-high risk** — working today; surgery on live tissue
- Requires Phase 5 test coverage to verify behavior preservation
- Requires update to `lib.rs`/`subprocess.rs` Tauri spawn call to point to new entry point

**→ Say "confirm Phase 7" to proceed.**

---

## Execution Summary

| Phase | Description | Sessions | Risk | Rating after |
|-------|-------------|----------|------|-------------|
| -1 | Baseline + worktree | 0.5 | None | 5/10 |
| 0 | Artifact cleanup + docs | 1 | Low | 6/10 |
| 1 | UI dedup (parity audit first) | 2–3 | Medium | 6.5/10 |
| 2 | lib.rs decomposition | 2 | Medium | 7.0/10 |
| 3 | ChatView split | 2 | Medium | 7.3/10 |
| 4 | Config consolidation | 1 | Low | 7.5/10 |
| 5 | Integration tests | 2 | None | 7.5/10 ✓ |
| **6** | **Typed IPC migration** | **3–4** | **High** | **9.0/10** |
| **7** | **Codex bridge consolidation** | **2–3** | **Med-High** | **10/10** |

**Phases -1 through 5:** ~10–11 sessions, low-medium risk, all reversible, gets to 7.5.
**Phase 6:** 3–4 sessions, high risk, one-way door (dual-emit safety net mitigates). Gets to 9.
**Phase 7:** 2–3 sessions, medium-high risk. Gets to 10.

---

*Say "start Phase -1" to begin. Say "confirm Phase 6" or "confirm Phase 7" at any time to add those phases to the execution queue.*
