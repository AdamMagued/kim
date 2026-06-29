# mcp_server/CLAUDE.md

## What lives here
The local MCP server exposing 50 OS-control tools to the orchestrator over stdio.

| File | Role |
|---|---|
| `server.py` | MCP server entry point; dispatches all tool calls |
| `tool_registry.py` | Schema + dispatch table for every tool (single source of truth) |
| `tool_tiers.py` | Risk tier mapping (mirrors `orchestrator/tool_risk.py`) |
| `config.py` | `PROJECT_ROOT`, allowed paths, config loading |
| `logger.py` | JSON-Lines structured logger (`logs/kim_YYYY-MM-DD.jsonl`) |
| `os_utils.py` | Cross-platform command translation and OS detection |
| `tools/` | One file per tool group: files, shell, screen, mouse, keyboard, windows, browser, web, git, code, search |
| `sites/` | Per-site web automation configs (`site_configs.py`, `FORM_SCHEMA`) |

## Local invariants
- **Schema ↔ dispatch parity**: every schema in `tool_registry.py` must have a corresponding dispatch entry and vice versa. Mismatch = startup error. Tests in `tests/test_invariants.py` verify this.
- **Stdout is sacred**: the MCP stdio transport uses stdout exclusively. All logging goes to stderr via `logger.py`. Never `print()` from a tool handler.
- **Path validation**: all file-path args must be validated against `PROJECT_ROOT`. Use `config.validate_path()`.
- **Tool handlers never raise**: wrap in try/except, return `{"error": "..."}` on failure.
- **Cross-platform**: new shell tools must go through `os_utils.translate_command()`.

## How to add an MCP tool
See `HOW_TO.md` → "Add an MCP tool" (4 files to touch).

## How to test this layer
```bash
python -m pytest tests/test_mcp*.py -v
python -m pytest tests/ -q  # full suite
```
