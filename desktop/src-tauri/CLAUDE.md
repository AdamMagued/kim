# desktop/src-tauri/CLAUDE.md

## What lives here
The Rust Tauri v2 backend: OS bridge, subprocess management, Tauri commands.

| File | Role |
|---|---|
| `src/lib.rs` | Central command router; spawns/manages the Python subprocess; ~2100 lines |
| `src/subprocess.rs` | `find_python_interpreter()`, subprocess spawn, stdout line reader |
| `src/main.rs` | Tauri app bootstrap |
| `src/config.rs` | App config loading (wraps `config.yaml`) |
| `src/google_oauth.rs` | PKCE OAuth 2.0 loopback flow for Gemini |
| `src/session_commands.rs` | Tauri commands for session JSONL reads |
| `src/schedule_commands.rs` | Tauri commands for cron/scheduled tasks |
| `src/account.rs`, `src/relay.rs`, etc. | Feature-specific command modules |

## IPC protocol (Python stdout → Rust → React)
Python emits newline-delimited lines on stdout. `lib.rs` reads them line-by-line and fires Tauri events:
- `[STATUS] <msg>` → `kim-agent-output` event
- `[PLAN]{json}` → parsed plan struct → Tauri event
- `[CONTEXT]{json}` → context meter update
- `[UI] SCREENSHOT_FLASH` / `[UI] SHOW` → window control
- Plain JSON `{"event": "kim:*", ...}` → typed event forwarded directly

Full spec: `ARCHITECTURE.md` § "Stdout Text Protocol".

## Local invariants
- **`find_python_interpreter()`** (`subprocess.rs`): resolution order is bundled-sidecar-first → `~/.kim_root` → `~/.kim` → system. Do not short-circuit this.
- **`tauri dev` restart required** after any `.rs` file change — Rust is not hot-reloaded.
- **No `unwrap()` in production paths** — use `?` or `match` with proper error propagation.
- **WebView label `"kim-browser-signin"`** is used by the OAuth flow heuristic. Do not rename.

## How to add a Tauri command
1. Implement `#[tauri::command] fn my_command(...)` in the appropriate `src/*.rs` module.
2. Register in `lib.rs` → `.invoke_handler(tauri::generate_handler![..., my_command])`.
3. Add TypeScript binding in `desktop/src/types/` and call via `invoke("my_command", ...)`.

## How to test this layer
```bash
cd desktop/src-tauri && cargo test   # 50 unit tests
cd desktop/src-tauri && cargo check  # type + borrow check (fast)
cd desktop/src-tauri && cargo clippy -- -D warnings
```
