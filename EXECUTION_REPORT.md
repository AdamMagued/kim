# Kim Production Roadmap — Execution Report

Branch: `production-roadmap` off `kim-improvement`
Last updated: 2026-06-10

---

## Completed Items

### Track A — Vibecodability

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| V-7 | Scoped CLAUDE.md docs + HOW_TO.md recipes | `9b19a12` | ✅ Done |
| V-8 | justfile + KIM_FAKE=1 offline mode + `@pytest.mark.slow` | `e2f00f3` | ✅ Done |
| V-6 | Invariant tests (prompt render, tool registry, Code-tab constraint, CSS order) | `eccb3ee` | ✅ Done |
| V-5 | `make_test_agent` factory in `conftest.py` | `ed23360` | ✅ Done |
| V-2 | ProviderResponse TypedDict contract + pyright in CI | `a7fff3d` | ✅ Done |

### Track B — Production Polish

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| P0-5 | CI branch triggers + `workflow_dispatch` | `5e7e345` | ✅ Done |
| P0-4 | README.md (install, architecture, providers) | `47d091c` | ✅ Done |
| P1-5 | Session retention pruning + screenshot stripping | `ac10b77` | ✅ Done |
| P1-1 | Structured rotating file logs + Reveal logs button | `56a15f7` | ✅ Done |
| P1-2 | Typed run_failed error events + error cards + rate-limited UI | `2f54625` | ✅ Done |

### Infra fixes

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| fix | Import fix: `from tests.conftest` → `from conftest` (ultralytics namespace collision) | `642a488` | ✅ Done |

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

### fix — ultralytics namespace collision
- Commit `642a488`
- Root cause: `test_prompt_render.py` and `test_make_test_agent.py` used `from tests.conftest import`, which resolved to the globally-installed ultralytics `tests` package instead of the local `tests/` directory. These tests were *introduced* in V-5/V-6 on this branch, so the 6 failures were NOT pre-existing.
- Fix: changed both files to `from conftest import` (pytest adds `tests/` to `sys.path`)
- Verification: `python3 -m pytest tests/ -q` → 862 passed, 0 failed, 13 skipped

---

## Remaining Items (roadmap order)

### Track A
- [x] V-2: `ProviderResponse` TypedDict + pyright in CI — ✅ Done (`a7fff3d`)
- [ ] V-1 (partial): schema-first codegen — `events.schema.json` → `events.gen.ts` + `npm run gen:events` + CI drift check
- [ ] V-1 (legacy-kill) BLOCKED: kill dual-emit from `subprocess.rs` / legacy `kim-agent-output` text parsing in `parsers.ts`. **Blocked on**: golden-transcript seam test (V-3) — removing the dual-emit without a test that asserts "these Python stdout lines → these typed events → these activity items" risks silent UI rot. The Codex CLI subprocess also uses `kim-agent-output` and cannot be schema-firsted. Do not remove dual-emit until V-3 seam tests exist.
- [ ] V-3: golden-transcript Rust↔Python seam test + provider contract suite
- [ ] V-4: split ChatView.tsx, decompose agent.py loop, split chat.css

### Track B
- [ ] P1-3: approval-gate UI round-trip + permission mode toggle
- [ ] P0-1: PyInstaller spec + sidecar resolution in `find_python_interpreter()`
- [ ] Part II quick wins: H (kill voice), J (feature-flag relay pane), D (cost meter), F (OS notifications)

### Human-blocked
- License choice (TODO in README)
- Apple signing certs / notarization
- Windows signing
- Updater keypair
- Sentry DSN
- GitHub Actions secrets (APPLE_CERTIFICATE, TAURI_SIGNING_PRIVATE_KEY, etc.)
