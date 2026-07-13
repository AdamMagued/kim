> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# AI Restructure Baseline

**Branch:** `ai-architecture-restructure`
**Date:** 2026-05-23
**Purpose:** Capture repo state before any structural changes so every phase can be validated against a known baseline.

---

## 1. Repository Map

### Source directories (will be touched)
| Directory | Language | Purpose |
|-----------|----------|---------|
| `orchestrator/` | Python | Agent loop, LLM providers, memory, task queue |
| `mcp_server/` | Python | MCP stdio server + 31 tools |
| `desktop/src/` | TypeScript/React | Tauri 2 frontend |
| `desktop/src-tauri/src/` | Rust | Tauri backend shell |
| `tray/` | Python | System tray UI + voice engine |
| `kimctl/` | Python | CLI entry point |
| `tests/` | Python | Test suites |

### Generated / runtime directories (DO NOT touch)
| Directory | Why |
|-----------|-----|
| `desktop/node_modules/` | npm install output |
| `desktop/src-tauri/target/` | cargo build output |
| `__pycache__/` (any) | Python bytecode cache |
| `sessions/` | Runtime conversation data |
| `logs/` | Runtime log output |
| `venv/` / `.venv/` | Python virtual environment |

### Other directories (read-only reference)
| Directory | Purpose |
|-----------|---------|
| `extension/` | Chrome extension (phases 3) |
| `relay_server/` | FastAPI relay (phase 4) |
| `pythonExperimentTool/` | Experimental CLI in Rust |

---

## 2. High-Risk Files (God Files)

These are the files that will be split in phases 3-9. Line counts as of baseline.

| File | Lines | Risk | Phase |
|------|-------|------|-------|
| `desktop/src-tauri/src/lib.rs` | 10,058 | CRITICAL | 8 |
| `desktop/src/components/ChatView.tsx` | 3,721 | HIGH | 3 |
| `orchestrator/providers/browser_provider.py` | 2,249 | HIGH | 4 |
| `orchestrator/agent.py` | 1,866 | HIGH | 5 |
| `desktop/src/components/SettingsPanel.tsx` | 1,680 | MEDIUM | 9 |
| `mcp_server/tools/web.py` | 1,447 | MEDIUM | 7 |
| `mcp_server/server.py` | 948 | LOW | 6 |

---

## 3. Stdout Protocol Contract

The agent emits structured markers on stdout. Both the Tauri frontend (`ChatView.tsx`) and the Rust CLI (`provider.rs`) parse these. This is the most critical contract to preserve.

### Markers
| Marker | Format | Emitter | Consumers |
|--------|--------|---------|-----------|
| `[STATUS]` | `[STATUS] free text` | agent.py:698 | ChatView.tsx, provider.rs:1087 |
| `[TOOL]` | `[TOOL] tool_name: args` | agent.py:812,1009 | ChatView.tsx, provider.rs:1504 |
| `[SUCCESS]` | `[SUCCESS] result text` | agent.py | ChatView.tsx, provider.rs:1087 |
| `[FAILED]` | `[FAILED] error text` | agent.py | ChatView.tsx, provider.rs:1091 |
| `[PLAN]` | `[PLAN]{json}` | agent.py:804 via `_emit_plan_markers` | ChatView.tsx (parsePlanFromActivity) |
| `[STEP]` | `[STEP]{json}` | agent.py:804 | ChatView.tsx |
| `[DONE]` | `[DONE]{json}` | agent.py:1024 | ChatView.tsx |
| `[UI] SCREENSHOT_FLASH` | literal string | agent.py | lib.rs (show_screenshot_flash) |
| `[UI] SHOW` | literal string | agent.py | lib.rs (show_main_window) |

### Plan JSON shape
```json
{
  "title": "string",
  "steps": [
    {"label": "string", "status": "pending|running|done|failed"}
  ]
}
```

---

## 4. Provider Response Contract

All LLM providers must return one of:

```python
{"type": "tool_call", "tool": str, "args": dict}
{"type": "text", "content": str}
```

Providers: `claude`, `openai`, `gemini`, `deepseek`, `ollama`, `browser`

