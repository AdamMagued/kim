> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# AI Restructure Final Report

**Branch:** `ai-architecture-restructure`
**Base:** `master`
**Completed:** 2026-05-24
**Total commits ahead of master:** 24

---

## Commit History (`git log --oneline master..HEAD`)

```
d8bdb22 docs(phase8): mark complete, update report with final stats (7421 lines, 9 modules extracted)
f603dce Phase 8: extract session_commands.rs + run_history.rs (-526 lines from lib.rs)
f0e2c21 Phase 8: extract codex_projects.rs + feedback.rs (-488 lines from lib.rs)
f6212e0 Phase 8: extract data_io.rs (−470 lines from lib.rs)
2a30628 Phase 8: extract relay.rs, account.rs, ollama.rs (−1028 lines from lib.rs)
dd5852f Phase 8 pilot: extract voice_config.rs from lib.rs (−135 lines)
6163afa docs(final): add AI_RESTRUCTURE_FINAL_REPORT.md
34f4795 chore(phase11): expand .gitignore with runtime/build boundaries
0536913 refactor(phase3): extract pure utilities → chat/utils.ts
fe4c1ad refactor(phase3): extract ChatView interfaces → chat/types.ts
209e785 refactor(phase9): split SettingsPanel.tsx — extract constants and icons
d869afb refactor(phase10): explicit AgentTermination state machine in run loop
e6f2cc8 refactor(phase7): extract pure element-scoring helpers → web_element_scoring.py
8a79ee0 refactor(phase7): extract _OBSERVE_JS blob → web_observe_js.py
16a3f6e Phase 5d: extract MultiMCPClient + mcp_session_context into orchestrator/mcp_client.py
b171ba8 Phase 5c: extract tool name normalization into orchestrator/tool_utils.py
d5eb45c Phase 5b: extract UIBridge + UIBridgeLogHandler into orchestrator/ui_bridge.py
8bb3ff0 Phase 5a: extract CLI block from agent.py into orchestrator/cli.py
7dba6dc refactor: extract MCP tool registry from server.py (Phase 6)
77896dc fix: remove dead code in response_parser, add 35 browser split tests
9bfe2ec refactor: split browser_provider.py into browser/ package (Phase 4)
ea6a7d5 test: add contract lock-down tests for stdout protocol, provider shapes, and message format (Phase 2)
61acfdf docs: add AI safety documentation layer (Phase 1)
4d3ab10 docs: add AI restructure baseline
```

---

## Summary of Modular Splits

| Phase | What split | Result |
|-------|-----------|--------|
| 3 | `ChatView.tsx` (3,721 lines) | + `chat/types.ts` (7 interfaces), `chat/utils.ts` (~840 lines) |
| 4 | `browser_provider.py` (~1,500 lines) | + `browser/` package: `provider.py`, `bridge_client.py`, `prompt_builder.py`, `response_parser.py`, `site_configs.py`, `__init__.py` |
| 5 | `agent.py` (~2,800 lines) | + `cli.py`, `ui_bridge.py`, `tool_utils.py`, `mcp_client.py`, `agent_states.py` |
| 6 | `mcp_server/server.py` (~960 lines) | + `tool_registry.py` (~922 lines) |
| 7 | `mcp_server/tools/web.py` (~1,450 lines) | + `web_observe_js.py` (~166 lines), `web_element_scoring.py` (~264 lines) |
| 8 | `lib.rs` (10,058 lines) | + `voice_config.rs`, `relay.rs`, `account.rs`, `ollama.rs`, `data_io.rs`, `feedback.rs`, `codex_projects.rs`, `session_commands.rs`, `run_history.rs` |
| 9 | `SettingsPanel.tsx` (~1,680 lines) | + `settings/constants.ts`, `settings/icons.tsx` |
| 10 | `agent.py` return dicts | + `agent_states.py`: `AgentTermination` enum + `make_run_result()` |

---

## File Movement Map

### Python / Orchestrator

