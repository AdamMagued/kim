# Kim browser reliability patch notes

Implemented from the browser LLM reliability/session-continuity plan:

- Unified Claw-over-browser transport markers so `claw_bridge.py` no longer hard-codes plain `[END_OF_RESPONSE]`; `BrowserProvider` owns the dynamic completion hash instruction.
- Added dynamic/legacy marker stripping in both the browser parser and Claw bridge conversion path.
- Documented/tolerated `tool_calls: []` as migration-compatible final text while telling the model to omit `tool_calls` for final answers.
- Added sidecar browser metadata next to session JSONL files: `<session_id>.browser.json`.
- Added Tauri commands for browser metadata and restore:
  - `get_browser_current_url`
  - `session_browser_meta_read`
  - `session_browser_meta_write`
  - `session_browser_url_commit`
  - `restore_browser_for_session`
- Wired `ChatView.tsx` to:
  - initialize provider from `browser_last_site`
  - restore a saved browser thread when selecting a session with browser metadata
  - persist current provider/thread after browser-backed runs
  - avoid overwriting saved thread URLs with login/home/new-chat URLs
  - switch browser providers through session-aware restore instead of blind home navigation
- Added protocol regression tests in `tests/test_browser_protocol.py`.

Validation run in this patch environment:

```bash
python3 -m py_compile orchestrator/providers/browser_provider.py mcp_server/tools/claw_bridge.py
# Run as a script (not `python -m unittest tests...` — a top-level package named `tests`
# on PYTHONPATH can shadow this folder and crash).
PYTHONPATH=. python3 tests/test_browser_protocol.py
node TypeScript transpile syntax check for desktop/src/components/ChatView.tsx and desktop/src/types/index.ts
```

Rust could not be compiled in this environment because `rustc` is not installed here.
