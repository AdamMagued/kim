# Codex App-Server Parity — Kim Code Mode = Full Codex, Routed to Browser

> **Status:** accepted — implemented code-complete as ROADMAP_TO_10 Phase 3 (2026-07-06, see docs/ROADMAP_PROGRESS.md "Phase 3 — App-server parity"); default transport flip + live GUI/CLI smoke still outstanding — 2026-07-13

**Verified against:** codex-cli 0.134.0 (local binary), branch `feat/browser-stateful-threads`
**Goal:** Kim's Code mode (CLI + Tauri code tab) behaves *exactly* like Codex — per-command
approvals ("allow / allow always / deny"), workspace-write sandbox with escalation, live
command output, plan + diff streaming, interrupts, session resume — with the model calls
routed to a browser LLM (Gemini/ChatGPT web) through the existing `_CodexProxy`.

---

## 0. Why this is feasible (verified evidence, not speculation)

All of the following was **proven live** against the installed `codex` binary on 2026-07-06
(probe artifacts: `docs/APPENDIX_appserver_probe_findings.md`; schema bundle regenerable via
`codex app-server generate-json-schema --out <dir>`):

1. `codex app-server` speaks newline-delimited JSON-RPC 2.0 over stdio. `initialize`
   handshake answered instantly.
2. `thread/start` accepted, in one call: `modelProvider: "kim-proxy"` + **inline dotted
   `config` overrides** (`model_providers.kim-proxy.base_url`, `wire_api = "responses"`),
   `approvalPolicy: "on-request"`, `sandbox: "workspace-write"`, `cwd`, `ephemeral`.
   → No temp `CODEX_HOME` needed on this path; the user's real `~/.codex` config
   (MCP servers, skills) applies automatically.
3. Threads persist with `thread.id == sessionId` (uuid-v7) → `thread/resume` gives true
   cross-process continuity. Today's exec path spawns a fresh Codex per message that
   forgets its own tool outputs; this fixes that structurally.
4. The protocol natively provides every UX piece we want:
   - Server→client approval **requests**: `item/commandExecution/requestApproval`,
     `item/fileChange/requestApproval` (v2) and `execCommandApproval` / `applyPatchApproval` (v1)
   - Decisions include `accept`, `acceptForSession` (= "always allow"), decline variants,
     and `acceptWithExecpolicyAmendment` (persistent allow-rules)
   - Streaming: `item/agentMessage/delta`, `item/commandExecution/outputDelta`,
     `item/reasoning/textDelta`, `turn/plan/updated`, `turn/diff/updated`,
     `thread/tokenUsage/updated`
   - Control: `turn/interrupt` (Esc-to-cancel), `turn/steer` (mid-turn user injection),
     `thread/compact/start` + `thread/compacted` (native codex-side compaction)
5. `workspace-write` defaults to `networkAccess: false` → network commands trigger an
   approval request with `networkApprovalContext`. This is exactly the "Codex can install
   Playwright itself once you say yes" behavior.

**Residual risks (honest list):**
- `app-server` is flagged *experimental*; message shapes may drift across codex releases.
  Mitigation: Part 0 pins a version gate + snapshots the schema bundle in-repo; the client
  is written against the snapshot with tolerant parsing (ignore unknown fields/methods).
- v1 vs v2 approval method selection depends on `initialize` capability negotiation —
  Part 1 includes a mandatory live probe (P2) to record which arrives; client handles both.
- Browser-model tool-call fidelity (prose instead of clean tool calls, no true token
  streaming) is unchanged by this migration — Part 7 hardens the proxy. Plumbing parity
  and model fidelity are orthogonal; this plan delivers the former completely.

**Architectural keystone that makes this executable without rearchitecting Kim:**
Keep the **spawn-per-message process model**. Both Tauri (`subprocess.rs:574`) and the CLI
(`provider.rs:975`) spawn `codex_bridge_service` per user message today, with SIGTERM
cancel and stdin-line HITL plumbing already working. We keep that: each message spawns the
service, which spawns `codex app-server`, calls `thread/resume(saved_id)` (or
`thread/start` on first message), runs exactly one `turn/start`, streams events, saves the
thread id to the sidecar, and exits. A persistent daemon is a *later optimization*
(Part 7), not a prerequisite. Approvals happen mid-turn, i.e. within one service process —
the existing stdin channel serves them.

