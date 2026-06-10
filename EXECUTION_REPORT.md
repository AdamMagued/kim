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
| V-5 | `make_test_agent` factory in `conftest.py` | `47d091c` | ✅ Done |

### Track B — Production Polish

| ID | Title | Commit | Status |
|----|-------|--------|--------|
| P0-5 | CI branch triggers + `workflow_dispatch` | `5e7e345` | ✅ Done |
| P0-4 | README.md (install, architecture, providers) | `ac10b77` | ✅ Done |
| P1-5 | Session retention pruning + screenshot stripping | `ed23360` | ✅ Done |
| P1-1 | Structured rotating file logs + Reveal logs button | `56a15f7` | ✅ Done |

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

---

## Remaining Items (roadmap order)

### Track A
- [ ] V-2: `ProviderResponse` TypedDict + typed tool-result envelope + pyright in CI
- [ ] V-3: golden-transcript Rust↔Python seam test + provider contract suite
- [ ] V-1 + II-E: kill legacy text IPC; schema-first events with codegen + CI drift check
- [ ] V-4: split ChatView.tsx, decompose agent.py loop, split chat.css

### Track B
- [ ] P1-2: typed `kim:run_failed` error events + error cards + pre-flight checks
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
