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

## Seam 2 — Rust ⇄ Python (stdout line protocol + stdin lines + /v1 HTTP bridge)

The orchestrator/bridge process is spawned by Rust (`spawn_supervisor::spawn` from a
`TaskSpec`). Rust feeds it **stdin lines** (steering, approvals) and consumes its
**stdout lines** (events). Separately, a **loopback HTTP bridge** (`/v1/*`) lets the
Python side (and kimctl, and the webview) call back into Rust.

### 2a. The stdout line protocol — GRAMMAR (previously unwritten; this is the spec)

Rust's `spawn_supervisor.rs` pump uses `BufReader::lines()`/`next_line()`, so the
consumer is **guaranteed whole newline-delimited lines** — a JSON frame is never split
(Team F confirmed: `parseAgentLine` never sees a partial frame). Each line is exactly
one of:

```
line            ::= typed-event-line | legacy-line
                    # Rust tries typed decode FIRST; on failure falls through to legacy.

# ---- Typed events (the governed path) ----
typed-event-line::= compact-json-object , newline
                    # { "type": <TYPE> [, <payload fields...>]
                    #   [, "run_id": <str>] [, "session_id": <str>] }
                    # Emitted ONLY by orchestrator/events_gen.py::emit_*.
                    # The run_id/session_id envelope is appended iff KIM_RUN_ID /
                    # KIM_SESSION_ID are in the process env (see 2d + F-H-8).
<TYPE>          ::= "status" | "plan" | "step" | "done" | "context" | "stats"
                  | "ui_screenshot_flash" | "ui_show"     # NB: kim:ui splits into
                                                          # two wire types, no "action" field
                  | "run_done" | "run_failed" | "provider_error" | "rate_limited"
                  | "hitl_approval_request" | "hitl_approval_result"
                  | "tool" | "answer" | "diff" | "activity"
                  | "command_approval_request" | "file_change_approval_request"
                  | "user_input_request" | "command_output" | "assistant_delta"
                  | "reasoning_delta" | "plan_update" | "diff_update"
                  | "token_usage" | "item_lifecycle" | "turn_lifecycle"
                    # snake_case of the schema typeNames; the ONLY authoritative list
                    # is events.schema.json. Rust decodes via #[serde(tag="type",
                    # rename_all="snake_case")] on the KimEvent enum (events.gen.rs).

# ---- Legacy text lines (the ungoverned path) ----
legacy-line     ::= tag-line | plan-envelope-line | diff-line | free-text
tag-line        ::= <TAG> SP text
<TAG>           ::= "[STATUS]" | "[STATS]" | "[CONTEXT]" | "[TOOL]" | "[ANSWER]"
                  | "[SUCCESS]" | "[FAILED]" | "[ERROR]"
                  | "TASK_COMPLETE:" | "NEED_HELP:"
                    # Source of truth for the vocabulary: events_gen.py LOG_TAG_*
                    # constants (K5) + events.schema.json "legacyTags".
plan-envelope-line ::= "[STATUS]" SP ("[PLAN]"|"[STEP]"|"[DONE]") json-object
                    # DOUBLE-WRAPPED: a plan marker rides inside a [STATUS] line.
                    # agent.py:341-379 emits it; parsers.ts:54 special-cases the nesting.
diff-line       ::= "[DIFF] path=" basename " +" int " -" int [" duration_ms=" int]
                    # basename must be space-free: parser regex is path=(\S+).
                    # A filename with spaces mis-parses here (typed kim:diff is safe).
tool-line       ::= "[TOOL]" SP [module ": "] tool_name "(" json-args ")"
                    # Frontend re-parses json-args with a hand-rolled brace/quote
                    # state machine (utils.ts:511-524) because args may contain
                    # unbalanced characters. Emit shape lives only in the f-string.
free-text       ::= anything else — stderr echoes, codex CLI JSONL, tracebacks,
                    Python crash lines ("ModuleNotFoundError: ..."). Surfaced
                    best-effort by parseLogLine's noise/crash heuristics.
```

