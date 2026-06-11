# Kim Production Roadmap — Execution Report

Branch: `production-roadmap` off `kim-improvement`
Last updated: 2026-06-11

---

## Completed Items

### Track A — Vibecodability

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| V-7 | Scoped CLAUDE.md docs + HOW_TO.md recipes | `9b19a12` | ✅ Done |
| V-8 | justfile + KIM_FAKE=1 offline mode + `@pytest.mark.slow` | `e2f00f3` | ✅ Done |
| V-6 | Invariant tests (prompt render, tool registry, Code-tab constraint, CSS order) | `eccb3ee` | ✅ Done |
| V-5 | `make_test_agent` factory in `conftest.py` | `ed23360` | ✅ Done |
| V-1 | Kill dual-emit for Kim-agent path; remove dead text parsers (`[STATS]`/`[CONTEXT]`/`[USAGE]`/`[UI]`); clean useChatStream dead cases | TBD | ✅ Done |
| V-2 | ProviderResponse TypedDict contract + pyright in CI | `a7fff3d` | ✅ Done |

### Track B — Production Polish

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| P0-5 | CI branch triggers + `workflow_dispatch` | `5e7e345` | ✅ Done |
| P0-4 | README.md (install, architecture, providers) | `47d091c` | ✅ Done |
| P1-5 | Session retention pruning + screenshot stripping | `ac10b77` | ✅ Done |
| P1-1 | Structured rotating file logs + Reveal logs button | `56a15f7` | ✅ Done |
| P1-2 | Typed run_failed error events + error cards + rate-limited UI | `2f54625` | ✅ Done |
| P1-3 | Approval-gate UI round-trip + per-session permission mode toggle | `f8a3e37` | ✅ Done |
| P0-1 | PyInstaller spec for sidecar + sidecar-first `find_python_interpreter()` | `a37cba0` | ✅ Done |
| II-H | Kill voice cleanly (tray.voice, VoiceSettings, PaneVoice, config keys) | `5c5e4f9` | ✅ Done |
| II-J | Feature-flag relay pane off (code preserved) | `734ee75` | ✅ Done |
| II-D | Cost meter — per-model price table + cost chip on WorkedForPill | `d5c71e5` | ✅ Done |
| II-F | OS notifications on task completion/failure (Tauri notification plugin) | pending | ✅ Done |

### Infra fixes

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| fix | Import fix: `from tests.conftest` → `from conftest` (ultralytics namespace collision) | `642a488` | ✅ Done |
| fix | CI workflow YAML invalid since V-2 — pyright step used plain scalar for multi-line script | `75897ee` | ✅ Done |
| fix | First real CI run fallout — pyright `# type: ignore` suppressions, clippy `allow(too_many_arguments)`, CLI session-id millisecond-collision bug | `4bbb1f1` | ✅ Done |
| fix | Flake8 backlog from first real CI run — unused imports, long lines, F822 lazy-export suppression | `0d3b771` | ✅ Done |

---

## Baseline test counts (as of `0d3b771`, branch head entering new session 2026-06-11)

| Suite | Count | Notes |
|-------|-------|-------|
| `python -m pytest tests/` | 884 passed, **1 failed**, 8 skipped | The 1 failure (`test_run_command_default_uses_requested_cwd`) is a macOS case-insensitive-FS artifact: `PROJECT_ROOT` resolves to `Desktop/kimFork/...` but `pwd` returns canonical `desktop/kimFORK/...`. CI (Linux) is unaffected — remote run `0d3b771` is green. Pre-existing from parent branch `d58f19e`. |
| `cd desktop && npm run test` | 41 passed | |
| `cd desktop/src-tauri && cargo test` | 54 passed | |
| `cd cli && cargo test` | 90 passed | |

---

## Item Details