Browser provider additionally handles: clipboard paste injection, popup dismissal, image upload via file input, response scraping with site-specific selectors.

---

## 5. Session Format

JSONL files in `sessions/`. Each line is a JSON object representing a conversation turn.

---

## 6. Tauri Commands (55 total)

Full list of `#[tauri::command]` functions in `lib.rs`:

```
add_code_project, add_custom_provider_capability, backup_to_gist,
cancel_task, clear_account, delete_all_sessions, delete_sessions,
export_data, get_app_version, get_browser_current_url, get_platform_info,
hide_browser_window, hide_main_window, import_data, list_codex_projects,
list_sessions, load_account, load_run_history, load_session_messages,
navigate_browser_window_if_open, ollama_get_status, ollama_pull_model,
ollama_signin, ollama_test_model, open_browser_signin_window,
open_in_finder, provider_check_auth, provider_signin, provider_signout,
read_relay_config, read_voice_config, relay_pair_init, relay_pair_status,
remove_code_project, reset_onboarding, restore_browser_for_session,
restore_from_gist, run_update, save_account, save_attachment,
save_run_history, send_feedback, send_task, session_browser_meta_read,
session_browser_meta_write, session_browser_url_commit,
set_browser_keep_visible, set_task_active_mode, show_browser_window,
show_main_window, show_screenshot_flash, summarize_session,
verify_github_pat, write_relay_url, write_voice_config
```

---

## 7. Test Baseline

### Test commands
```bash
python -m tests.kim_test_suite          # Main suite (crashes on Windows — encoding bug)
python -m pytest tests/ -v              # Individual test files
```

### Baseline results (2026-05-23)
| Test file | Result | Notes |
|-----------|--------|-------|
| `tests/test_browser_protocol.py` | 2 PASS | |
| `tests/test_browser_provider_parse.py` | 1 SKIP | playwright not installed |
| `tests/test_context_meter.py` | 2 PASS | |
| `tests/test_interaction_policy.py` | 2 PASS | |
| `tests/test_ollama_provider.py` | 1 PASS | |
| `tests/test_web_resolver.py` | 2 PASS (expected) | Not re-run this session |
| `tests/test_web_wait_for_url.py` | 1 PASS (expected) | Not re-run this session |
| `tests/kim_test_suite.py` | CRASH | UnicodeEncodeError: cp1252 can't encode box-drawing chars |

### Known pre-existing issues
- `kim_test_suite.py` crashes on Windows due to `print("─" * 100)` — cp1252 encoding
- `yaml` module not installed (`ModuleNotFoundError: No module named 'yaml'`)
- `node_modules/` not present — `npm install` required before frontend builds
- `PairingModal.tsx` — missing `qrcode.react` type declarations (pre-existing build error)

---

## 8. Toolchain Versions

| Tool | Version |
|------|---------|
| Node.js | v24.15.0 |
| npm | 11.12.1 |
| cargo | 1.95.0 |
| rustc | 1.95.0 |
| Python | 3.x (system) |

---

## 9. Restructuring Plan (Staged)

| Phase | What | Risk | Depends on |
|-------|------|------|------------|
| 0 | This baseline document | NONE | - |
| 1 | AI safety docs (ARCHITECTURE.md, CONTRACTS.md, etc.) | NONE | Phase 0 |
| 2 | Contract lock-down tests (stdout protocol, provider shapes) | LOW | Phase 1 |
| 3 | Split ChatView.tsx | MEDIUM | Phase 2 |
| 4 | Split browser_provider.py | MEDIUM | Phase 2 |
| 5 | Split agent.py | HIGH | Phase 2 |
| 6 | Split MCP server registry | LOW | Phase 2 |
| 7 | Split web.py | MEDIUM | Phase 2 |
| 8 | Split lib.rs | CRITICAL | Phase 2 |
| 9 | Split SettingsPanel.tsx | MEDIUM | Phase 2 |
| 10 | Explicit state machines | HIGH | Phases 3-9 |
| 11 | Clean generated/runtime boundaries | LOW | Phase 10 |

### Rules
- One phase per commit (minimum)
- Tests must pass after every phase
- No behavior changes — structure only
- No touching generated/runtime directories
- Advisor consulted before each high-risk phase