| Code moved | From | To |
|-----------|------|----|
| `MultiMCPClient`, `mcp_session_context` | `orchestrator/agent.py` | `orchestrator/mcp_client.py` |
| `UIBridge`, `UIBridgeLogHandler` | `orchestrator/agent.py` | `orchestrator/ui_bridge.py` |
| Tool name normalization helpers | `orchestrator/agent.py` | `orchestrator/tool_utils.py` |
| CLI argument parsing + `__main__` block | `orchestrator/agent.py` | `orchestrator/cli.py` |
| `AgentTermination` enum + `make_run_result()` | `orchestrator/agent.py` (inline dicts) | `orchestrator/agent_states.py` |
| `BrowserProvider` class body | `orchestrator/providers/browser_provider.py` | `orchestrator/providers/browser/provider.py` |
| CDP bridge helpers | `orchestrator/providers/browser_provider.py` | `orchestrator/providers/browser/bridge_client.py` |
| System prompt construction | `orchestrator/providers/browser_provider.py` | `orchestrator/providers/browser/prompt_builder.py` |
| Response parsing | `orchestrator/providers/browser_provider.py` | `orchestrator/providers/browser/response_parser.py` |
| Site selector configs | `orchestrator/providers/browser_provider.py` | `orchestrator/providers/browser/site_configs.py` |
| All 31 MCP tool definitions | `mcp_server/server.py` | `mcp_server/tool_registry.py` |
| `_OBSERVE_JS` blob (~5 KB) | `mcp_server/tools/web.py` | `mcp_server/tools/web_observe_js.py` |
| Element scoring helpers | `mcp_server/tools/web.py` | `mcp_server/tools/web_element_scoring.py` |

### TypeScript / React

| Code moved | From | To |
|-----------|------|----|
| 7 TypeScript interfaces | `desktop/src/components/ChatView.tsx` | `desktop/src/components/chat/types.ts` |
| `groupCodexMessages`, `collapseMessages`, `friendlyError`, formatting utils | `desktop/src/components/ChatView.tsx` | `desktop/src/components/chat/utils.ts` |
| Provider name constants, model lists | `desktop/src/components/SettingsPanel.tsx` | `desktop/src/components/settings/constants.ts` |
| 21 SVG icon components | `desktop/src/components/SettingsPanel.tsx` | `desktop/src/components/settings/icons.tsx` |

### Rust / Tauri (lib.rs)

| Code moved | From | To |
|-----------|------|----|
| Voice config loading + `VoiceConfig` struct | `lib.rs` | `src/voice_config.rs` |
| Relay types + `connect_relay` | `lib.rs` | `src/relay.rs` |
| Account types + account commands | `lib.rs` | `src/account.rs` |
| Ollama tags command | `lib.rs` | `src/ollama.rs` |
| File I/O helpers (`chrono_now`, `unix_secs_to_utc_iso`, `read_file_content`, etc.) | `lib.rs` | `src/data_io.rs` |
| Feedback command + `FeedbackPayload` | `lib.rs` | `src/feedback.rs` |
| Codex project types + commands | `lib.rs` | `src/codex_projects.rs` |
| Session listing, deletion, summarization, message loading, `get_app_version` | `lib.rs` | `src/session_commands.rs` |
| Run history persistence, platform info, update command | `lib.rs` | `src/run_history.rs` |

### Documentation (new files, no source moved)

| File | Purpose |
|------|---------|
| `AI_EDIT_GUIDE.md` | Safe editing rules for AI sessions |
| `AI_RESTRUCTURE_BASELINE.md` | Pre-restructure metrics snapshot |
| `ARCHITECTURE.md` | Component map and data flows |
| `CONTRACTS.md` | Public APIs that must not change |
| `SOURCE_OF_TRUTH.md` | Canonical files per subsystem |
| `STATE_FLOWS.md` | Agent run loop state diagram |
| `TEST_MATRIX.md` | Contract test registry |
| `tests/test_contracts.py` | Contract lock-down tests |

