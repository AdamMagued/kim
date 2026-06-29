# AGENTS.md — Kim Agent Platform (root router)

## What this is
Kim is a local AI agent platform (Tauri + React frontend, Python orchestrator, MCP server).
Real app root is `kim-pro/`. Never modify anything outside `kim-pro/`.

## Standing constraints (never violate)
1. **Code tab** must NEVER use OpenAI auth or gpt-5.5 — only ollama cloud or browser provider.
2. **CSS import order** in `desktop/src/index.css` is load-bearing (cascade). Do not reorder.
3. **System-prompt f-strings**: examples inside f-strings need doubled braces `{{ }}`.
4. After any Rust change, `tauri dev` needs a restart (hot-reload does not apply to Rust).
5. **Secret-file sandbox**: `mcp_server/config.validate_path()` denies secret files
   (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `credentials`, `.npmrc`,
   `.pypirc`) at any depth and sensitive dirs (`.ssh`, `.aws`, browser profiles, cloud
   SDKs, …). Don't weaken these globs/paths. Note: enforcement is filename/dir-based, not a
   content scanner — secrets pasted into a normally-named file are not caught.

## Architecture pointer
See `ARCHITECTURE.md` for the full layer diagram and IPC protocol spec.
See `PRODUCTION_ROADMAP.md` for the current work backlog and sequencing.

## Per-directory guides (load only the one you need)
| Directory | Guide | What's there |
|---|---|---|
| `orchestrator/` | `orchestrator/AGENTS.md` | Agent loop, providers, session store |
| `mcp_server/` | `mcp_server/AGENTS.md` | 31 OS-control tools, tool registry |
| `desktop/src/` | `desktop/src/AGENTS.md` | React UI, IPC event consumers |
| `desktop/src-tauri/` | `desktop/src-tauri/AGENTS.md` | Rust shell, subprocess mgmt |

## How-to recipes
`HOW_TO.md` — exact minimal file sets for common change patterns.

## Finding things fast
1. Know which file → open it directly.
2. Don't know → grep/find with a targeted pattern; `repomap.md` if it exists.
3. Never do a broad full-repo sweep as a first move.

## Test commands (all four suites — cli/ is a separate crate, easy to forget)
```
python -m pytest tests/            # 927+ Python tests (venv required)
cd desktop && npx tsc --noEmit && npm run test  # 73 Vitest tests
cd desktop/src-tauri && cargo test # 54 Rust tests (desktop)
cd cli && cargo test               # 90 Rust tests (kim CLI)
```
After every push, confirm the remote CI run is green: `gh run list --limit 1`.

## Known pre-existing issue
`desktop/src/components/PairingModal.tsx` — missing `qrcode.react` types. Ignore; predates recent work.
