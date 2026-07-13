# desktop/src-tauri/CLAUDE.md

## What lives here
The Rust Tauri v2 backend: OS bridge, subprocess management, Tauri commands.

| File | Role |
|---|---|
| `src/lib.rs` | Central command router + shared statics (~1300 lines) |
| `src/task_spec.rs` | **K2, pub**: pure `TaskSpec` + `EnvBuilder` + named spec builders (`chat_task_spec` / `codex_browser_spec` / `codex_direct_spec`) — both spawn paths build here |
| `src/spawn_supervisor.rs` | **K2**: `reserve_slot → spawn → supervise` lifecycle against the single `TaskRuntime` (stdout/stderr pumps, pid-guarded cleanup) |
| `src/task_runtime.rs` | Single-runner slot: pid, `starting` flag, one tokio stdin handle for HITL/steer |
| `src/subprocess.rs` | `send_task` (GUI orchestration, <150 lines), `forward_agent_stdout_line` (the one IPC translator), `find_python_interpreter()`, cancel/signals |
| `src/http_bridge.rs` | `/v1/*` HTTP endpoints (kimctl) — `/v1/task` uses the same builders + supervisor |
| `src/codex_route.rs` | Provider routing (`ProviderRoute`) for direct Codex/Claw CLI runs |
| `src/hitl.rs` | Approval-transport seam: `hitl_respond_approval`, `hitl_approve` line format |
| `src/main.rs` | Tauri app bootstrap |
| `src/config.rs` | App config loading (wraps `config.yaml`), incl. `ipc_protocol` (default `typed`) |
| `src/google_oauth.rs` | PKCE OAuth 2.0 loopback flow for Gemini |
| `src/session_commands.rs` | Tauri commands for session JSONL reads |
| `src/schedule_commands.rs` | Tauri commands for cron/scheduled tasks |
| `tests/task_spawn.rs` | T2 integration tests on the pub `task_spec` seam |

## IPC protocol (Python stdout → Rust → React)
Python emits newline-delimited JSON events on stdout; `subprocess.rs::forward_agent_stdout_line`
decodes them into the generated `KimEvent` enum (`events.gen.rs`) and re-emits
typed `kim:*` Tauri events (`ipc_protocol: typed`, the default). Legacy mode
forwards raw lines on `kim-agent-output`. The event vocabulary lives in
`desktop/src/types/events.schema.json` (codegen via `npm run gen:events`).

Full spec: `ARCHITECTURE.md` § "Stdout Text Protocol".

## Local invariants
- **Spawn changes go in `task_spec.rs` builders**, never inline in `send_task`
  or the `/v1/task` handler — that is what keeps the two paths from diverging.
- **`find_python_interpreter()`** (`subprocess.rs`): resolution order is bundled-sidecar-first → `~/.kim/venv` (or `~/.kim/.venv`) → project-local venv (`venv/`, `.venv/`) → bare system `python3`/`python`. Do not short-circuit this. When the search falls through to a bare system python, `preflight_python_deps()` probes `import mcp, anthropic` and fails the spawn with an actionable message if Kim's deps are missing. (The former `~/.kim_root`-as-directory arm was dead — install.sh writes `~/.kim_root` as a *file* — and has been removed from the code.)
- **`tauri dev` restart required** after any `.rs` file change — Rust is not hot-reloaded.
- **No `unwrap()` in production paths** — use `?` or `match` with proper error propagation.
- **WebView label `"kim-browser-signin"`** is used by the OAuth flow heuristic. Do not rename.

## How to add a Tauri command
1. Implement `#[tauri::command] fn my_command(...)` in the appropriate `src/*.rs` module.
2. Register in `lib.rs` → `.invoke_handler(tauri::generate_handler![..., my_command])`.
3. Add TypeScript binding in `desktop/src/types/` and call via `invoke("my_command", ...)`.

## How to test this layer
```bash
cd desktop/src-tauri && cargo test   # unit tests + tests/task_spawn.rs integration
cd desktop/src-tauri && cargo check  # type + borrow check (fast)
cd desktop/src-tauri && cargo clippy -- -D warnings
```
