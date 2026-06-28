# Kim — Agent Execution Prompts

> **STATUS: COMPLETED / SUPERSEDED**
> All prompts in this file (Phases -1 through 7) have been executed and merged.
> The playbook that governs active ongoing work is **`PRODUCTION_ROADMAP.md`** (authoritative).
> This file is retained as a decision archive — do not re-run these prompts.

Each prompt below is self-contained. To use one, tell an agent:
> "Read `kim-pro/AGENT_PROMPTS.md` and execute Prompt N."

The agent will find exactly what to do, where to look, and how to confirm it's done.
All prompts assume the working directory is `kim-pro/` unless stated otherwise.

---

## Prompt 1 — Phase -1: Baseline & Worktree Protection

Read `kim-pro/IMPROVEMENT_PLAN.md` — the "Phase -1" section is your spec.

Your job is to establish a clean, verified baseline before any other work begins. Do not skip this — every later phase depends on it.

**Steps:**
1. `git status` from `kim-pro/`. If there are modified tracked files or untracked artifacts, commit them or stash them. The goal is a clean tree.
2. Create a feature branch (e.g. `kim-improvement`) and switch to it. All improvement work happens on this branch.
3. Run `cargo build` from `kim-pro/desktop/`. Record the number of warnings in your response.
4. Run `npm run build` from `kim-pro/desktop/`. Record the number of warnings/errors.
5. Record these baseline line counts (run `wc -l` on each):
   - `desktop/src-tauri/src/lib.rs`
   - `desktop/src/components/ChatView.tsx`
   - `desktop/src/components/kim-ui/RevampSettings.tsx`
   - `desktop/src/components/SettingsPanel.tsx`
6. Commit the clean baseline with message: `chore: baseline commit before improvement phases`

**Acceptance criteria (all must pass before you declare done):**
- `git status` shows clean tree
- `cargo build` exits 0
- `npm run build` exits 0
- Baseline warning counts recorded
- Feature branch created and active

**Do not touch any source files. This is observation only.**

---

## Prompt 2 — Phase 0a+0b: Root Process Artifacts & Archive Cleanup

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 0 / 0a" and "Phase 0 / 0b" are your spec.

**Part A — Root process artifacts (0a):**