### V-7 — Scoped CLAUDE.md docs + HOW_TO.md recipes
- Root `CLAUDE.md` shrunk from 694 → 41 lines; acts as a router
- `HOW_TO.md` at root: golden-path recipes for adding MCP tools, providers, settings panes, agent events
- `orchestrator/CLAUDE.md`, `mcp_server/CLAUDE.md`, `desktop/src/CLAUDE.md`, `desktop/src-tauri/CLAUDE.md` each created (~30–43 lines)

### V-8 — justfile + KIM_FAKE=1 + slow mark
- `justfile` with `just check` (parallel tsc + cargo + pytest, target <30s), `just test`, `just test-web`, `just fake`, `just dev`
- `orchestrator/providers/fake.py`: `FakeProvider` returns scripted responses; no network
- `orchestrator/providers/base.py`: `KIM_FAKE=1` env var short-circuits `create_provider()` to `FakeProvider`
- `pytest.ini`: registered `@pytest.mark.slow` marker
- `tests/test_fake_provider.py`: 5 tests

### V-6 — Invariant tests
- `tests/test_invariants.py`: tool-registry parity, Code-tab no-gpt-5.5, CSS import order (15 files)
- `tests/test_prompt_render.py`: 6 f-string brace-escape tests; `try: import mcp` pattern avoids sys.modules pollution

### V-5 — `make_test_agent` factory
- `tests/conftest.py`: `make_test_agent(**overrides)` and `test_agent` fixture
- `tests/test_make_test_agent.py`: 8 tests verifying factory

### V-2 — ProviderResponse TypedDict + pyright in CI
- Commit `a7fff3d`; added `ProviderResponse` TypedDict in `orchestrator/providers/base.py`
- pyright in CI for orchestrator + mcp_server at `basic` strictness

### P0-5 — CI branch triggers
- `.github/workflows/ci.yml` on-push branches extended to include `kim-improvement`, `production-roadmap`
- Added `workflow_dispatch`

### P0-4 — README.md
- Full README: install, architecture summary, provider setup, privacy statement, `TODO(human):` license placeholder

### P1-5 — Session retention pruning
- `orchestrator/session_store.py`: `prune_old_sessions(max_age_days, screenshot_strip_age_days, base_dir)` and `delete_all_sessions(base_dir)`
- `desktop/src-tauri/src/session_commands.rs`: `prune_sessions` Tauri command (delegates to Python)
- `desktop/src-tauri/src/lib.rs`: registered `prune_sessions`
- `tests/test_session_retention.py`: 9 tests