---

## Part 0 — Groundwork (small; unblocks everything)

**Objective:** feature flag, version gate, in-repo protocol snapshot, probe tooling.

1. **Config flag** — `config.yaml.example`: under a new `codex_bridge:` section add
   `transport: exec | app-server` (default `exec` until Part 8 flips it), plus
   `sandbox_mode: workspace-write` and `approval_policy: on-request` defaults for the
   app-server path. Read in `codex_bridge_service._load_config()` (service:111-123).
2. **Schema snapshot** — commit `codex_engine/appserver_schema/` generated via
   `codex app-server generate-json-schema --out …` (39 files, small). This is the contract
   the client is coded against; regenerating it on a codex upgrade shows the diff.
3. **Version gate** — at service startup on the app-server path, run `codex --version`;
   parse `codex-cli X.Y.Z`; warn (status event) if minor version differs from the pinned
   one in `codex_engine/appserver_schema/VERSION`; refuse only on major drift.
4. **Probe script** — `scripts/probe_appserver.py`: standalone re-runnable script doing
   initialize + thread/start + (optional, `--turn`) a trivial `turn/start` with approvals
   auto-declined, dumping every JSON-RPC line. Used in Part 1's P2 probe and for future
   codex-upgrade smoke checks.

**Acceptance:** flag parses; probe script runs green against local binary; schema committed.
**Size:** ~half a day for an agent.

---

## Part 1 — Python protocol client: `codex_engine/app_server.py`

**Objective:** a self-contained, unit-testable JSON-RPC client for `codex app-server`.

**New module `codex_engine/app_server.py`** (~350 lines):

```python
class AppServerClient:
    async def start(binary, env, config_overrides) -> None   # spawn `codex app-server`
    async def initialize(client_info) -> dict
    async def request(method, params, timeout) -> dict        # id-correlated
    def notify(method, params) -> None
    async def events() -> AsyncIterator[Incoming]             # notifications + server requests
    async def respond(request_id, result) -> None             # answer a server request
    async def stop() -> None                                   # graceful; SIGKILL fallback
```

Implementation notes (all patterns already exist in the codebase — copy them):
- Spawn via `asyncio.create_subprocess_exec` with piped stdio, like engine.py:172-207.
  Env: minimal env dict pattern from service:392-403, **plus `CODEX_API_KEY=<bearer>`**
  (the proxy's `env_key` — engine.py:527 generates the token; the app-server child must
  have it at spawn time since `thread/start` config sets `env_key = "CODEX_API_KEY"`).
- Framing: one JSON object per line, both directions (verified live).
- Correlation: `dict[int, asyncio.Future]` keyed by request id; monotonic id counter.
- **Incoming taxonomy** (the part that matters):
  - has `id` + `method` → **server request** (approval etc.) — MUST be answered or codex
    hangs the turn. Yield to consumer; track outstanding ids; on shutdown/timeout,
    auto-respond with the safest decline.
  - has `method` only → notification. Yield.
  - has `id` only → response to us. Resolve future.
- Tolerant parsing: unknown methods → yield as `Unknown(method, params)`; never crash.
- stderr → drain into a ring buffer surfaced on abnormal exit (pattern: engine.py:1258).

**Tests — `tests/test_app_server_client.py`** (~15 tests): drive the client against a
**fake app-server** (a tiny Python subprocess script in the test file that speaks the
protocol from stdin/stdout): handshake, request/response correlation, out-of-order
responses, server-request round trip, unknown-notification tolerance, dead-process error,
shutdown auto-decline of outstanding approvals.

**Mandatory probe P2 (do during this part, record results in the module docstring):**
run `scripts/probe_appserver.py --turn` in a scratch dir with a prompt like
"create a file named x.txt" against a *real* model (or the kim-proxy with a canned
response) and record: which approval method arrives (v1 `execCommandApproval` vs v2
`item/commandExecution/requestApproval`), and the exact `item/*` notification sequence of
a simple turn. Wire `InitializeParams.capabilities` accordingly. Handle **both** shapes in
the dispatcher regardless.