Delete these files/directories from `kim-pro/` root (they are not imported or referenced anywhere):
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
KimLogo.tsx
Kim_Test/
```

Before deleting `KimLogo.tsx` from root, confirm the real component lives at `desktop/src/components/KimLogo.tsx` (or similar). If so, the root copy is a stale duplicate — delete it. If not, do NOT delete it.

**Part B — Agent handoff archives (0b):**

Before deleting each archive directory, scan it quickly for any decision, constraint, or behavioral rule not already documented in `ARCHITECTURE.md` or `IMPROVEMENT_PLAN.md`. If you find something, add a bullet to `ARCHITECTURE.md` under a "Preserved decisions" section. Then delete:
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

**After cleanup, commit with message:** `chore(phase-0a-0b): delete root artifacts and agent handoff archives`

**Acceptance criteria:**
- `find kim-pro -maxdepth 1 -name "*.patch"` → 0 results
- `find kim-pro -maxdepth 1 -name "agent-handoff*"` → 0 results
- `cargo build` still exits 0
- `npm run build` still exits 0

---

## Prompt 3 — Phase 0c+0d: Dead Python Modules & Design Mocks CSS Extraction

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 0 / 0c" and "Phase 0 / 0d" are your spec.

**Part C — Dead Python modules (0c):**

Delete:
- `orchestrator/run_codex_relay.py` — dead relay launcher (not the same as `run_codex_bridge.py`, which is live — do NOT delete that one)
- `tray/` directory — fully superseded by the Tauri app

Archive to `kim-pro/archive/` (create the directory if it doesn't exist):
- `orchestrator/relay_worker.py` → `archive/relay_worker.py`

`relay_server/` stays untouched. `relay.rs` stays untouched.

**Part D — Design mocks CSS extraction (0d):**

`design-mocks/tokens.css` is imported by `desktop/src/main.tsx:3`. You MUST move it before deleting the directory or the build will break.

Exact sequence — do not skip any step:
1. Read `desktop/src/design-mocks/tokens.css`
2. Check whether `desktop/src/design-mocks/styles.css` exists; if yes, check if it's imported anywhere with `grep -rn "design-mocks/styles" desktop/src/`
3. Create `desktop/src/styles/` directory
4. Move `tokens.css` → `desktop/src/styles/design-tokens.css`
5. If `styles.css` was imported, move it too
6. Update `desktop/src/main.tsx:3` import path from `./design-mocks/tokens.css` to `./styles/design-tokens.css`
7. Run `npm run build` from `desktop/` — must pass before continuing
8. Delete only the `.tsx` mock components, `index.ts`, `tsconfig.json`, `README.md` inside `design-mocks/`
9. Delete the now-empty `design-mocks/` directory

**Commit:** `chore(phase-0c-0d): remove dead Python modules, extract CSS, delete design mocks`

**Acceptance criteria:**
- `find kim-pro/tray` → "No such file or directory"
- `npm run build` exits 0 after CSS move
- `grep -rn "design-mocks" desktop/src/` → 0 results
- `cargo build` exits 0

---

## Prompt 4 — Phase 0e: Doc Consolidation

Read `kim-pro/IMPROVEMENT_PLAN.md` — section "Phase 0 / 0e" is your spec.

**Step 1 — Create `docs/archive/` directory** at `kim-pro/docs/archive/`

**Step 2 — Move these files to `docs/archive/`** (do not delete — they may have decision history):
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

**Step 3 — Delete these** (pure stale task lists):
```
BUGS_PENDING.md
CONTRACTS.md
SOURCE_OF_TRUTH.md
STATE_FLOWS.md
TEST_MATRIX.md
TO_BE_DONE.md
```

**Step 4 — Rename:** `claude.md` → `CLAUDE.md`

**Step 5 — Update `repomap.md`:** Rewrite it to accurately reflect the current actual file tree. Use `find` to generate a fresh directory listing and update the descriptions. Remove references to files that no longer exist after Phases 0a–0d.

**Step 6 — Rewrite `ARCHITECTURE.md`:** Replace the current stale content with a description of the actual running system:
- Tauri 2 desktop app structure (Rust backend + React 19 frontend)
- Rust module layout (lib.rs, ollama.rs, relay.rs, google_oauth.rs, build.rs)
- Python orchestrator + MCP server (KimAgent loop, MCP stdio transport, 50 tools)
- IPC event protocol: the 5 Tauri events and the stdout text protocol (`[STATUS]`, `[PLAN]{json}`, `[STEP N]:{json}`, `[DONE N]`, `[CONTEXT]{json}`, `[UI] SCREENSHOT_FLASH`, `[UI] SHOW`)
- Codex bridge flow (5 layers: Tauri → run_codex_bridge.py → codex_bridge.py → _CodexProxy → Codex CLI → BrowserProvider)
- Browser provider behavior and `[END_OF_RESPONSE_{hash}]` sentinel
- Config sources: config.yaml, .env, tauri.conf.json, lib.rs constants

**Commit:** `docs(phase-0e): consolidate docs, rewrite ARCHITECTURE.md and repomap.md`

**Acceptance criteria:**
- `find kim-pro -maxdepth 1 -name "*.md" | wc -l` → ≤8
- `ARCHITECTURE.md` describes the actual current system
- `repomap.md` matches actual current file tree
- `cargo build` exits 0, `npm run build` exits 0

---

## Prompt 5 — Phase 1a+1b: Settings Parity Audit & Port Missing Features

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 1a" and "Phase 1b" are your spec.

**This prompt is research + porting only. Do NOT delete any files yet.**

**Step 1 — Read both files completely:**
- `desktop/src/components/SettingsPanel.tsx`
- `desktop/src/components/kim-ui/RevampSettings.tsx`

**Step 2 — Produce a section matrix** in your response listing every `NavSection` from SettingsPanel and its status in RevampSettings: Present / Missing / Different.

Confirmed missing (must be ported):
- **Phone Relay section** — SettingsPanel lines 1173–1264. Contains: `type NavSection` includes `'relay'`, nav item `{ id: 'relay', label: 'Phone Relay', icon: <PhoneIcon /> }`, full settings pane with `invoke<RelayConfigSnapshot>('read_relay_config')` and `invoke('write_relay_url')`.

Also verify during audit:
- Google account controls (OAuth scopes, account display, sign-out)
- Gemini OAuth integration details
- Any MCP server management features

**Step 3 — For each Missing/Different feature:**
1. Port it into `RevampSettings.tsx` with matching Tauri invoke calls
2. Match the visual structure of existing RevampSettings sections (same spacing, same component patterns)
3. Do not add new invoke calls for things that already exist in RevampSettings

**After porting, run:** `npx tsc --noEmit` from `desktop/` — must exit 0.

**Commit:** `feat(phase-1a-1b): port missing settings sections (relay, etc) to RevampSettings`

**Acceptance criteria:**
- Section matrix documented in your commit message or a comment
- Phone Relay section renders in RevampSettings with working `read_relay_config`/`write_relay_url` invoke calls
- TypeScript compiles clean, zero new errors
- Do NOT delete SettingsPanel.tsx yet — that's Prompt 6

---

## Prompt 6 — Phase 1c: Delete Legacy UI Files

Read `kim-pro/IMPROVEMENT_PLAN.md` — section "Phase 1c" is your spec.

**This prompt requires Prompt 5 to be complete and verified first.**

**Step 1 — Find all imports of the legacy files:**
```bash
grep -rn "SettingsPanel" desktop/src/
grep -rn "from.*['\"].*Sidebar['\"]" desktop/src/
```

**Step 2 — Update every import** that references `SettingsPanel` or `Sidebar` (the legacy one) to point to `RevampSettings` or `RevampSidebar` instead.

**Step 3 — Delete:**
```
desktop/src/components/SettingsPanel.tsx
desktop/src/components/Sidebar.tsx
```

**Step 4 — Compile check:** `npx tsc --noEmit` from `desktop/` must exit 0.

**Step 5 — Build check:** `npm run build` must exit 0.

**Commit:** `refactor(phase-1c): delete legacy SettingsPanel and Sidebar`

**Acceptance criteria:**
- `grep -rn "SettingsPanel" desktop/src/` → 0 results
- `grep -rn "from.*['\"].*Sidebar['\"]" desktop/src/` → only the canonical RevampSidebar import
- TypeScript compiles clean
- `npm run build` exits 0

**Note: Do NOT touch WorkedForPill. It is actively rendered by ChatView.tsx and is not a deletion candidate.**

---

## Prompt 7 — Phase 2: lib.rs Decomposition

Read `kim-pro/IMPROVEMENT_PLAN.md` — "Phase 2" is your spec.

`lib.rs` is currently ~7476 lines. Your goal is to extract identifiable units into separate Rust modules and extract the embedded JavaScript to an external file via `include_str!`.

**Step 1 — Extract `PERSISTENT_BRIDGE_JS` to `bridge.js` (do this first — biggest reduction, lowest risk):**
- `PERSISTENT_BRIDGE_JS` is a Rust raw string literal spanning approximately lines 1358–2245 of `lib.rs` (~887 lines of JavaScript)
- Create `desktop/src-tauri/src/bridge.js` containing exactly the JavaScript content from that literal (without the surrounding Rust `r#"..."#` delimiters)
- Replace the inline literal in `lib.rs` with:
  ```rust
  const PERSISTENT_BRIDGE_JS: &str = include_str!("bridge.js");
  ```