### P1-1 — Structured rotating file logs + Reveal logs button
- `mcp_server/logger.py`: `apply_log_retention(log_dir, keep_days)` — date-based retention, deletes `kim_YYYY-MM-DD.jsonl` where `file_date < today - keep_days`
- `orchestrator/cli.py`: calls `setup_structured_logging()` + `apply_log_retention()` at startup (wrapped in try/except)
- `desktop/src-tauri/src/session_commands.rs`: `reveal_logs()` command — creates `logs/` if needed, opens it via `open`/`explorer`/`xdg-open`
- `desktop/src-tauri/src/lib.rs`: registered `reveal_logs`
- `desktop/src/components/kim-ui/settings-panes/PaneInfo.tsx` → `PaneFeedback`: "Reveal logs" button with folder icon and description
- `tests/test_log_retention.py`: 7 tests
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### P1-2 — Typed run_failed error events + error cards
- Commit `2f54625`
- `orchestrator/agent_states.py`: `run_failure_event()` maps `AgentTermination` → typed failure dict
- `orchestrator/agent.py`: emits `kim:run_failed` JSON; stores `_last_provider_error_code`; emits `rate_limited` JSON before sleep
- `desktop/src-tauri/src/subprocess.rs`: `RunFailed` + `RateLimited` variants in `KimEvent` enum
- `desktop/src/hooks/useChatStream.ts`: `runFailure` + `rateLimitedState` state + event listeners
- `desktop/src/hooks/useTaskRunner.ts`: clears stale state on run start
- `desktop/src/components/ChatView.tsx`: passes new props to `StreamRenderer`
- `desktop/src/components/chat/StreamRenderer.tsx`: run-failed card + rate-limited banner
- `tests/test_run_failure_event.py`: 11 tests
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### P1-3 — Approval-gate UI round-trip + per-session permission mode toggle
- `orchestrator/ui_bridge.py`: Added `StdinApprovalBridge` — reads `{"type":"hitl_approve","approved":true|false}` from stdin, 120 s auto-deny timeout
- `orchestrator/agent.py`: Auto-wires `StdinApprovalBridge` in `__init__` when `KIM_TAURI_MODE=1` + `KIM_HITL_RISK_THRESHOLD` is set
- `desktop/src-tauri/src/subprocess.rs`: Added `hitl_stdin()` global, `hitl_respond_approval` command, piped stdin for Kim orchestrator, pass `KIM_TAURI_MODE=1` + `KIM_HITL_RISK_THRESHOLD` based on `permission_mode` param, cleanup on task end
- `desktop/src-tauri/src/lib.rs`: Registered `hitl_respond_approval` command, exported from subprocess
- `desktop/src/types/index.ts`: Added `PermissionMode` type + `permission_mode` field to `Settings` (default: `'full_auto'`)
- `desktop/src/hooks/useTaskRunner.ts`: Passes `permissionMode` to `send_task`
- `desktop/src/hooks/useChatStream.ts`: Exports `hitlRespond(approved)` callback via `invoke('hitl_respond_approval', ...)`
- `desktop/src/components/chat/StreamRenderer.tsx`: Added Approve/Deny buttons in HITL status card when `approved === null`; added `renderPermissionToggle()` (3-button toggle: Full auto / Ask risky / Ask always) rendered above the composer
- `desktop/src/components/ChatView.tsx`: Local `permissionMode` state for per-session override; passes `onHitlRespond`, `permissionMode`, `onPermissionModeChange` to StreamRenderer; merges override into settings for useTaskRunner
- `tests/test_stdin_approval_bridge.py`: 9 tests (approve/deny/cancel/timeout/malformed, auto-wire variants)
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### P0-1 — PyInstaller spec + sidecar-first interpreter resolution
- `kim-orchestrator.spec`: PyInstaller spec at repo root; bundles `orchestrator/` + `mcp_server/` as single `console=True` executable; includes all 6 provider hidden imports; excludes tkinter/numpy/pytest; `TODO(human):` markers for codesign_identity + entitlements
- `desktop/src-tauri/src/subprocess.rs`: New `find_bundled_orchestrator()` — looks for `kim-orchestrator` or `kim-orchestrator-<target-triple>` adjacent to current exe; `is_bundled_orchestrator()` predicate; `dirs_home()` helper; `find_python_interpreter()` now checks bundled sidecar first (sidecar → `~/.kim_root` → `~/.kim` → system); `send_task` skips `-m orchestrator.agent` when sidecar is detected
- `externalBin` in `tauri.conf.json` NOT added — file must exist at build time and the binary is machine-built; documented as `TODO(human)` in the spec file
- 4 new Rust tests: `sidecar_name_no_triple`, `sidecar_name_with_triple`, `is_bundled_orch_true/false`
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### II-H — Kill voice cleanly
- `orchestrator/agent.py`: Removed `TYPE_CHECKING` VoiceEngine import, `voice_engine` param from `KimAgent.__init__` and `mcp_agent_context`, `_voice_speak()` and all call sites, `voice.human_quirks` blocks from both system-prompt builders
- `desktop/src/types/index.ts`: Removed `VoiceEngine` type, `VoiceSettings` interface, `VOICES_BY_ENGINE` catalog, `voice` field from `Settings` and `DEFAULT_SETTINGS`
- `desktop/src/components/kim-ui/RevampSettings.tsx` + `RevampSidebar.tsx`: Removed 'voice' from PaneId, NAV, NavIcon, PANE_META, render, SettingsPane type
- `desktop/src/components/kim-ui/settings-panes/PaneSystem.tsx`: Deleted `PaneVoice` component and `VOICE_ENGINES` constant
- `tests/test_make_test_agent.py`: Added `test_agent_no_voice_attributes` invariant
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### II-J — Feature-flag relay pane off
- `desktop/src/components/kim-ui/RevampSettings.tsx`: Added `RELAY_ENABLED = false` constant; `NAV` computed by filtering relay from `NAV_ALL`; render of `PaneRelay` gated on flag
- All relay code preserved: `PaneRelay` in `PaneInfo.tsx`, `relay.rs`, `relay.css`, relay capability still in `default.json`
- `tests/test_invariants.py`: `TestRelayFeatureFlag` — verifies flag is false, code preserved, PaneId 'relay' exists