**Acceptance:** all unit tests green; probe transcript committed under
`codex_engine/appserver_schema/SAMPLE_TURN.jsonl` (redacted).
**Size:** 1–2 days.

---

## Part 2 — Service transport: app-server path in `codex_bridge_service.py`

**Objective:** the bridge service gains a second engine path; behavior identical from the
outside (same stdout event stream Tauri/CLI already parse), plus true session continuity.

1. **Sidecar gains codex thread id** — `codex_engine/thread_state.py` (fields at
   docstring 9-16): add `codex_thread_id: str|None`. Same key derivation
   (`sha256(cwd|provider)[:16]`, thread_state.py:34-36). Bump nothing else; existing
   sidecars remain valid (missing key = None).
2. **New flow in `_run_async`** (service:282-493), branched on `transport == "app-server"`:
   - Start `_CodexProxy` exactly as today (service:375-382) — **unchanged class**, two
     adjustments below.
   - `AppServerClient.start()` with `CODEX_API_KEY=proxy._bearer_token`; `initialize`.
   - `thread/resume(codex_thread_id)` if sidecar has one and cwd matches; on error
     (deleted/foreign thread) fall back to `thread/start`. `thread/start` params:
     `cwd`, `model="kim-browser"`, `modelProvider="kim-proxy"`, inline `config`
     (`model_providers.kim-proxy.{name,base_url,wire_api,env_key}` — mirror
     `_write_codex_config` engine.py:293-303), `approvalPolicy` + `sandbox` from config
     flag (default on-request + workspace-write), `developerInstructions` if we carry any.
     Save returned thread id to sidecar immediately.
   - `turn/start(threadId, input=[{type:"text", text:task}])`.
   - **Event pump** (the core loop): consume `client.events()` until `turn/completed`:
     translate to Kim events (Part 3 table) and print to stdout via `events_gen`.
   - **Approval bridging:** on a server approval request, emit the new
     `command_approval_request` event (Part 3) and block on the **existing stdin decision
     channel** (generalize `_await_hitl_decision`, service:170-189, to parse
     `{"type":"approval_decision","id":…,"decision":"accept"|"acceptForSession"|"decline"}`
     while remaining backward-compatible with `{"approved":bool}`); forward the decision
     via `client.respond()`. Timeout (config, default 120s) → decline. Non-interactive
     env (`KIM_TAURI_MODE` unset AND stdin not a TTY pipe) → auto-decline with a status
     event explaining why.
   - **Cancellation:** extend `_install_sigterm_handler` (service:94-105): on SIGTERM send
     `turn/interrupt`, give it 3s to emit `turn/completed`, then stop client. Kim's
     existing kill path still works as the hard fallback.
   - `finally`: save sidecar (id + turns/est_tokens), stop client, stop proxy
     (mirror service:488-493).
3. **`_CodexProxy` adjustments** (engine.py:495-942) — both small:
   - `_relay_count` guard (engine.py:579-586) becomes per-*turn*: add
     `proxy.begin_turn()` called before each `turn/start` (resets count). MAX_RELAYS
     semantics otherwise unchanged.
   - Nothing else changes: stateful browser-thread logic, compaction, handoff seeding,
     keepalive short-circuit all stay byte-identical — the proxy neither knows nor cares
     which transport spawned codex.
4. **Codex-side compaction:** when the pump sees `thread/tokenUsage/updated` exceeding a
   configured codex-transcript budget, fire `thread/compact/start` (native) — Kim's
   `/compact` control task (service:298-299) triggers BOTH: browser-thread compaction
   (existing `_compact_browser_thread`, service:223-250) and `thread/compact/start`.
   Two independent context budgets, both handled.
5. **Git-repo gate:** unchanged (service:316-339). `--skip-git-repo-check` has no
   app-server equivalent flag; instead pass `config` override
   `skip_git_repo_check` if supported (probe during P2) or simply keep the gate as
   Kim-side UX (the confirm prompt) and start the thread regardless — workspace-write
   sandbox makes non-git dirs safe enough once the user confirmed.