- Run `cargo check` — fix any issues before continuing

**Step 2 — Extract `http_bridge.rs` (~400 lines):**
- Identify the internal axum/tokio HTTP bridge: `/v1/send` and `/v1/result` handlers in `lib.rs`
- Move them to `desktop/src-tauri/src/http_bridge.rs`
- Add `mod http_bridge;` to `lib.rs`
- Use `pub(crate)` for anything referenced from `lib.rs`
- Run `cargo check` after

**Step 3 — Extract `subprocess.rs` (~600 lines):**
- Move `_run_agent_subprocess()`, `find_python_executable()`, `find_code_backend()`, `find_kim_md()`, process kill/cancel, process group management
- Add `mod subprocess;` to `lib.rs`
- Run `cargo check` after

**Step 4 — Extract `window_manager.rs` (~200 lines):**
- Move `set_task_active_mode()`, `show_main_window()`, `hide_main_window()`, screenshot flash window management
- Add `mod window_manager;` to `lib.rs`
- Run `cargo check` after

**Step 5 — Extract `updater.rs` (~150 lines):**
- Move `check_for_updates()` and auto-update logic
- Add `mod updater;` to `lib.rs`
- Run `cargo check` after

**Step 6 — Final build:**
- `cargo build` from `desktop/` — must exit 0 with zero new warnings

