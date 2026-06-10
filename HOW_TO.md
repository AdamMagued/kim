# HOW_TO.md — Golden-Path Recipes

Each recipe lists the exact minimal file set to touch. Read only those files.

---

## Add an MCP tool (4 files)

1. **`mcp_server/tools/<group>.py`** — implement `async def handle_<tool>(args) -> str`.
2. **`mcp_server/tool_registry.py`** — add the JSON schema under `TOOL_SCHEMAS` and a dispatch entry in `TOOL_DISPATCH` pointing to your handler.
3. **`mcp_server/tool_tiers.py`** — add a risk tier entry (`low` / `medium` / `high`).
4. **`tests/test_mcp_tools.py`** — add a unit test for the new tool.

Invariant: schema and dispatch entries must be added in the same commit — startup validation catches mismatches.

---

## Add a provider (3 files)

1. **`orchestrator/providers/<name>.py`** — subclass `BaseProvider`, implement `async def complete(messages, tools, system) -> dict`. Return `{"type": "tool_call", "tool": ..., "args": ...}` or `{"type": "text", "content": ...}`.
2. **`orchestrator/providers/__init__.py`** — add the provider to the factory mapping.
3. **`tests/test_providers.py`** — add the provider to the parametrized contract-test suite (text reply, tool call, tool-result round-trip).

---

## Add a settings pane (3 files)

1. **`desktop/src/components/settings/<PaneName>Pane.tsx`** — create the React pane component.
2. **`desktop/src/components/kim-ui/RevampSettings.tsx`** — add the pane to `PANE_META` and the pane router switch.
3. **`desktop/src/components/kim-ui/settings-panes/`** — if adding a nav icon, add it to `icons.tsx`.

---

## Add an agent event — end-to-end (3 files after V-1)

After the IPC refactor (V-1) lands, every new event touches exactly these:

1. **`events.schema.json`** (repo root) — define the new event shape.
2. Run **`npm run gen:events`** — regenerates `desktop/src/types/events.gen.ts` (do not hand-edit).
3. **`orchestrator/ui_bridge.py`** — add an emit method calling `self._emit("kim:<event>", payload)`.
4. **`desktop/src/components/ChatView.tsx`** (or the relevant consumer) — add a `listen("kim:<event>", ...)` handler.

Until V-1 lands: also emit the legacy text line from `ui_bridge.py` and add a regex in `desktop/src/components/chat/parsers.ts`.

---

## Fix a web automation issue (3–5 files)

1. **`mcp_server/tools/web.py`** — main web tool handlers (`web_fill_form`, `web_observe`, etc.).
2. **`mcp_server/tools/web_element_scoring.py`** — element resolver, synonym table, scoring logic.
3. **`mcp_server/sites/site_configs.py`** — per-site overrides and `FORM_SCHEMA` definitions.
4. **`tests/evals/<fixture>.py`** — add or update a fixture-driven eval for the bug scenario.
5. **`tests/test_web_tools.py`** — unit test for the specific fix.

---

## Run a targeted test pass (by layer)

```bash
# Python — all
python -m pytest tests/ -q

# Python — agent + providers only
python -m pytest tests/test_agent*.py tests/test_providers*.py -v

# Python — MCP tools only
python -m pytest tests/test_mcp*.py -v

# Python — web evals
python -m pytest tests/evals/ -v

# Frontend
cd desktop && npm run test && npx tsc --noEmit

# Rust
cd desktop/src-tauri && cargo test
cd cli && cargo test
```