**Tests — extend `tests/test_codex_stateful_threads.py` + new
`tests/test_appserver_bridge.py`:** flow tests with a fake AppServerClient (inject via
constructor param): resume-or-start decision, sidecar id persistence, approval
decision round trip incl. timeout→decline, SIGTERM→interrupt, turn event translation,
proxy `begin_turn` reset. Target ~25 tests.

**Acceptance:** with `transport: app-server` in config, CLI code mode completes a real
"create pong.html and open it" task end-to-end with one approval prompt (manual
verification), and `rg thread_id kim_sessions/codex_threads/*.json` shows a persisted id
reused on the next message. Legacy `exec` path untouched and green.
**Size:** 2–3 days. **This is the keystone part.**

---

## Part 3 — Kim event schema: typed events for the new UX

**Objective:** one shared event vocabulary from Python → (Tauri Rust | CLI Rust) → UIs.

`orchestrator/events_gen.py` is **generated** — locate the generator/schema first
(`rg -l "events_gen|GENERATED" scripts/ tools/ *.json *.py` — likely an events schema
JSON + a small generator; DO NOT hand-edit the generated file; if no generator is found,
document that and edit consistently with the header comment).

New event types (emitters + fields):

| event type | fields | source protocol msg |
|---|---|---|
| `command_approval_request` | `id, command, cwd, reason, risk, proposed_amendment?, network_context?` | `item/commandExecution/requestApproval` (+v1) |
| `file_change_approval_request` | `id, files:[{path,kind}], reason` | `item/fileChange/requestApproval` (+v1) |
| `command_output` | `item_id, chunk` | `item/commandExecution/outputDelta` |
| `assistant_delta` | `chunk` | `item/agentMessage/delta` |
| `reasoning_delta` | `chunk` | `item/reasoning/textDelta` (scrub provider names — reuse `_surface_relay_reasoning` engine.py:1309) |
| `plan_update` | `steps:[{text,status}]` | `turn/plan/updated` |
| `diff_update` | `unified_diff` | `turn/diff/updated` |
| `token_usage` | `input, output, total` | `thread/tokenUsage/updated` |
| `item_lifecycle` | `item_id, kind, phase(started/completed), title` | `item/started` / `item/completed` |
| `turn_lifecycle` | `phase(started/completed/interrupted), turn_id` | `turn/started` / `turn/completed` |

Stdin decision line (client → service): `{"type":"approval_decision","id":…, "decision":…}`.
Final answer keeps the existing `emit_answer` / `TASK_COMPLETE:` contract (service:267) so
old consumers don't break.

**Tests:** golden-file translation tests: recorded `SAMPLE_TURN.jsonl` (Part 1) in →
expected Kim-event lines out.
**Acceptance:** pyright green (events_gen is in the include set); translation goldens pass.
**Size:** 1 day.

---

## Part 4 — CLI (Rust): the Claude-style interactive experience

**Objective:** `kim` code mode looks and feels like Codex TUI: approval prompts, live
command output, plan checklist, Esc interrupt.

1. **Pipe stdin to the service** — the CLI browser branch (provider.rs:975-998) does NOT
   pipe stdin today. Add `.stdin(Stdio::piped())`, hold the handle, and add a
   `mpsc::Sender<String>` "decision channel" threaded from main.rs into
   `stream_codex_subprocess` (alongside the existing event `tx`).
2. **Extend `AppEvent`** (provider.rs:22-29): add
   `ApprovalRequest { id, command, cwd, reason, risk }`,
   `CommandOutput(String)`, `PlanUpdate(Vec<(String,String)>)`, `DiffUpdate(String)`,
   `TokenUsage{input,output}`, `TurnPhase(String)`.
3. **Parse new typed events** in `process_codex_line` (provider.rs:1107-1146): the typed
   JSON events from Part 3 map 1:1 onto the new AppEvent variants; unknown types remain
   dropped (existing behavior, provider.rs:1238-1252).
4. **Approval prompt UI** in `consume_turn_events` (main.rs:1182-1330): on
   `ApprovalRequest`: `clear_spinner_line()` (main.rs:1175), render

   ```
   Codex wants to run:
     $ npx playwright install     (in ~/Desktop/test)
     reason: needs browsers for testing   [network access]
   Allow? [y]es once / [a]lways this session / [n]o:
   ```

   read one line from stdin (the pattern exists: `confirm_run_outside_git_repo`,
   main.rs:1074-1086), map y→`accept`, a→`acceptForSession`, n/other→`decline`, send
   `{"type":"approval_decision",…}` down the decision channel → child stdin. Resume
   spinner. NOTE: the REPL reads stdin between turns only, so mid-turn reads don't race —
   verify and document; if the REPL uses raw-mode/readline, gate with the same mechanism
   `confirm_run_outside_git_repo` already uses.
