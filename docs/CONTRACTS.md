# Kim IPC Contracts — the four process seams

**Owner:** Team H (Operation Google-Level). **Status:** authoritative reference (Wave 1).
**Scope:** Kim is four cooperating processes. This document is the single source of
truth for what crosses each boundary — the message shapes, the transport, the
direction, and the authentication. Where the code and this doc disagree, the code
is a bug (see the linked `F-H-*` findings in `docs/ops/findings/team-h.md`).

```
┌─────────────┐  Tauri invoke/  ┌──────────────┐  stdout JSONL +  ┌───────────────┐  stdio     ┌────────────┐
│  React /    │◀───events──────▶│  Rust /      │◀──stdin lines───▶│  Python        │◀─JSON-RPC─▶│  MCP server │
│  TS (webview)│   (Seam 1)     │  Tauri (Rust)│    + /v1 HTTP     │  orchestrator  │  (Seam 3)  │  (tools)   │
└─────────────┘                 └──────────────┘    (Seam 2)       └───────────────┘            └────────────┘
                                       │                                   │
                                       │  /v1 HTTP (kimctl, webview bridge)│  spawns codex binary
                                       │  (Seam 2b)                        ▼
                                       │                          ┌────────────────────────┐
                                       └─────────────────────────▶│  codex bridge:          │
                                                                  │  _CodexProxy (OpenAI    │  (Seam 4)
                                                                  │  Responses/Chat) ⇄ codex│
                                                                  │  binary ⇄ BrowserProvider│
                                                                  └────────────────────────┘
```

The **generated seam** (Seam 1 + the typed half of Seam 2) has one source of truth:
`desktop/src/types/events.schema.json` → `npm run gen:events` →
`events.gen.ts` (TS), `events.gen.rs` (Rust `KimEvent` enum), `events_gen.py`
(Python emitters). CI (`.github/workflows/ci.yml:84-95`) fails if any generated
output drifts from the schema. **Everything NOT in that schema is an ungoverned
seam** — those are where the bugs live (F-H-1, F-H-5, F-H-6).

---

## Seam 1 — Frontend ⇄ Rust (Tauri `invoke` commands + emitted events)

Two sub-channels: **commands** (TS → Rust, request/response, `invoke(name, args)`)
and **events** (Rust → TS, fire-and-forget, `emit(name, payload)` / `listen(name)`).

### 1a. Command surface (TS `invoke` → Rust `#[tauri::command]`)

Census method: every `#[tauri::command]` fn vs every `invoke('name')` call site.
**Result: names are in full parity — 0 phantom invokes, 0 unused commands** (the one
apparent orphan, `load_run_history`, is invoked in `useSessionLoader.ts:65`).

> **Arg-key convention (load-bearing):** Tauri auto-converts `snake_case` Rust
> parameters to `camelCase` on the JS side. Call sites MUST pass camelCase keys
> (`resumeSessionId`, `projectRoot`, `ollamaBaseUrl`) even though the Rust fn
> declares `resume_session_id`. A snake_case key on an `Option<T>` param does NOT
> error — it silently arrives as `None`. This is the seam's sharpest footgun; it
> is why `send_task` (subprocess.rs:574) takes 15 `Option` params and the frontend
> (useTaskRunner.ts:207) passes camelCase.

| Module | Commands |
|---|---|
| `subprocess.rs` | `send_task`, `cancel_task`, `steer_task` |
| `session_commands.rs` | `list_sessions`, `summarize_session`, `load_session_messages`, `set_privacy_pause`, `get_privacy_pause`, `reveal_logs`, `get_app_version` |
| `account.rs` | `load_account`, `save_account`, `reset_onboarding`, `delete_all_sessions` |
| `secrets.rs` | `store_github_token`, `delete_github_token` |
| `data_io.rs` | `verify_github_pat`, `export_data`, `import_data`, `backup_to_gist`, `restore_from_gist` |
| `run_history.rs` | `save_run_history`, `load_run_history`, `get_platform_info`, `run_update` |
| `hitl.rs` | `hitl_respond_approval`, `respond_approval_decision`, `respond_user_input` |
| `google_oauth.rs` | `google_oauth_status`, `google_oauth_start`, `google_oauth_disconnect`, `google_oauth_test` |
| `provider_auth.rs` | `provider_check_auth`, `provider_signin`, `provider_signout` |
| `ollama.rs` | `ollama_get_status`, `ollama_test_model`, `ollama_signin`, `ollama_pull_model` |
| `codex_projects.rs` | `list_codex_projects`, `open_in_finder` |
| `schedule_commands.rs` | `list_scheduled_tasks`, `add_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task`, `run_due_scheduled_task`, `start_schedule_timer`, `stop_schedule_timer`, `get_schedule_timer_status` |
| `browser_bridge.rs` | `open_browser_signin_window` |
| `window_manager.rs` | `show_main_window`, `set_task_active_mode` |
| `screenshot_flash.rs` | `show_screenshot_flash` |
| `lib.rs` | `show_browser_window`, `session_browser_meta_write`, `session_browser_url_commit`, `restore_browser_for_session` |

All registered in one `generate_handler![...]` in `lib.rs:~1140`.

### 1b. Struct-shape drift (TS type vs Rust struct)