**Commit:** `refactor(phase-2): decompose lib.rs into modules, extract bridge.js`

**Acceptance criteria:**
- `wc -l desktop/src-tauri/src/lib.rs` → ≤5500
- `cargo build` exits 0, zero new warnings
- All Tauri commands still callable (smoke test: open the app and verify `send_task` and `get_settings` work)

**Important:** Run `cargo check` after EACH extraction step. Do not batch them. Fix any visibility or circular dependency issue before moving to the next extraction.

---

## Prompt 8 — Phase 3a+3b: ChatView Parsing Extraction & useChatStream Hook

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 3a" and "Phase 3b" are your spec.

**Before starting, read these files to understand what's already extracted:**
- `desktop/src/components/chat/utils.ts`
- `desktop/src/components/chat/types.ts`

Do not re-extract anything that already lives there.

**Step 1 — Create `desktop/src/components/chat/parsers.ts`:**

Extract all remaining parsing logic from `ChatView.tsx` that is not already in `utils.ts`. This file must have:
- Zero React imports
- Zero hooks
- Only pure TypeScript functions: input in, typed data out

Functions to move (check if each still lives in ChatView before moving):
- `[STATUS]` / `[PLAN]{json}` / `[STEP N]:{json}` / `[DONE N]` / `[CONTEXT]{json}` / `[UI]` line parsing
- `parsePlanFromActivity()` (if still in ChatView)
- `buildThinkingTrace()` (if still in ChatView)
- Codex JSONL event parsing: `item.completed` → discriminate between `agent_message`, `reasoning`, `local_shell_call` item types

Wire `parsers.ts` to call utilities already in `utils.ts` — no duplication.

**Step 2 — Create `desktop/src/hooks/useChatStream.ts`:**

A React hook that:
- Subscribes to these 5 Tauri events (exact names):
  - `kim-agent-output` → `listen<string>(...)`
  - `kim-agent-error` → `listen<string>(...)`
  - `kim-agent-done` → `listen<boolean>(...)`
  - `kim-agent-cancelled` → `listen<boolean>(...)`
  - `kim-agent-code-session` → `listen<SessionInfo>(...)`
- Calls functions from `chat/parsers.ts` to transform raw strings into typed structures
- Returns: `{ traceItems: TraceItem[], planSteps: PlanStep[], activityEntries: ActivityEntry[], lastStatus: string, contextUsage: number, isDone: boolean, isCancelled: boolean }`
- Handles unlisten cleanup on unmount

The hook is ONLY responsible for event wiring and React lifecycle. All parsing logic lives in `parsers.ts`.

**Step 3 — Type check:** `npx tsc --noEmit` must exit 0.

**Commit:** `refactor(phase-3a-3b): extract chat/parsers.ts and useChatStream hook`