5. **Live rendering:** `CommandOutput` → dim streamed lines (cap at N lines with
   `… (+k more)`); `PlanUpdate` → re-render compact checklist (`✓ / ▸ / ○`);
   `DiffUpdate` → one summary line (`diff: 2 files, +45 −3`), full diff on `/diff`;
   `assistant_delta` → stream into the existing "Kim: " text flow (main.rs:1252-1265).
6. **Esc/Ctrl-C = interrupt, not kill:** the existing cancel select-arm (main.rs:1160-1163)
   currently drops/kills the child. Change order: first close via SIGTERM (service now
   maps SIGTERM → `turn/interrupt`, Part 2), print `⏹ interrupting…`, escalate to kill
   after 5s.
7. `cargo fmt --check` + unit tests for arg/event mapping (pattern: provider.rs:2086-2145).

**Acceptance (manual script):** in a scratch dir, `kim` → `/code` → browser provider →
"make pong.html and boot it up" → see plan, see file-change approval, approve, see
`open pong.html` command approval, approve → game opens. Then "also add sound" →
thread resumed (no re-onboarding). Esc mid-turn stops cleanly.
**Size:** 2–3 days.

---

## Part 5 — Tauri code tab: same UX in the app

**Objective:** desktop parity with Part 4, reusing the existing HITL wiring end-to-end.

The whole round trip already exists for the coarse task-level gate — extend, don't invent:
1. **Rust `subprocess.rs`:** the event reader (110-155) forwards typed events. Add the
   Part 3 event types → `app.emit("kim:approval-request", …)`, `kim:command-output`,
   `kim:plan-update`, `kim:diff-update`, `kim:token-usage` (mirror the
   `HitlApprovalRequest` pattern at 110-120).
2. **Decision command:** extend `hitl_respond_approval` (subprocess.rs:160-169) or add
   `respond_approval_decision(id, decision)` writing the Part 3 stdin line. Keep the old
   command for the legacy gate.
3. **React:**
   - `useChatStream.ts` (476-496): listeners for the new events; state slices for
     pending approval, plan, streaming output, diff.
   - `StreamRenderer.tsx` (335-378): generalize the existing HITL card into an
     `ApprovalCard` with three buttons (Allow once / Always this session / Deny), command
     + cwd + reason + network badge.
   - New lightweight components: `PlanChecklist`, collapsible `CommandOutputBlock`
     (streaming, auto-scroll), `DiffSummary` (expandable).
   - `useTaskRunner.ts:147-159`: unchanged (same `send_task`), plus a Stop button that
     invokes the existing cancel (which now interrupts gracefully via Part 2).
4. **Permission-mode mapping:** `permission_mode` → `KIM_HITL_RISK_THRESHOLD`
   (subprocess.rs:595-604) maps onto `approvalPolicy`: "always ask" → `on-request`;
   "auto" → `never` + workspace-write (no bypass flag needed anymore — this **retires
   `KIM_CODEX_BYPASS_SANDBOX`** on the app-server path).

**Tests:** Rust event-mapping unit tests; React component tests if the repo has a JS test
runner (check `desktop/package.json`; if none, manual QA checklist in the PR).
**Acceptance:** same pong scenario as Part 4 but in the app: approval cards render,
buttons resolve the turn, plan/output/diff visible, Stop interrupts.
**Size:** 2–3 days.

---

## Part 6 — Parity extras (each independent, small)

1. **web_search:** config flag `codex_bridge.web_search: true` → inline config override
   `tools.web_search = true` at thread/start. (Search executes inside codex, not the
   browser model — works regardless of provider.)
2. **User config passthrough:** already free on the app-server path (no CODEX_HOME
   override) — user's MCP servers + skills apply. Add a status line listing active MCP
   servers at thread start (`mcpServerStatus/list`).