---

## Line Counts Before / After

| File | Before (master) | After (this branch) | Delta |
|------|----------------|---------------------|-------|
| `desktop/src-tauri/src/lib.rs` | 10,058 | 7,421 | −2,637 |
| `desktop/src/components/ChatView.tsx` | 3,721 | 2,751 | −970 |
| `desktop/src/components/SettingsPanel.tsx` | ~1,680 | 1,491 | ~−189 |
| `orchestrator/agent.py` | ~2,800 | 1,540 | ~−1,260 |
| `orchestrator/providers/browser_provider.py` | ~1,500 | 25 | ~−1,475 (now thin shim) |
| `mcp_server/server.py` | ~960 | 134 | ~−826 (tool defs moved to registry) |
| `mcp_server/tools/web.py` | ~1,450 | 1,057 | ~−393 |

**Rust modules created (lib.rs extraction):**
| New file | Lines |
|----------|-------|
| `src/voice_config.rs` | 143 |
| `src/relay.rs` | ~370 |
| `src/account.rs` | ~300 |
| `src/ollama.rs` | ~75 |
| `src/data_io.rs` | ~470 |
| `src/feedback.rs` | ~188 |
| `src/codex_projects.rs` | ~320 |
| `src/session_commands.rs` | 325 |
| `src/run_history.rs` | 188 |

---

## Validation Commands and Exact Outcomes

### 1. Rust / Tauri — `cargo test`

```bash
cd desktop/src-tauri
cargo test
```

**Result:**
```
running 1 test
test tests::test_build_bridge_complete_script_no_poisoning ... ok

test result: ok. 1 passed; 0 failed
```

- 0 errors
- Rust test target builds and passes

### 2. Python Contracts — `pytest tests/test_contracts.py`

```
pytest tests/test_contracts.py -v
```

**Result:** `28 passed in 0.36s`

All 28 contract tests pass. These lock down:
- Stdout protocol format (JSON envelope shape)
- Provider `complete()` return shapes (`{"type": "tool_call", ...}` / `{"type": "text", ...}`)
- Canonical message format (text, multimodal, tool result)
- Task completion signal patterns (`TASK_COMPLETE:`, `NEED_HELP:`)
- Plan parsing (block detection, step markers, done markers)
- MCP error envelope prefixes

### 3. Python Full Test Suite

```
PYTHONPATH=. python -m pytest -q tests test_json_repair.py
```

**Result:** `129 passed, 1 warning in 0.72s`

Follow-up review fixed the stale `test_browser_protocol.py` Claw import and
made the provider-name contract tolerant of missing local credentials. With the
repo venv dependencies present, the Python suite now runs without the previous
collection errors.

### 4. TypeScript / Vite Build

```
cd desktop
npm ci
npm run build
```

**Result:** `tsc && vite build` completed successfully.

---

## What Was NOT Extracted (Documented Blockers)

`lib.rs` still contains ~4,004 lines of JS bridge code that could not be safely extracted. All sections depend on OnceLock statics shared across the bridge:

| Static | Used by |
|--------|---------|
| `WEBVIEW_BRIDGE_CFG` | provider auth, send_task, cancel_task |
| `WEBVIEW_BRIDGE_RESULTS` | provider auth, send_task |
| `WEBVIEW_BRIDGE_REQ_COUNTER` | provider auth |
| `WEBVIEW_KEEP_VISIBLE` | browser window commands |
| `BRIDGE_TASK_PID` | send_task, cancel_task |
| `BRIDGE_TASK_SESSION` | cancel_task |

Sections that remain in lib.rs as a result:
- Codex file-bridge watcher (L5345–5611)
- Browser window commands (L6144–6529)
- Provider auth commands (L6530–6883)
- Chrome launch + `send_task` (L6884–7634)
- `cancel_task` (L7636–7791)

These require extracting the OnceLock statics first, then all dependent functions simultaneously — a larger coordinated refactor outside the scope of this branch.

