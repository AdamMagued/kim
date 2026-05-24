# AI Edit Guide

Rules for any AI agent (Claude Code, Copilot, Cursor, etc.) editing this repo.

---

## Before you touch anything

1. Read `AI_RESTRUCTURE_BASELINE.md` — it lists every high-risk file and known issue.
2. Read `CONTRACTS.md` — it defines the stdout protocol, provider response shapes, and session format. These are **inviolable**.
3. Read `SOURCE_OF_TRUTH.md` — it tells you which file owns each concept. Don't duplicate logic.

## Golden rules

1. **Never change the stdout protocol markers.** `[STATUS]`, `[TOOL]`, `[SUCCESS]`, `[FAILED]`, `[PLAN]{json}`, `[STEP]{json}`, `[DONE]{json}` are consumed by both the Tauri frontend and the Rust CLI. Changing format, adding whitespace, or reordering fields will silently break the UI.

2. **Never change the provider response shape.** All providers return `{"type": "tool_call", "tool": str, "args": dict}` or `{"type": "text", "content": str}`. The agent loop depends on this exact shape.

3. **Don't touch generated/runtime directories.** `node_modules/`, `target/`, `__pycache__/`, `sessions/`, `logs/`, `venv/` are not source code.

4. **Run tests after every change.** See `TEST_MATRIX.md` for commands. If a test that passed before now fails, your change broke something — fix it before committing.

5. **One concern per commit.** Don't mix refactoring with bug fixes. Don't mix structural changes with behavior changes.

6. **Preserve behavior exactly.** When splitting a file, the public API (function signatures, class interfaces, import paths) must not change. Re-export from the original location if needed.

7. **No new dependencies without justification.** Adding a pip/npm dependency is a decision that affects all platforms. Say why in the commit message.

## File size limits

If a file exceeds 500 lines, it's a candidate for splitting. If it exceeds 1000 lines, it should be split before adding more code.

## Import conventions

- Python: relative imports within a package (`from .memory import ConversationMemory`), absolute for cross-package (`from mcp_server.config import PROJECT_ROOT`)
- TypeScript: relative imports within `src/` (`import { SessionInfo } from '../types'`)
- Rust: `mod` declarations in the parent, `use crate::` for cross-module

## Testing requirements

- Any new module split must have at least one test proving the contract is preserved
- Any new function in the stdout protocol path must be tested against `CONTRACTS.md` examples
- See `TEST_MATRIX.md` for the full test inventory