**Acceptance criteria:**
- `parsers.ts` has zero React imports
- `useChatStream.ts` contains no regex or string parsing — only calls to parsers
- `chat/parsers.ts` calls `chat/utils.ts` utilities, no duplication
- TypeScript compiles clean
- Do NOT modify ChatView yet — that's Prompt 9

---

## Prompt 9 — Phase 3c+3d+3e: Remaining Hooks, StreamRenderer & ChatView Wiring

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 3c", "Phase 3d", and "Phase 3e" are your spec.

**This prompt requires Prompt 8 to be complete first.**

**Step 1 — Create `desktop/src/hooks/useSessionScroll.ts`:**
- Scroll-to-bottom logic
- Auto-scroll on new message
- Manual scroll override detection (user scrolled up → stop auto-scrolling)
- ~80 lines, no Tauri dependencies

**Step 2 — Create `desktop/src/components/chat/StreamRenderer.tsx`:**
- Pure rendering component
- Props: `traceItems: TraceItem[]`, `activityEntries: ActivityEntry[]`
- Renders the visual feed exactly as ChatView currently renders it
- No local state, no Tauri calls, no event listeners

**Step 3 — Refactor `ChatView.tsx` to be an orchestrator:**
- Import and wire `useChatStream`, `useSessionScroll`
- Import `StreamRenderer`
- ChatView keeps only: task input state (input value, loading flag, cancel handle) and the top-level layout
- Render: `<StreamRenderer>`, `<ThinkingWithPlan>`, `<WorkedForPill>`, and the cancel button/input area
- Remove all parsing logic — it now lives in `parsers.ts` and `useChatStream`

**Step 4 — Verify visually (if dev server is available):**
- Start `npm run dev` from `desktop/`
- Send a test task and confirm: plan cards appear, step cards appear, context ring updates, WorkedForPill renders
- If dev server is not available, at minimum confirm `npm run build` exits 0

**Commit:** `refactor(phase-3c-3e): split ChatView into hooks + StreamRenderer, wire up`

**Acceptance criteria:**
- `wc -l desktop/src/components/ChatView.tsx` → ≤500
- `npx tsc --noEmit` exits 0
- `npm run build` exits 0
- All parsing logic is gone from ChatView

---

## Prompt 10 — Phase 4: Config Consolidation

Read `kim-pro/IMPROVEMENT_PLAN.md` — "Phase 4" is your spec.

**Step 1 — Create `desktop/src-tauri/src/config.rs`:**

Define an `AppConfig` struct with `serde::Deserialize`:
```rust
#[derive(Debug, Deserialize, Clone)]
pub struct AppConfig {
    pub default_model: HashMap<String, String>,  // per-provider
    pub bridge_timeout_secs: u64,
    pub screenshot_flash_duration_ms: u64,
    pub max_iterations: u32,
    // add others as you find hardcoded values in lib.rs
}
```

At app startup, read `config.yaml` → deserialize into `AppConfig` → store as Tauri managed state with `app.manage(config)`.

Commands that currently hardcode values should read from `State<AppConfig>` instead.

**Step 2 — Move hardcoded values out of `lib.rs`/`subprocess.rs`:**

Search for and replace bare string literals for:
- Default model names (e.g. `"claude-opus-4-6"`, `"gpt-4o"`, etc.)
- Bridge timeout values
- Screenshot flash duration constant
- Max iterations default

**Step 3 — Update `config.yaml.example`:**
Make it comprehensive — every supported key documented with inline comments. This is the canonical operator reference.

**Security constraint:** API keys must stay in `.env` only. `AppConfig` must not expose any secrets. If you find any command that returns API key values, do not include those in `AppConfig`.

**Step 4 — Build check:** `cargo build` from `desktop/` must exit 0.

**Commit:** `refactor(phase-4): centralize runtime config in AppConfig, config.rs`

**Acceptance criteria:**
- No bare string literals for model names or timeout values remain in `lib.rs`
- `AppConfig` struct deserializes cleanly from `config.yaml.example`
- No API key accessible via any Tauri command
- `cargo build` exits 0