### V-1 — Kill legacy dual-emit + dead text parsers
- **Rust (`subprocess.rs`)**: `ipc_typed=true` branch no longer dual-emits typed `KimEvent` lines on `kim-agent-output`. Only non-KimEvent lines (legacy text: `[TOOL]`, `[SUCCESS]`, `[STATUS]`) are forwarded on the legacy channel. `ipc_typed=false` (Codex CLI path) is unchanged — all lines still forwarded on legacy channel.
- **`parsers.ts`**: Removed dead parsing branches that were never reachable for Kim-agent stdout:
  - `[STATS] input_tokens=...` text regex (goes to Python logger/stderr, not stdout)
  - `[CONTEXT] cumulative_input=...` text regex (goes to Python logger/stderr)
  - `[USAGE] {...}` prefix block (goes to Python logger/stderr)
  - Explicit `run_done`/`provider_error`/`hitl_*` silencing (now handled at Rust level — never reaches `kim-agent-output`)
  - `[UI] SCREENSHOT_FLASH` / `[UI] SHOW` text checks (agent emits JSON format; handled by `kim:ui` typed event)
- **`ParsedAgentLine` type**: Removed `stats`, `context`, `usage`, `screenshot_flash`, `show_window` variants. Removed `ContextState`, `TokenStats` interfaces. Removed `ProviderUsageState` import.
- **`useChatStream.ts`**: Removed dead `case 'stats':`, `case 'context':`, `case 'usage':`, `case 'screenshot_flash':`, `case 'show_window':` from `appendRaw`. Removed `providerUsage`/`setProviderUsage` state (was only set from dead `[USAGE]` path; no typed `kim:usage` event exists). UI state for context and stats is still set from the typed `kim:context` / `kim:stats` Tauri event listeners.
- **`parsers.test.ts`**: Removed 4 dead tests for `[STATS]`, `[CONTEXT]` (×2), `[USAGE]` text formats.
- **Test counts after V-1**: pytest 926/1/8 (unchanged); Vitest 62 passed (−4 dead text-format tests); Rust unchanged.

### V-3 — Golden-transcript seam test + provider contract suite
- Commit `e0aca6d`
- `tests/test_v3_golden_transcript.py`: runs `KimAgent.run()` end-to-end with `FakeProvider` + mocked async MCP session; captures stdout; verifies every emitted line has the expected shape (`status`, `context`, `stats`, `ui_screenshot_flash`, `ui_show`, `legacy_tool`); writes `tests/fixtures/golden_transcript.json` snapshot; additional tests for `run_failed` and `provider_error` emission paths. 10 tests.
- `tests/fixtures/golden_transcript.json`: canonical 7-line transcript (status → context → context → [TOOL] take_screenshot → ui_screenshot_flash → ui_show → context) from `FakeProvider(take_screenshot + TASK_COMPLETE)` run.
- `tests/test_provider_contract.py`: 32 parametrized tests across claude, openai, deepseek, gemini, ollama message-formatting layers — text, tool-call, tool-result, and image scenarios. No network required.
- `desktop/src/components/chat/__tests__/agentProtocol.test.ts`: 25 Vitest tests feed all 7 golden lines to `parseAgentLine()`; assert typed JSON events are silenced (`type:'none'`), legacy `[TOOL]` lines produce `activity_item`, `[SUCCESS]`/`[FAILED]` behave correctly. Covers full golden fixture round-trip.
- **Test counts after V-3**: pytest 926 passed / 1 failed (known macOS casing) / 8 skipped; Vitest 66 passed; Rust desktop 54; Rust CLI 90.