**Emit sites** (who writes this protocol):
- Typed: `orchestrator/events_gen.py` only (generated).
- Legacy: `agent.py::_log` (INFO logs mirrored to stdout in Tauri mode),
  `cli.py:137-138`, `codex_bridge_service.py` (`print(f"{LOG_TAG_…} …")`),
  `codex_engine/engine.py` (`print(f"{LOG_TAG_STATUS} …")`), and **Rust itself**
  pre-spawn (`subprocess.rs:866-940` emits `[STATUS] …` strings straight onto
  `kim-agent-output` before the child starts).

**Parse/route** (`subprocess.rs::forward_agent_stdout_line(ipc_typed, is_codex, line)`):

| line | typed mode + chat | typed mode + codex | legacy mode |
|---|---|---|---|
| valid `KimEvent` JSON | decode → `emit("kim:*")` + `merge_run_envelope` | same | raw → `kim-agent-output` (frontend's `decodeKimEventLine` swallows it) |
| anything else | **DROPPED silently** (F-H-3) | raw → `kim-agent-output` | raw → `kim-agent-output` |

Frontend legacy parsing (`chat/parsers.ts` + `chat/utils.ts::parseLogLine`) matches
tags by **substring** (`raw.includes('[SUCCESS]')`), not anchored prefix — so any text
that merely contains a tag token is reclassified (F-H-6). `[CONTEXT]`/`[STATS]` are in
the `legacyTags` list but are no longer emitted as text (typed-only), so those parser
branches are dead.

**Termination contract (the split-brain — F-H-1 / F-H-2):**

| Spawn shape | Terminal signal(s) | Governed? |
|---|---|---|
| Chat (`orchestrator.agent`) | typed `run_done{termination, success}` + optional `answer`/`activity` detail (cli.py:118-135), then process exit → Rust emits untyped `kim-agent-done{bool}` | run_done ✓ / done ✗ |
| Codex browser-bridge (`codex_bridge_service`) | **only** legacy `TASK_COMPLETE:` / `[FAILED]` / `[ERROR]` text lines, then `kim-agent-done{bool}` — **NO typed run_done/run_failed** | ✗ |
| Codex direct (`codex exec --json`) | codex's own JSONL (parsed by `codexEvents.ts`), then `kim-agent-done{bool}` | ✗ (foreign schema) |
| Crash / kill | possibly only `kim-agent-done{false}` (subprocess.rs:746, M-PROC-6 guarantees it even on wait() error) | ✗ |

The one guaranteed terminal signal across ALL shapes is the **untyped, un-enveloped**
`kim-agent-done{bool}`. This is why the frontend needs a watchdog (F-F-5) and why the
Code tab's termination is magic-string-only (F-H-2).

### 2b. The stdin line protocol (Rust → Python) — one JSON object per line

Two DIFFERENT consumers exist depending on spawn shape (a wrong-type line is silently
ignored — nothing enforces routing):

| `type` | Fields | Rust producer | Python consumer |
|---|---|---|---|
| `user_steer` | `text` | `steer_task` (subprocess.rs:304) | `ui_bridge.py::StdinPump._dispatch` → agent injects as user msg |
| `hitl_approve` (alias `hitl_approval`) | legacy `approved: bool`, or K1/T1 `id, decision: "accept"\|"acceptForSession"\|"decline"` | `hitl_respond_approval` (hitl.rs:30) | `StdinApprovalBridge` (id-gated: stale ids voided, T1) |
| `approval_decision` | `id, decision` | `respond_approval_decision` (hitl.rs:84) | appserver `_StdinDecisionPump.read_decision` |
| `user_input` | `id, answers` | `respond_user_input` (hitl.rs:107) | appserver `_StdinDecisionPump.read_user_input` |

`normalize_decision` (ui_bridge.py:205) maps both the legacy `{approved}` and the K1
`{decision}` shapes; `accept_for_session` is normalized to `acceptForSession`.

### 2c. The /v1 loopback HTTP bridge (Rust `http_bridge/`, the OTHER direction)

Server: 127.0.0.1, ports 18991+, bound by Rust. **Auth contract:** every route requires
header `X-Kim-Token` (constant-time compared, #19) EXCEPT `GET /v1/health`. Body cap
32 MB (M-BRIDGE-3). The `/v1/result/{id}` dynamic route is handled AFTER the token gate,
so it IS authenticated (F-H-9 verified-not-a-vuln, but the auth rule is undocumented →
pin it with a test). Token file `~/.kim/bridge_token` (0600, but any same-user process
can read it — threat model in Team C F-C-4 / Team D F-D-3/F-D-4).

Clients: kimctl, `BrowserProvider` (in-app bridge mode), `bridge_client.py`, and the
provider-page JS injected into the webview (token embedded — F-D-4).

| Route | Method | Purpose | Notes |
|---|---|---|---|
| `/v1/health` | GET | liveness | **unauthenticated** (only exemption) |
| `/v1/task` | POST | spawn an agent run (reuses `send_task`'s TaskSpec builders) | emits orphan `kim-agent-started` (F-H-5) |
| `/v1/cancel` | POST | cancel the active run | |
| `/v1/task/approve` | POST | answer a HITL approval over HTTP (kimctl) | |
| `/v1/status` | GET | run status | token-gated since #12 |
| `/v1/send` | POST | push a prompt into the provider webview (browser provider) | |
| `/v1/complete` | POST | provider complete (token passed through) | resolves via `provider_url.rs` allowlist |
| `/v1/open` | POST | navigate the webview to a URL | **no allowlist — SSRF (F-D-1)** |
| `/v1/callback`, `/v1/result/{id}` | POST/GET | async browser-result delivery + pickup | keyed by request id |
| `/v1/provider` | POST | switch provider webview | allowlist-checked |
| `/v1/browser/{show,hide,click,new-chat,current-url}` | POST/GET | webview control | |
| `/v1/browser/{meta(GET/POST),commit-url,restore}` | | `BrowserSessionMeta` thread-state sidecar | `restore` exact-origin-checked (safe) |
| `/v1/hide`, `/v1/show` | POST | main-window visibility (screenshot blink) | |

### 2d. Run-identity envelope (the cross-cut that ties Seam 1 events to a run)

`events_gen.py::emit_event` appends `run_id`/`session_id` from `KIM_RUN_ID`/
`KIM_SESSION_ID` env; `subprocess.rs::merge_run_envelope` copies them from the raw line
onto the curated `kim:*` payload (the typed enum drops them on decode). **Only
`chat_task_spec` exports these env vars** — `codex_browser_spec` does not (F-H-8), so
Code-tab typed events cross the seam envelope-less and the frontend routes them by
mounted view (defeats the stated guarantee; root cause of F-F-2/F-F-8).

---

## Seam 3 — Python orchestrator ⇄ MCP server (stdio JSON-RPC)

Transport: the `mcp` SDK over stdio (`mcp_server/server.py::main` → `stdio_server()`).
The orchestrator is the client (`agent.py` holds `self.session`, calls
`session.call_tool`); the server hosts 50 tools. Server stdout is protocol-only —
`_protect_stdio_pipe()` rebinds `print` to stderr at runtime so a stray tool `print`
can't corrupt the JSON-RPC pipe.

### 3a. Tool advertisement

`list_tools()` returns `_TOOLS` — `TOOLS` (tool_registry.py) filtered by
`KIM_ENABLED_TOOL_TIERS`, plus site-connector tools merged at startup. Each `Tool` has
`name`, `description`, `inputSchema` (JSON Schema with `properties` + `required`).
**Schema↔dispatch parity is enforced**: every schema has a dispatch handler and vice
versa (startup check + `tests/test_invariants.py:33`). Connector tool-name collisions
are a hard `RuntimeError` at startup (server.py:88).

### 3b. Call contract (`call_tool(name, arguments) -> list[TextContent]`)

```
request : JSON-RPC "tools/call" { name: str, arguments: dict }
flow    : handler = _DISPATCH.get(name)
          if handler is None            -> [TextContent("Unknown tool: <name>")]
          decision = policy.enforce(name, args)      # ALWAYS first; never raises
          if decision.action == "deny"  -> [TextContent(decision.message)]   # "POLICY_DENIED: ..."
          if decision.action == "approve" and not session-approved:
              outcome = await approvals.request_approval(...)   # broker round-trip, default-deny
              if outcome not in ("accept","acceptForSession") -> [TextContent("HITL_DENIED: ...")]
          result = await handler(args)                # handlers return str
          return [TextContent(str(result))]
except PermissionError as e -> [TextContent("PERMISSION_ERROR: <e>")]
except Exception     as e   -> [TextContent("ERROR: <e>")]
```

**The response is ALWAYS `list[TextContent]` with `isError` unset** — success, denial,
unknown-tool, and exception are indistinguishable at the MCP protocol level; the client
discriminates purely by **string prefix** (F-INH-6). Works because both ends are
in-repo, but it is a brittle contract seam.

**No argument validation at the boundary (F-H-4):** the advertised `inputSchema`
(`required`, types) is documentation for the model only — `call_tool` never validates
`arguments` against it. Handlers read required args positionally (`args["path"]`), so a
missing required field raises `KeyError` → generic `ERROR: 'path'`, which classifies as
`execution_error`, NOT the (still-unpopulated) `bad_args` code. Type coercion is likewise
absent. **Note the doc-vs-reality gap:** `mcp_server/CLAUDE.md` states "tool handlers
never raise; return `{"error": "..."}` on failure", but the actual handlers return
`str` with `ERROR:`-style prefixes and rely on `server.py`'s `except` — the `{"error"}`
dict contract is aspirational, not what crosses the seam.

### 3c. Error-shape vocabulary — THE contract (string prefixes on the result text)

The agent's only way to know a call failed is the leading bytes of the result string.
This vocabulary is the real seam contract:

| Prefix | Producer | `tool_errors.classify_tool_output` code | in `interaction_policy` failure set |
|---|---|---|---|
| `PERMISSION_ERROR:` | server.py:152 / tools | `permission_denied` | ✓ |
| `BLOCKED:` | shell tools | `blocked` | (via POLICY_BLOCK) |
| `POLICY_DENIED:` | policy.py `_deny` messages | **MISSING** (F-H-9-class gap) | ✓ |
| `HITL_DENIED:` | server.py:141 | **MISSING** | ✓ |
| `Unknown tool:` | server.py:115 | **MISSING** | ✓ |
| `TIMEOUT:` | tools | `timeout` | ✓ |
| `OS_LIMITATION:` | tools | `os_limitation` | — |
| `NOT_FOUND:` | tools | `not_found` | — |
| `ERROR calling ` | agent.py:1417,1422 (client-side transport/timeout) | `internal_error` | ✓ (`ERROR`) |
| `ERROR:` | tools / server.py:155 | `execution_error` | ✓ (`ERROR`) |

**Two divergent copies of this vocabulary exist** — `orchestrator/tool_errors.py`
(`_PREFIX_TO_CODE`) and `orchestrator/interaction_policy.py:32-33` (failure-prefix set).
`POLICY_DENIED`/`HITL_DENIED`/`Unknown tool:` are recognized by `interaction_policy` but
NOT mapped by `tool_errors.classify_tool_output` (they fall through to `None` = "not an
error"), so a denied tool is not counted as an error by the classifier. Canonical list =
this table; the fix is a single shared constant module both import.

### 3d. Client-side timeout (double-execution guard)

`agent.py::_execute_tool` sets `_call_timeout = approval_backstop + exec_ceiling + margin`,
deliberately chosen to **strictly exceed** the server's worst case so `asyncio.wait_for`
never abandons a call the server is still running (which would make the model re-issue it
and double the side effect — the inherited finding-2.1 fix). Tool results are joined from
`[c.text for c in result.content if hasattr(c,"text")]`; a result with no text parts
becomes `"(no output)"`.

---

## Seam 4 — Codex bridge (proxy ⇄ codex binary ⇄ browser provider)

The Code tab runs the real `codex` binary but routes its model calls through Kim's
`BrowserProvider` (no OpenAI key — invariant 1). Two transports, selected by
`codex_appserver_transport.transport_name(config)` (`codex_bridge.transport`, default
`app-server`; unknown values degrade to default):

### 4a. `exec` transport — `_CodexProxy` (codex_engine/engine.py:330)

A loopback aiohttp server impersonating the OpenAI API. `codex exec --json` is spawned
with `OPENAI_API_KEY` = a per-run `secrets.token_urlsafe(32)` bearer, verified
constant-time on every request (#47). Endpoints:

| Endpoint | Method | Fidelity contract |
|---|---|---|
| `POST /v1/responses` | codex Responses API | primary. Request → prompt via `_extract_prompt_from_responses_request`: `instructions`→`[SYSTEM PROMPT]` ✓ (the instruction-drop bug's fix), `tools`→`[AVAILABLE CODEX TOOLS]` **prose** (F-H-7), input items→`[USER]`/`[ASSISTANT]`. Reply ← `_provider_response_to_responses_api`: expects browser model to emit `{"text":…,"tool_calls":[{name,input}]}`; `_normalize_tool_calls` snaps invented names/keys onto request tools (`command`→`cmd`). Returns SSE (`_sse_or_json`) when `stream:true`, else JSON. |
| `POST /v1/chat/completions` | OpenAI Chat | secondary. Same lossy prose flattening; no delta/thread state. |
| `GET /v1/models` | — | returns a single stub `kim-proxy-model`. |

Fidelity gaps (all in F-H-7): tool JSON schemas become prose; `function_call_output`
content is `" ".join(str(...))`-flattened before the browser sees it; the model's
emitted `tool_calls[].input` is NOT validated against the declared `parameters`. Robustness
machinery layered on top: auto-compaction above a per-provider token threshold,
`_nudge_contract_retry` (one re-ask when a reply ignored the JSON contract),
loop-guard (identical/subset repeated tool calls end the turn with an honest final answer),
`MAX_RELAYS=50` per turn (`begin_turn()` resets it), and a `Continue.`-only-delta shortcut
that returns the cached response. Cross-task browser-thread state (system-prompt already
sent, handoff, turns) lives in the `codex_engine/thread_state.py` sidecar and is mutated
in place.

### 4b. `app-server` transport (orchestrator/codex_appserver_transport.py — the default)

`codex app-server` speaks newline-delimited **JSON-RPC 2.0** on stdin/stdout (verified
against codex-cli 0.134.0, `docs/APPENDIX_appserver_probe_findings.md`). The model still
routes through `_CodexProxy` (`modelProvider: "kim-proxy"`, inline `config` overrides set
`base_url` to the proxy, `wire_api="responses"`; bearer via `CODEX_API_KEY`,
`build_appserver_env`). This transport adds native per-command approvals, live output, and
true session resume.

**Client → server methods:** `initialize`, `thread/start`, `thread/resume`, `turn/start`,
`turn/interrupt`, `turn/steer`, `thread/compact/start`.

**Server → client REQUESTS (must answer or codex hangs)** — translated to Kim events, the
answer written back as a stdin decision line (Seam 2b):

| codex method | Kim event emitted | Answered via |
|---|---|---|
| `item/commandExecution/requestApproval` (+ v1 `execCommandApproval`) | `command_approval_request` | `approval_decision` stdin (`_V1_DECISION`: accept→approved, acceptForSession→approved_for_session, decline→denied) |
| `item/fileChange/requestApproval` (+ v1 `applyPatchApproval`) | `file_change_approval_request` | `approval_decision` stdin |
| `item/tool/requestUserInput` | `user_input_request` (kind=questions) | `user_input` stdin `{id, answers}` |
| `mcpServer/elicitation/request` | `user_input_request` (kind=elicitation) | auto-declined today (informational) |

**Server → client NOTIFICATIONS → Kim events:** `turn/started|completed|interrupted|failed`
→ `turn_lifecycle`; `item/started|completed` → `item_lifecycle`;
`item/agentMessage/delta` → `assistant_delta`; `item/reasoning/textDelta` →
`reasoning_delta` (provider names scrubbed by `_PROVIDER_NAME_RE`);
`item/commandExecution/outputDelta` → `command_output`; `turn/plan/updated` →
`plan_update`; `turn/diff/updated` → `diff_update`; `thread/tokenUsage/updated` →
`token_usage`; `thread/compacted` → native compaction handled inline.

**Known gaps vs `docs/PROPOSAL_codex_appserver_parity.md`:**
- `compact_codex_thread` (transport:1025-1043) issues `thread/resume` with only
  `{threadId, cwd}` — omitting `modelProvider:"kim-proxy"`/`config`/policies that the real
  turn path supplies — so the resumed thread has no route to the proxy and the codex-side
  half of `/compact` silently never runs (**Team A F-A-4**).
- `_on_token_usage` awaits `thread/compact/start` (timeout 30s) inline in the notification
  pump, stalling all other notifications for up to 30s (**Team A F-A-5**).
- Dynamic client-side tools (`item/tool/call`) and `account/chatgptAuthTokens/refresh` are
  listed in the probe as server requests but are not wired — a codex build that issues them
  would hang (no handler → no response).

### 4c. Directional summary

```
codex binary ──JSON-RPC (app-server)──▶ transport.py ──emit_*──▶ stdout ──▶ Rust ──▶ frontend
     ▲                                       │
     │  model call (OpenAI Responses)        │ approval/user-input answer (stdin line)
     ▼                                       ▼
 _CodexProxy ──complete()──▶ BrowserProvider ──▶ provider webview (chatgpt.com / gemini / …)
```

The browser provider itself has its own contract (`[END_OF_RESPONSE_{id}]` sentinel — a
behavioral invariant) documented in Team B's `team-b.md` V-3 matrix; that is the fifth,
in-process seam and is out of scope for this cross-process doc.

---

## 5. Contract-drift quick reference (what fails silently today)

| Seam | Silent-failure class | Finding |
|---|---|---|
| 1 | Run-terminal events off-schema + un-enveloped; frontend can't attribute done/cancel | F-H-1 |
| 1 | Four orphaned event channels (3 emit-no-listener, 1 listen-no-emit) | F-H-5 |
| 1 | `SessionInfo.project_path` / `ToolResultBlock.output` type-vs-runtime drift | F-H (1b), F-F-9 |
| 2 | Non-JSON chat stdout dropped silently in typed mode | F-H-3 |
| 2 | Codex bridge termination is magic-string-only (no typed run_done) | F-H-2 |
| 2 | Tag protocol matched by substring, not prefix; grammar was unwritten | F-H-6 |
| 2 | Codex spawn spec omits KIM_RUN_ID/SESSION_ID → events un-enveloped | F-H-8 |
| 3 | `inputSchema.required` never enforced → cryptic `ERROR: 'path'` | F-H-4 |
| 3 | `isError` never set; error is string-prefix-only; two divergent prefix copies | F-INH-6, F-H (3c) |
| 4 | Tool schemas flattened to prose; tool_call input unvalidated | F-H-7 |
| 4 | `compact_codex_thread` resume omits proxy config → no-op | F-A-4 |

---

## 6. Golden-transcript TEST PLAN (finishes the abandoned V-3)

The principle: **each seam gets a recorded golden transcript and a test on BOTH sides**
that asserts the wire bytes round-trip. A schema change that breaks a contract must break
a test, not production. Fixtures live under `tests/golden/` (Python) and
`desktop/src/**/__tests__/golden/` (TS); the same `.jsonl` is shared where both sides read it.

### 6.1 Seam 1 — Frontend ⇄ Rust
- **Command parity test** (Rust + TS): a generated test enumerates every
  `#[tauri::command]` and asserts a matching `invoke('<name>')` exists in TS (and vice
  versa). Fails on a renamed/removed command. (Closes the census in §1a as a guard.)
- **Struct round-trip** (Rust → TS): for each shared struct (`SessionInfo`,
  `CompletedCodexSession`, `BrowserSessionMeta`, `KimAccount`, `CodexProject`), serialize a
  fixture in Rust (`serde_json::to_string`), commit it as golden, and in a TS test
  `JSON.parse` it against the TS interface (via a type-assertion helper or zod schema).
  Asserts optionality/field-name drift breaks the build — would have caught
  `ToolResultBlock.output` (F-F-9) and `SessionInfo.project_path` (§1b).
- **Event parity test**: enumerate every `emit(...)` name in Rust and every `listen(...)`
  name in TS; assert bijection except a documented allowlist. Fails on the four orphans
  (F-H-5).

### 6.2 Seam 2 — Rust ⇄ Python
- **Emit→decode golden** (Python → Rust): for every `events_gen.emit_*`, capture the exact
  stdout line into `tests/golden/events/*.jsonl`; a Rust test feeds each line to
  `serde_json::from_str::<KimEvent>` and asserts it decodes to the expected variant with the
  envelope preserved by `merge_run_envelope`. (Extends the existing `test_parse_*` in
  subprocess.rs to full coverage, driven by the schema.)
- **Grammar conformance** (the tag protocol): a golden file of representative legacy lines
  (`[STATUS]`, `[STATUS] [PLAN]{…}`, `[TOOL] mod: name({…})`, `[DIFF] path=…`,
  `TASK_COMPLETE:`, `NEED_HELP:`, a free-text crash line) fed to BOTH the frontend
  `parseAgentLine`/`parseLogLine` (Vitest) and — for the typed subset — the Rust decoder,
  asserting each classifies to the documented §2a production. Add adversarial cases: a line
  whose *payload* contains `[SUCCESS]` (F-H-6), a `[DIFF]` path with a space, a partial JSON
  line (must not crash).
- **Termination-contract test**: drive a `KIM_FAKE=1` chat run and a codex-bridge run;
  assert a chat run emits typed `run_done` AND `kim-agent-done`, and record (as a known-gap
  xfail until F-H-2) that the codex run currently emits neither typed terminal event.
- **Stdin round-trip** (Rust → Python): golden lines for `user_steer`, `hitl_approve`
  (both legacy `{approved}` and K1 `{id,decision}`), `approval_decision`, `user_input`;
  a Python test feeds each to `StdinPump._dispatch` / `_StdinDecisionPump` and asserts the
  routing + `normalize_decision` output.
- **Bridge auth test** (Rust): hit every `/v1/*` route with no `X-Kim-Token` and assert 401
  except `GET /v1/health` — pins the §2c exemption list (F-H-9).

### 6.3 Seam 3 — Python ⇄ MCP
- **Error-vocabulary golden**: a table test (the §3c table as data) asserting each prefix
  maps to the expected `tool_errors.classify_tool_output` code AND is in the
  `interaction_policy` failure set — fails today on `POLICY_DENIED`/`HITL_DENIED`/`Unknown
  tool:` (drives the single-shared-constant fix).
- **Missing-required-arg test**: call each tool with a required field omitted; assert a
  `BAD_ARGS:`-prefixed result (drives F-H-4's schema validation + `bad_args` code). Until
  fixed, an xfail documents the cryptic `ERROR: 'path'`.
- **Response-shape test**: assert `call_tool` always returns `list[TextContent]` and (post
  F-INH-6 fix) sets `isError` for deny/unknown/exception while keeping the string prefixes.
- **Schema↔dispatch parity**: already covered by `tests/test_invariants.py` — keep it in the
  required set.

### 6.4 Seam 4 — Codex bridge
- **Request→prompt golden** (`_extract_prompt_from_responses_request`): recorded codex
  Responses request `SAMPLE_TURN.jsonl` → expected prompt string, asserting `instructions`
  and `tools` both survive (guards the instruction-drop + F-H-7 schema-prose classes).
- **Response→Responses-API golden** (`_provider_response_to_responses_api`): fixture browser
  replies (clean JSON contract, prose-with-fence, bare `DONE`, aliased tool name, repeated
  tool call) → expected Responses payload, asserting `_normalize_tool_calls` snaps names and
  the loop-guard/salvage/nudge branches fire as documented.
- **App-server translation golden**: recorded `codex app-server` notification/request
  `.jsonl` (from the probe) → expected `emit_*` lines (Part 3 of the proposal's own test
  plan). Include an `item/commandExecution/requestApproval` → `command_approval_request`
  round-trip and its `approval_decision` answer.
- **Parity-gap guards**: a test asserting `compact_codex_thread`'s `thread/resume` payload
  includes `modelProvider`/`config` (fails today — F-A-4), and that `_on_token_usage`
  schedules compaction without blocking the pump (F-A-5).

### 6.5 CI wiring
- Add a `golden` job that runs all four suites; a schema edit without regenerated goldens
  fails it (mirrors the existing `gen:events` drift gate at ci.yml:84-95).
- Fixtures are versioned; regenerating them is an explicit `just regen-goldens` step so a
  drift shows up as a reviewed diff, never a silent update.

---

*All four seams + the golden-transcript test plan are documented above. Contract
mismatches are cross-linked to `docs/ops/findings/team-h.md` (F-H-1…9) and the
territory teams' findings.*