**Leave alone:** `tauri.conf.json` (app identity), `mcp_server/config.py` (already correct), `.env`/`.env.example` (secret storage).

---

## Prompt 11 — Phase 5a+5b: Test Framework Setup & TypeScript Tests

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 5a" and "Phase 5b" are your spec.

**This prompt requires Prompt 8 (parsers.ts extracted) to be complete first.**

**Step 1 — Install Vitest (5a):**
```bash
cd desktop && npm install --save-dev vitest @vitest/ui jsdom @testing-library/react
```

**Step 2 — Configure Vitest:**
Create `desktop/vitest.config.ts`:
```ts
import { defineConfig } from 'vitest/config'
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
```

Add to `desktop/package.json` scripts:
```json
"test": "vitest run",
"test:watch": "vitest"
```

**Step 3 — Write tests for `chat/parsers.ts` (5b):**

Create `desktop/src/components/chat/__tests__/parsers.test.ts`:

Write tests for:
- `parsePlanFromActivity` — given `[PLAN]{json}` lines, assert correct `PlanStep[]` output
- `buildThinkingTrace` — all item types produce correct trace shape
- Codex JSONL parsing — `item.completed` events → correct message shapes for `agent_message`, `reasoning`, `local_shell_call`
- Status line variants — `[STATUS]`, `[STEP N]`, `[DONE N]`, `[CONTEXT]` each parsed correctly to typed output

Use realistic fixture strings that match the actual format used by `orchestrator/agent.py`. Read `orchestrator/agent.py` to get exact format strings if you're unsure.

**Step 4 — Run tests:**
```bash
cd desktop && npm run test
```
All tests must pass.

**Commit:** `test(phase-5a-5b): add Vitest, write chat/parsers.ts unit tests`

**Acceptance criteria:**
- `npm run test` from `desktop/` passes
- Tests cover the 4 categories above
- No mocking of Tauri — parsers.ts has no Tauri dependency

---

## Prompt 12 — Phase 5c+5d: Python & Rust Tests

Read `kim-pro/IMPROVEMENT_PLAN.md` — sections "Phase 5c" and "Phase 5d" are your spec.

**Step 1 — Python test setup (5c):**

Check if `pytest-asyncio` is installed: `pip show pytest-asyncio`. If not, add it to `requirements.txt` and install it.

Create `tests/conftest.py` with shared fixtures (paths, mock config, etc).

**Step 2 — Write Python tests:**

`tests/test_session_store.py`:
- Write JSONL session, read it back, assert messages are identical
- Test date-bucketed file naming

`tests/test_context_meter.py`:
- Charge tokens, assert budget decrements
- Test overflow detection edge case

`tests/test_codex_env_scoping.py`:
- Verify that `run_codex_subtask()` in `mcp_server/tools/codex_bridge.py` passes env vars via the subprocess `env=` dict parameter, NOT via `os.environ` mutation
- This is a regression test — if someone changes this, the test breaks

`tests/test_agent_plan_parsing.py`:
- Mock stdout lines → assert correct plan structure parsed

`tests/test_ollama_provider.py`:
- Mock HTTP responses from Ollama
- Verify tool call normalization produces the expected `{"type": "tool_call", "tool": ..., "args": ...}` format

**Step 3 — Rust tests (5d):**

In `desktop/src-tauri/src/subprocess.rs` (from Phase 2):
```rust
#[test]
fn test_find_python_finds_venv() { ... }
```

In `desktop/src-tauri/src/config.rs` (from Phase 4):
```rust
#[test]
fn test_config_load_from_yaml() { ... }
```

**Step 4 — Run all tests:**
```bash
pytest tests/ -v          # all must pass
cargo test                # from desktop/
```

**Step 5 — Add to CI:** Update `.github/workflows/ci.yml` to run both `pytest tests/` and `npm run test` and `cargo test` on every push.

**Commit:** `test(phase-5c-5d): add Python and Rust tests, wire CI`