---

## Files Needing Future Cleanup

| File | Issue |
|------|-------|
| `desktop/src-tauri/src/lib.rs` | Still 7,421 lines; JS bridge sections (L1340–7791) are a future extraction target once OnceLock statics are addressed |
| `orchestrator/providers/browser_provider.py` | Now a 25-line shim re-exporting from `browser/`; could be removed entirely once all callers import from `browser/` directly |
| Test environment | Requires repo venv/deps (`pytest`, `pyyaml`, `python-dotenv`, `httpx`, `google-generativeai`, `json-repair`) for full Python test discovery |
| `lib.rs:409` | Pre-existing `unused variable: e` warning — rename to `_e` |

---

## Human QA Checklist

The following items must be verified manually before merging to master. Automated tests do not cover these.

### App Launch
- [ ] App builds: `cargo tauri build` completes without errors
- [ ] App launches on Windows without crash
- [ ] Tray icon appears in system tray
- [ ] Main window opens on tray click

### Session Management
- [ ] Session list loads (calls `list_sessions` — now in `session_commands.rs`)
- [ ] Sessions display correct titles and dates
- [ ] Session deletion works (calls `delete_sessions`)
- [ ] Deleted session no longer appears in list after refresh

### Chat / Message Loading
- [ ] Opening a session loads messages (calls `load_session_messages`)
- [ ] Messages render in correct order (user / assistant alternation)
- [ ] Code blocks render correctly
- [ ] Long sessions (50+ messages) load without hanging

### Session Summarization
- [ ] Triggering summarize on a session creates a `.summary.txt` file
- [ ] Summary appears in session metadata on next load

### Run History
- [ ] Starting an agent run saves a `.runs.json` file (calls `save_run_history`)
- [ ] Run history loads correctly on session reopen (calls `load_run_history`)

### Browser Tool
- [ ] Browser window opens when a browser provider tool is invoked
- [ ] Browser window hides when task completes
- [ ] Browser task does not crash when cancelled mid-run

### Provider Switching
- [ ] Switching from Claude → OpenAI in Settings saves correctly
- [ ] Switching back to Claude restores previous config
- [ ] Ollama provider appears in dropdown (calls `ollama_tags` — now in `ollama.rs`)

### OAuth / Account
- [ ] Account token save/load works (account commands now in `account.rs`)
- [ ] Relay connection status updates in UI (relay commands now in `relay.rs`)

### Voice
- [ ] Voice config loads from disk (now in `voice_config.rs`)
- [ ] Voice enabled/disabled toggle persists across restart
- [ ] No crash if voice backend is unavailable

### Cancellation
- [ ] Sending a task then cancelling stops the agent
- [ ] UI returns to idle state after cancellation
- [ ] No zombie processes after cancellation

### Update Flow
- [ ] `run_update` command (now in `run_history.rs`) triggers git pull
- [ ] Progress events appear in UI during update
- [ ] App restarts after successful update

### Codex Integration
- [ ] Codex projects list loads (calls `list_codex_projects` — now in `codex_projects.rs`)
- [ ] Adding a codex project persists to disk
- [ ] Removing a codex project removes it from list
- [ ] `mirror_latest_claw_session_to_codex` runs without panic

### Platform Info
- [ ] `get_platform_info` returns correct string for this OS (now in `run_history.rs`)
- [ ] `get_app_version` returns correct semver (now in `session_commands.rs`)

---

## Push Status

**Nothing has been pushed.** Remote is configured (`origin = https://github.com/AdamMagued/kim.git`) but no `git push` was executed during this restructure. All 24 commits exist only on the local branch `ai-architecture-restructure`.

To push when ready:
```bash
git push -u origin ai-architecture-restructure
```

---

## Inspection Commands

```bash
# Commits on this branch not yet on master:
git log --oneline master..HEAD

# Summary of all file changes vs master:
git diff --stat master...HEAD
```