| Type | TS (`desktop/src/types/index.ts`) | Rust | Mismatch |
|---|---|---|---|
| `SessionInfo` | `session_id: string`, `session_key?`, `title?`, `message_count`, `session_type: 'kim'\|'codex'`, `pinned?`, `project_path?` | `lib.rs:194` `SessionInfo` — `session_key: String` (non-opt), `title: String` (non-opt), `session_type: String` | TS marks `session_key`/`title` optional; Rust always serializes them (non-`Option`). Harmless (TS accepts present). **`project_path` is on the TS `SessionInfo` but NOT on Rust `SessionInfo`** — it lives only on `CompletedCodexSession` (lib.rs:217). So `SessionInfo.project_path` is always `undefined` from `list_sessions`; the field is populated by a different path (RevampSidebar.tsx:722 stamps it client-side from the codex project list). A consumer assuming `list_sessions` returns it gets `undefined`. |
| `ToolResultBlock` | `{ type, tool_use_id, content }` (index.ts:51) | serialized by Python session JSONL, not a Rust struct | **The runtime also carries `output` — not modeled.** Read via `(trb as unknown as {output?}).output` in utils.ts:161,620 (Team F F-F-9). The double-unknown cast hides the drift from tsc. Canonical shape must model `content` OR `output`. |
| `BrowserSessionMeta` | `{ browser_threads, browser_last_site?, browser_threads_updated_at_ms?, last_llm_provider? }` | `lib.rs:229` — matches (`#[serde(default)]` on optionals) | ✓ aligned |
| `Settings` | `index.ts:171` | **No Rust struct** — persisted client-side in `localStorage['kim-settings']` (App.tsx:26,49), NOT via a Tauri command | Settings never cross this seam; individual fields are passed to `send_task` as camelCase args. No drift, but no server-side validation either. |

### 1c. Event channel (Rust `emit` → TS `listen`) — the full census

**Typed events** (schema-generated, decoded by `subprocess.rs::forward_agent_stdout_line`
from Python stdout, re-emitted as `kim:*`): `kim:status`, `kim:plan`, `kim:step`,
`kim:done`, `kim:context`, `kim:stats`, `kim:ui`, `kim:run-done`, `kim:run-failed`,
`kim:provider-error`, `kim:rate-limited`, `kim:hitl-approval-request`,
`kim:hitl-approval-result`, `kim:tool`, `kim:answer`, `kim:diff`, `kim:activity`,
plus the codex app-server set (`kim:command-approval-request`,
`kim:file-change-approval-request`, `kim:user-input-request`, `kim:command-output`,
`kim:assistant-delta`, `kim:reasoning-delta`, `kim:plan-update`, `kim:diff-update`,
`kim:token-usage`, `kim:item-lifecycle`, `kim:turn-lifecycle`). These are governed by
the schema on both sides. ✓

**Ungoverned events** (ad-hoc `app.emit`, NO schema entry, NO generated type):

| Event | Emitted at | Listener | Payload | Status |
|---|---|---|---|---|
| `kim-run-id` | subprocess.rs:709, tasks.rs | useChatStream.ts:606 | `{run_id, session_id}` | Live but off-schema — carries identity but shape unpinned (F-H-1) |
| `kim-agent-output` | subprocess.rs:290,293 + Rust status lines | useChatStream.ts:778 | raw `string` | Legacy text passthrough (Seam 2) |
| `kim-agent-done` | subprocess.rs:746,760 | useChatStream.ts:786 | bare `bool` | **Off-schema, un-enveloped, terminal (F-H-1).** The signal that clears `isRunning`. |
| `kim-agent-cancelled` | subprocess.rs:1017,1032 | useChatStream.ts:907 | bare `bool` | Off-schema, un-enveloped (F-H-1) |
| `kim-agent-error` | subprocess.rs:745, speed_access.rs | useChatStream.ts:782 | `string` | Off-schema |
| `kim-agent-code-session` | subprocess.rs:756 | useChatStream.ts:903 | `SessionInfo` | Off-schema |
| `kim-auth-changed` | provider_auth.rs et al. | App.tsx | — | Off-schema |
| `kim-update-progress` | updater.rs (×6) | UpdateModal.tsx | progress | Off-schema |
| `schedule-timer-tick` | scheduler.rs | SchedulePane | — | Off-schema |
| `kim-agent-started` | **tasks.rs:257 (HTTP bridge only)** | **none** | — | **ORPHAN emit (F-H-5)** — kimctl runs announce start; GUI never listens |
| `kim-tray-cancel` | **speed_access.rs:88** | **none** | `()` | **ORPHAN emit (F-H-5)** — tray "Cancel run" menu is dead |
| `kim-browser-hidden` | **browser_bridge.rs:137** | **none** | `bool` | **ORPHAN emit (F-H-5)** |
| `kim-ollama-changed` | **none** | ProviderPicker.tsx:102 | — | **ORPHAN listen (F-H-5)** — no producer |

**Contract rule (to enforce):** every event either side names must have a
counterpart on the other. A codegen check or test should fail on a one-sided event.
Run-lifecycle events (`kim-agent-done`/`-cancelled`/`-error`/`kim-run-id`) SHOULD be
migrated into the schema and stamped with the run envelope so the frontend can
attribute a run's terminal state to its owning session (root cause of F-F-2/F-F-5).

---
*(seams 2, 3, 4 + test plan below — written incrementally)*