**Acceptance criteria:**
- `pytest tests/` passes, all new tests green
- `cargo test` passes
- CI workflow runs all three test suites on push

---

## Prompt 13 — Phase 6: Typed IPC Migration (DECISION REQUIRED)

**⚠️ Read the DECISION REQUIRED section in `kim-pro/IMPROVEMENT_PLAN.md` before starting.**
**Only proceed if the user has said "confirm Phase 6".**

Read `kim-pro/IMPROVEMENT_PLAN.md` — "Phase 6" is your spec. The strategy is **dual-emit during cutover** so the legacy path is never broken until the typed path is verified by tests.

**Step 1 — Add `ipc_protocol` flag to `AppConfig` (in `config.rs`):**
```rust
pub ipc_protocol: String,  // "legacy" | "typed", default "legacy"
```

**Step 2 — Define typed Tauri events on the Rust side (`subprocess.rs`):**

```rust
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum KimEvent {
    Status { message: String },
    Plan { steps: Vec<serde_json::Value> },
    Step { n: usize, data: serde_json::Value },
    Done { n: usize },
    Context { used: u32, total: u32 },
    UiScreenshotFlash,
    UiShow,
}
```

In `typed` mode, for each line received from the agent subprocess:
1. Try `serde_json::from_str::<KimEvent>(line)` first
2. On success, emit the corresponding typed event (`kim:status`, `kim:plan`, `kim:step`, `kim:done`, `kim:context`, `kim:ui`)
3. Also emit `kim-agent-output` unchanged (dual-emit)
4. On parse failure, emit only `kim-agent-output` (legacy fallback)

**Step 3 — Update Python side (`orchestrator/agent.py`):**

Replace every `print(f"[TYPE] ...")` call with `print(json.dumps({"type": "...", ...}), flush=True)`:
- `print(f"[STATUS] {msg}")` → `print(json.dumps({"type": "status", "message": msg}), flush=True)`
- `print(f"[PLAN]{json_str}")` → `print(json.dumps({"type": "plan", "steps": steps}), flush=True)`
- `print(f"[STEP {n}]:{json_str}")` → `print(json.dumps({"type": "step", "n": n, "data": data}), flush=True)`
- `print(f"[DONE {n}]")` → `print(json.dumps({"type": "done", "n": n}), flush=True)`
- `print(f"[CONTEXT]{json_str}")` → `print(json.dumps({"type": "context", ...}), flush=True)`
- `print("[UI] SCREENSHOT_FLASH")` → `print(json.dumps({"type": "ui_screenshot_flash"}), flush=True)`
- `print("[UI] SHOW")` → `print(json.dumps({"type": "ui_show"}), flush=True)`

**Step 4 — TypeScript typed listeners (`useChatStream.ts`):**

Add typed event listeners alongside the existing `kim-agent-output` listener:
```ts
listen<{ message: string }>('kim:status', e => { ... })
listen<{ steps: PlanStep[] }>('kim:plan',   e => { ... })
listen<{ n: number, data: StepData }>('kim:step', e => { ... })
```

**Step 5 — Update Phase 5 tests** to assert typed listeners produce the same output as legacy string parsing for each event type.

**Step 6 — Set default to `"typed"` in `config.yaml.example`** once tests pass. Do NOT delete the legacy `kim-agent-output` path yet.

**Step 7 — After a confidence period** (user confirms the typed path is working in daily use), remove the legacy string-parsing path from TypeScript. Only delete after explicit user confirmation.

**Commit:** `feat(phase-6): dual-emit typed IPC events, update Python stdout protocol`

**Acceptance criteria:**
- `ipc_protocol: "typed"` in config works end-to-end
- `ipc_protocol: "legacy"` still works (dual-emit preserved)
- `cargo build` exits 0
- `npm run build` exits 0
- Phase 5 tests still pass
- Old-format JSONL session replay still works

---

## Prompt 14 — Phase 7: Codex Bridge Consolidation (DECISION REQUIRED)

