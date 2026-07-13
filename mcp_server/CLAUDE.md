# mcp_server/CLAUDE.md

## What lives here
The local MCP server exposing 58 OS-control tools to the orchestrator over stdio.

| File | Role |
|---|---|
| `server.py` | MCP server entry point; `call_tool` gates every call via `policy.enforce` then dispatches |
| `policy.py` | **The security chokepoint** (K1): `enforce(name,args)->PolicyDecision` (allow/deny/approve). Shell allowlist + argv rules (S2), path validation of all path args (S3). Called first in `call_tool`. |
| `approvals.py` | Server-side HITL: when policy says `approve`, blocks on the orchestrator's approval broker (`KIM_APPROVAL_SOCK`/`_TCP`); default-deny on any failure. |
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
- **Path validation**: all file-path args must be validated against `PROJECT_ROOT`. Use `config.validate_path()`. `policy.py` also validates every path-typed arg at the chokepoint (defense-in-depth).
- **Policy chokepoint**: `call_tool` calls `policy.enforce()` BEFORE dispatch. Never add a dispatch path that skips it. New shell binaries go in `policy._ALLOWED_MUTATING`/`_SAFE_READONLY` (or `config.yaml` `shell.allowlist_extra`/`safe_extra`), not around the gate.
- **Minimal subprocess env**: every tool subprocess uses `os_utils.minimal_subprocess_env()` (allowlist, no parent-env inherit) — never pass `env=os.environ` or omit `env=`.
- **Tool handlers never raise**: wrap in try/except, return `{"error": "..."}` on failure.
- **Cross-platform**: new shell tools must go through `os_utils.translate_command()`.

## How to add an MCP tool
See `HOW_TO.md` → "Add an MCP tool" (4 files to touch).

## How to test this layer
```bash
python -m pytest tests/test_mcp*.py -v
python -m pytest tests/ -q  # full suite
```