### II-D — Cost meter
- `desktop/src/components/chat/utils.ts`: `PRICE_PER_1M` table for claude/openai/gemini/deepseek/ollama/browser; `estimateCostUsd()` and `formatCostUsd()` utilities
- `desktop/src/components/chat/StreamRenderer.tsx`: Added `tokenStats` prop; cost chip shown below `WorkedForPill` for last run — "local · $0" for ollama/browser, "~$X.XXXX" for cloud
- `desktop/src/components/ChatView.tsx`: Passes `stream.tokenStats` to `StreamRenderer`
- `desktop/src/components/chat/__tests__/utils.test.ts`: 10 new tests for cost utilities

### II-F — OS notifications on task completion/failure
- `desktop/src-tauri/Cargo.toml`: Added `tauri-plugin-notification = "2"`
- `desktop/src-tauri/src/lib.rs`: Registered `tauri_plugin_notification::init()`
- `desktop/src-tauri/capabilities/default.json`: Added `"notification:default"` permission
- `desktop/package.json`: Added `@tauri-apps/plugin-notification`
- `desktop/src/hooks/useOsNotifications.ts`: New hook; listens to `kim:run-done` + `kim:run-failed`; sends OS notification via plugin (lazy permission request, best-effort)
- `desktop/src/components/ChatView.tsx`: Calls `useOsNotifications()` unconditionally
- `tests/test_invariants.py`: `TestOsNotificationsHook` — 5 structural invariants
- **Note:** `tauri dev` needs a restart to pick up Rust changes

### fix — ultralytics namespace collision
- Commit `642a488`
- Root cause: `test_prompt_render.py` and `test_make_test_agent.py` used `from tests.conftest import`, which resolved to the globally-installed ultralytics `tests` package instead of the local `tests/` directory. These tests were *introduced* in V-5/V-6 on this branch, so the 6 failures were NOT pre-existing.
- Fix: changed both files to `from conftest import` (pytest adds `tests/` to `sys.path`)
- Verification: `python3 -m pytest tests/ -q` → 862 passed, 0 failed, 13 skipped

---

## Remaining Items (roadmap order)

### Track A
- [x] V-2: `ProviderResponse` TypedDict + pyright in CI — ✅ Done (`a7fff3d`)
- [x] V-3: Golden-transcript seam test + provider contract suite — ✅ Done (`e0aca6d`)
- [x] V-1: Kill dual-emit (Kim-agent path) + dead text parsers — ✅ Done (TBD commit)
- [ ] V-1 (partial): schema-first codegen — `events.schema.json` → `events.gen.ts` + `npm run gen:events` + CI drift check (intentionally deferred — no value-add until event schema stabilizes)
- [x] V-3: golden-transcript Rust↔Python seam test + provider contract suite — ✅ Done
- [ ] V-4: split ChatView.tsx, decompose agent.py loop, split chat.css

### Track B
- [x] P1-3: approval-gate UI round-trip + permission mode toggle — ✅ Done
- [x] P0-1: PyInstaller spec + sidecar resolution in `find_python_interpreter()` — ✅ Done
- [x] II-H: Kill voice — ✅ Done (`5c5e4f9`)
- [x] II-J: Feature-flag relay pane off — ✅ Done (`734ee75`)
- [x] II-D: Cost meter — ✅ Done (`d5c71e5`)
- [x] II-F: OS notifications — ✅ Done

### Human-blocked
- License choice (TODO in README)
- Apple signing certs / notarization
- Windows signing
- Updater keypair
- Sentry DSN
- GitHub Actions secrets (APPLE_CERTIFICATE, TAURI_SIGNING_PRIVATE_KEY, etc.)