**⚠️ Read the DECISION REQUIRED section in `kim-pro/IMPROVEMENT_PLAN.md` before starting.**
**Only proceed if the user has said "confirm Phase 7".**

Read `kim-pro/IMPROVEMENT_PLAN.md` — "Phase 7" is your spec. The goal is to merge two thin Python launcher files and improve subprocess lifecycle management. **Do NOT rewrite the aiohttp proxy or change BrowserProvider behavior.**

**Step 1 — Read both files completely before writing anything:**
- `orchestrator/run_codex_bridge.py`
- `mcp_server/tools/codex_bridge.py`

Understand exactly what each one does before merging.

**Step 2 — Create `orchestrator/codex_bridge_service.py`:**

Merge both files into one module. The new module:
- Accepts the same CLI arguments as `run_codex_bridge.py` (`--task`, `--cwd`, `--provider`, `--model`, `--config`)
- Starts `_CodexProxy` (aiohttp) on an ephemeral port — same behavior as `codex_bridge.py`
- Spawns `codex exec --json ...` — same invocation
- Adds `atexit` handler: guarantees proxy server shutdown and codex process kill
- Adds `signal.SIGTERM` handler: same cleanup
- Uses a `tempfile.TemporaryDirectory` context manager (with `__exit__`) for temp config cleanup, not ad-hoc `os.unlink`
- Surfaces codex subprocess stderr to stdout via `[STATUS] codex error: {line}` (currently silently dropped)

**Step 3 — Update the Tauri spawn call:**

In `desktop/src-tauri/src/subprocess.rs` (from Phase 2), update the spawn command from:
```
python -m orchestrator.run_codex_bridge
```
to:
```
python -m orchestrator.codex_bridge_service
```

**Step 4 — Verify old files are no longer needed:**
- Keep `mcp_server/tools/codex_bridge.py` for now but add a deprecation comment at the top
- Keep `orchestrator/run_codex_bridge.py` for now but add a deprecation comment
- Do NOT delete them until the new service has been tested end-to-end

**Step 5 — Build and test:**
```bash
cargo build          # from desktop/ — must exit 0
pytest tests/test_codex_env_scoping.py  # must still pass
```

**Step 6 — Smoke test:** Run an actual Codex task in the Code tab (or verify the spawn invocation is correct by reading the new code carefully). Once working, commit, and then delete the old files.

**Commit:** `refactor(phase-7): consolidate codex bridge into codex_bridge_service.py`

**Acceptance criteria:**
- `orchestrator/codex_bridge_service.py` exists and handles the full lifecycle
- `cargo build` exits 0
- Codex tasks run end-to-end
- `atexit`/SIGTERM handlers prevent proxy port leaks
- Old files deleted after smoke test passes
- `tests/test_codex_env_scoping.py` still passes (env dict scoping unchanged)

---

## Quick reference: Prompt → Phase mapping

| Prompt | Phase | Description | Risk |
|--------|-------|-------------|------|
| 1 | -1 | Baseline & worktree | None |
| 2 | 0a+0b | Root artifacts + archive cleanup | Low |
| 3 | 0c+0d | Dead Python + design mocks CSS | Low |
| 4 | 0e | Doc consolidation + ARCHITECTURE.md rewrite | Low |
| 5 | 1a+1b | Settings parity audit + port relay UI | Medium |
| 6 | 1c | Delete legacy SettingsPanel + Sidebar | Medium |
| 7 | 2 | lib.rs decomposition into modules | Medium |
| 8 | 3a+3b | Extract parsers.ts + useChatStream hook | Medium |
| 9 | 3c+3d+3e | Remaining hooks + StreamRenderer + wire ChatView | Medium |
| 10 | 4 | Config consolidation (AppConfig, config.rs) | Low |
| 11 | 5a+5b | Vitest setup + TypeScript parser tests | None |
| 12 | 5c+5d | Python + Rust tests, CI wiring | None |
| 13 | 6 | Typed IPC migration (confirm required) | High |
| 14 | 7 | Codex bridge consolidation (confirm required) | Med-High |
