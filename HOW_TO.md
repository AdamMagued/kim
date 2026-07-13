# HOW_TO.md — Golden-Path Recipes

Each recipe lists the exact minimal file set to touch. Read only those files.

---

## Add an MCP tool (3 files)

1. **`mcp_server/tools/<group>.py`** — implement `async def handle_<tool>(args) -> str`.
2. **`mcp_server/tool_registry.py`** — add the tool schema to the `TOOLS` list, a dispatch entry in the `DISPATCH` dict pointing to your handler, and a tier entry in `TIER_DISPATCH` (`file_read` / `git` / `shell` / …). All three tables live in this one file; `mcp_server/tool_tiers.py` only *filters* tools by `KIM_ENABLED_TOOL_TIERS` — no per-tool entry goes there.
3. **`tests/`** — add a unit test for the new tool (schema↔dispatch parity is enforced by `tests/test_invariants.py` and `tests/test_tool_registry_schema.py`).

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

## Add an agent event — end-to-end (3 files, 4 steps)

1. **`desktop/src/types/events.schema.json`** — define the new event shape.
2. Run **`npm run gen:events`** (from `desktop/`) — regenerates all three codegen targets: `desktop/src/types/events.gen.ts`, `desktop/src-tauri/src/events.gen.rs`, and `orchestrator/events_gen.py` (do not hand-edit any of them).
3. Emit site (e.g. **`orchestrator/agent.py`** / **`orchestrator/cli.py`**) — call the generated `emit_<event>(...)` helper from `orchestrator/events_gen.py`.
4. **`desktop/src/hooks/useChatStream.ts`** (or the relevant consumer) — add a listener for the typed `kim:<event>` Tauri event (names in `KimEventNames`, `events.gen.ts`).

Typed IPC (`ipc_protocol: typed`) is the default. Legacy mode (`ipc_protocol: legacy` in `config.yaml`) forwards raw stdout lines on `kim-agent-output`; text-tag parsing for that path lives in `desktop/src/components/chat/parsers.ts`.

---

## Fix a web automation issue (3–5 files)

1. **`mcp_server/tools/web/`** — main web tool handlers, split by concern: `actions.py` (`web_fill_form`, clicks), `navigation.py`, `observation.py` (`web_observe`, `FORM_SCHEMA`), `resolution.py`, `browser.py` (Playwright/CDP session).
2. **`mcp_server/tools/web_element_scoring.py`** — element resolver, synonym table, scoring logic.
3. **`orchestrator/providers/browser/site_configs.py`** — per-site overrides (`SITE_CONFIGS`) for the browser provider.
4. **`tests/evals/<fixture>.py`** — add or update a fixture-driven eval for the bug scenario (e.g. `tests/evals/test_web_fill_form_eval.py`).
5. **`tests/test_web_*.py`** — unit test for the specific fix (e.g. `test_web_resolver.py`, `test_web_wait_for.py`).

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