3. **Images:** `turn/start` input supports image items — wire Tauri attachment UI and CLI
   `/image <path>` into `input:[{type:"image",…}]` (check exact UserInput image variant in
   the schema snapshot).
4. **/review:** map a `/review` control task → `review/start`.
5. **/resume & /threads:** CLI + app commands listing `thread/list` for the cwd and
   switching the sidecar's `codex_thread_id`.
6. **Steer:** typing while a turn runs → `turn/steer` (CLI: line buffered during turn;
   Tauri: input box stays enabled) — optional, ship last.

**Size:** 2–3 days total, parallelizable.

---## Part 7 — Hardening & optimization (ongoing)

1. **Proxy tool-call repair:** when the browser model returns prose where the Responses
   API expects a tool call, add repair heuristics in `_handle_responses`
   (engine.py:572-724): re-ask once with a terse "reply ONLY with the tool call JSON"
   nudge; strip markdown fences; tolerate single-quote JSON. Metrics: count repairs in
   thread_state for visibility.
2. **Persistent app-server daemon (optional):** one `codex app-server` per Kim session
   instead of per message (saves ~1s spawn + thread/resume replay). Only after Parts 2-5
   are stable; requires ownership/cleanup management in Tauri (`subprocess.rs`) and CLI.
3. **Pseudo-streaming from browser:** `BrowserProvider.complete()` returns one blob; the
   proxy can chunk it into SSE deltas so codex→`item/agentMessage/delta` feels live.
4. **Version-drift CI check:** a test (skipped when `codex` absent) that regenerates the
   schema and diffs against the snapshot, failing with a readable message on drift.

---

## Part 8 — Rollout

1. Flip default `codex_bridge.transport` to `app-server` after one week of local use.
2. Keep `exec` path for one release as fallback (`transport: exec`), then delete:
   `build_codex_args` exec bits, `KIM_CODEX_BYPASS_SANDBOX`, `_write_codex_config`
   temp-dir machinery (the ollama CLI branch, provider.rs:1007-1058, migrates to
   app-server too or stays exec — decide then).
3. Docs: update `HOW_TO.md` + `ARCHITECTURE.md` (bridge diagram), `config.yaml.example`.
4. Memory/PR hygiene: PR per part, all suites green per part
   (pytest full run, `cargo test -p kim-cli`, `cargo fmt --check`, flake8, pyright).

---

## Execution order & sizing summary

| Part | What | Depends on | Size |
|---|---|---|---|
| 0 | flags, schema snapshot, probe script | — | 0.5d |
| 1 | `AppServerClient` + fake-server tests + P2 probe | 0 | 1–2d |
| 2 | service app-server path, sidecar id, approvals over stdin | 1 | 2–3d |
| 3 | typed Kim events (generator) | 2 (co-develop) | 1d |
| 4 | CLI approval UX + streaming + interrupt | 2,3 | 2–3d |
| 5 | Tauri approval cards + plan/output/diff UI | 2,3 | 2–3d |
| 6 | web_search, images, /review, /resume, steer | 2 | 2–3d |
| 7 | proxy repair, daemon, pseudo-streaming | 4,5 | ongoing |
| 8 | default flip, exec removal, docs | all | 1d |

**Total to full parity UX (Parts 0–5): roughly 8–12 agent-days.** Parts 4 and 5 are
parallelizable; Part 6 items are independent.

## Invariants (do not break while executing)

- `_CodexProxy` browser-thread semantics (stateful deltas, handoff seeding, compaction,
  keepalive) are transport-agnostic and must remain byte-identical — the full
  `tests/test_codex_stateful_threads.py` + `tests/test_stateful_browser_threads.py`
  suites stay green untouched.
- The legacy exec path keeps working until Part 8.
- Outside-facing contracts preserved: `TASK_COMPLETE:` final line, `emit_answer`,
  legacy `{"approved":bool}` stdin line, `kim:hitl-approval-request` Tauri event.
- Never write Kim state into the user's project dir (sidecars stay in
  `kim_sessions/codex_threads/`).
- CI gates: pyright (providers/mcp_server/codex_engine), flake8 (orchestrator/, 120 cols),
  `cargo fmt --check` for the CLI.
