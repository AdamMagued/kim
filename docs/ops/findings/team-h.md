# Team H — Wave 1 findings (Contracts & IPC, cross-cutting)

Territory: the four process seams — Frontend⇄Rust (Tauri invoke/events),
Rust⇄Python (stdout tag/JSONL protocol + /v1 HTTP bridge), Python⇄MCP
(stdio JSON-RPC), codex bridge (proxy ⇄ codex ⇄ browser provider).
Read-only hunt. Deliverable: `docs/CONTRACTS.md`. Most severe first.

Status: PRELIMINARY PASS — findings below are confirmed against source;
more to follow as each seam is documented.

---

## F-H-1: Run-lifecycle events (kim-agent-done / -cancelled / -error / kim-run-id) live OUTSIDE the typed schema and carry no run/session envelope — the events that CLEAR the running state are exactly the unattributable ones
- **File:** desktop/src-tauri/src/subprocess.rs:746,760 (kim-agent-done), :1017,1032 (kim-agent-cancelled), :745 (kim-agent-error), :709-712 (kim-run-id); desktop/src/types/events.schema.json (none of these present)
- **Severity:** High
- **Class:** contract | race
- **Evidence:** The RUN-IDENTITY envelope design (events_gen.py:80-89, subprocess.rs `merge_run_envelope`) stamps `run_id`/`session_id` onto every typed `kim:*` event so the frontend can "route/file output by the run it belongs to instead of by whatever view is currently mounted." But the events that terminate a run — `kim-agent-done` (payload: bare bool), `kim-agent-cancelled` (bare bool), `kim-agent-error` (bare string) — are ad-hoc `app.emit` calls with no envelope, no schema entry, and no generated type on either side. A frontend listening across a mid-run session switch cannot attribute the done/cancel to the run that owns it; this is the backend half of Team F's F-F-2 (event bleed) and F-F-5 (spinner-forever, where run-failed is typed but the terminal done is not, so the two halves of "run ended" travel on different, differently-attributed channels). `kim-run-id` does carry both ids but is likewise schema-invisible: nothing forces its shape to stay in sync with the frontend's expectations.
- **Fix sketch:** Add run-lifecycle events to events.schema.json (e.g. `kim:agent-done {success, run_id, session_id}`), emit them through the same envelope path, and deprecate the bare-bool events; frontend keys `isRunning` clearing on the enveloped event.
- **Cross-territory?** yes — Rust emit sites Team D, frontend handling Team F, schema/codegen Team H (this doc + CONTRACTS.md seam 1).

## F-H-2: Codex bridge speaks BOTH protocols on one stream and signals termination ONLY in the legacy text protocol — typed mode never sees run-done/run-failed for Code-tab runs
- **File:** orchestrator/codex_bridge_service.py:512-533,553,597,870-902 (terminal outcomes via `print(f"{LOG_TAG_TASK_COMPLETE}…")` / `LOG_TAG_FAILED` raw lines), :210,400-418 (typed `emit_status` / `emit_hitl_approval_request` on the same stdout); desktop/src-tauri/src/subprocess.rs:289-291 (`else if is_codex` → raw passthrough)
- **Severity:** High
- **Class:** contract
- **Evidence:** In typed IPC mode the Rust forwarder decodes JSON lines into `KimEvent` and re-emits `kim:*`; for codex runs, undecodable lines pass through raw on `kim-agent-output`. codex_bridge_service interleaves typed JSONL (`emit_status`, `emit_hitl_approval_request`) with legacy tag prints (`TASK_COMPLETE:`, `[FAILED]`, `[ERROR]`) on the same stdout. Consequences: (1) the bridge NEVER emits typed `run_done`/`run_failed` — Code-tab termination semantics exist only as magic strings the frontend must regex out of raw lines, while Chat-tab runs get the typed event; the two tabs have divergent termination contracts. (2) The same run emits semantically-overlapping events on two channels (typed kim:status + raw [STATUS] lines emitted by Rust itself at subprocess.rs:866-876 pre-spawn), which is why the frontend needs the 800ms text-window dedup heuristic (Team F F-F-12) — a protocol-level double-emit patched by a UI-level timing hack.
- **Fix sketch:** Bridge emits typed `run_done`/`run_failed` (and `answer`) alongside or instead of the tag prints; Rust pre-spawn status lines use a typed emit; the raw-passthrough branch becomes debug-only.
- **Cross-territory?** yes — bridge emits Team A, Rust pre-spawn emits Team D, doc Team H.

## F-H-3: Typed mode silently swallows all unparseable stdout from CHAT runs (else-branch forwards raw lines only when `is_codex`)
- **File:** desktop/src-tauri/src/subprocess.rs:33-34,289-294 (`forward_agent_stdout_line`)
- **Severity:** Medium
- **Class:** contract | bug
- **Evidence:** `if ipc_typed { if let Ok(event) … } else if is_codex { emit raw } }` — for a chat run (`is_codex == false`) in typed mode (the default), any stdout line that is not valid `KimEvent` JSON is dropped with no log, no counter, no fallback. A stray `print()` in orchestrator code, a third-party library writing to stdout, a truncated/partial JSON line from a killed process, or a typed event whose payload fails serde validation (e.g. `percent` overflowing u32, missing required field from a version-skewed orchestrator) disappears silently. Version skew between a bundled sidecar orchestrator and an updated desktop app makes "typed event fails to decode" a realistic, invisible failure mode: the run appears to produce nothing.
- **Fix sketch:** In the failed-decode path, forward the line on `kim-agent-output` (or a `kim:protocol-error` event) with a rate limit; count decode failures and surface after N.
- **Cross-territory?** yes — fix is Team D; contract statement (what MAY appear on stdout) is Team H.

## F-H-4: MCP tool `inputSchema.required` is never enforced — a missing required arg surfaces as a cryptic `ERROR: 'path'` (KeyError leak), not a typed bad-args error
- **File:** mcp_server/server.py:111-155 (`call_tool` — dispatches `handler(args)` with no schema validation); mcp_server/tools/files.py:20 (`args["path"]`); mcp_server/tool_registry.py:84-157 (schemas declare `"required": [...]`)
- **Severity:** Medium
- **Class:** contract
- **Evidence:** Every tool schema declares `required` fields, but `call_tool` runs policy.enforce then calls the handler directly — nothing validates `arguments` against `inputSchema`. Handlers read required args positionally (`args["path"]`), so a call missing a required field raises `KeyError('path')`, which the generic `except Exception` maps to `ERROR: 'path'`. That string is classified by `classify_tool_output` as `execution_error` (not the planned `bad_args` code, which tool_errors.py:20 explicitly lists as "NOT yet populated"). So the schema's `required` contract is documentation only: the MCP server is a JSON-RPC seam whose declared argument contract is not enforced at the boundary, and the failure mode is an un-actionable one-word error the model cannot distinguish from a real runtime failure. Type coercion is likewise absent — a schema `integer` arrives as whatever JSON type the caller sent.
- **Fix sketch:** Validate `arguments` against the tool's `inputSchema` in `call_tool` before dispatch (jsonschema or a minimal required/type check); return `BAD_ARGS: missing required 'path'` and wire the `bad_args` code in tool_errors.py.
- **Cross-territory?** yes — Team C (server/registry) owns the fix; Team H documents the seam contract; Team A (agent) benefits from the typed error code.

## F-H-5: Two orphaned event channels — Rust emits `kim-agent-started`, `kim-tray-cancel`, `kim-browser-hidden` with NO frontend listener; frontend listens for `kim-ollama-changed` with no Rust emitter
- **File:** desktop/src-tauri/src/http_bridge/tasks.rs:257 (`kim-agent-started` emit), speed_access.rs:88 (`kim-tray-cancel` emit), browser_bridge.rs:137 (`kim-browser-hidden` emit); desktop/src/components/ProviderPicker.tsx:102 (references `kim-ollama-changed`)
- **Severity:** Medium
- **Class:** contract | dead-code
- **Evidence:** Full both-sides event census (see CONTRACTS.md seam 1 table) shows four events that cross the seam only halfway. (1) `kim-agent-started` is emitted by the `/v1/task` HTTP-bridge path so a kimctl-launched run announces itself, but NO desktop listener consumes it — the GUI never learns a bridge-initiated run began (relevant to Team F's session-attribution work: a run started via kimctl is invisible to the app until output arrives). (2) `kim-tray-cancel` (tray "Cancel run" menu) and (3) `kim-browser-hidden` are emitted with zero `listen()` sites in desktop/src — the tray cancel button is wired to an event nobody handles, so the menu item is dead. (4) `kim-ollama-changed` is referenced in the frontend but no Rust `.emit` produces it. These are latent contract rot: an event either side believes is live but the other end dropped.
- **Fix sketch:** Wire `kim-tray-cancel` to `cancel_task` in App.tsx (or remove the menu item); add a `kim-agent-started` listener or delete the emit; confirm `kim-ollama-changed` producer exists (PaneAI/ollama.rs) or remove the consumer. Add a codegen/test that fails when an emitted event has no listener and vice versa.
- **Cross-territory?** yes — emit sites Team D, listeners Team F, the census/test is Team H/K.

## F-H-6: The `[TAG]` text protocol has no written grammar and each parser re-derives it — `[TOOL]` args are re-parsed with a hand-rolled brace matcher on the frontend that diverges from the Python emit shape
- **File:** desktop/src/components/chat/utils.ts:507-534 (`parseLogLine` `[TOOL]` balanced-paren extractor); orchestrator/agent.py:341-379 (`[STATUS] [PLAN]{json}` / `[STEP]{json}` / `[DONE]{json}` emit); orchestrator/events_gen.py:43-57 (`LOG_TAG_*` constants — the closest thing to a spec)
- **Severity:** Medium
- **Class:** contract
- **Evidence:** The legacy tag protocol is emitted from ≥5 Python sites (agent.py, cli.py, codex_bridge_service.py, codex_engine/engine.py, and Rust's own pre-spawn `[STATUS]` lines at subprocess.rs:866-940) and parsed in ≥3 frontend sites (parsers.ts, utils.ts:parseLogLine, buildThinkingTrace) plus the Rust legacy passthrough. There is no single grammar doc: the shape of `[TOOL] module: tool_name({json})` lives only in the emitter's f-string and the frontend's regex `/\[TOOL\]\s+(?:[\w.]+:\s+)?(\w+)\(/` + a 20-line balanced-paren/quote state machine (utils.ts:511-524) that re-implements JSON parsing to survive args with unbalanced characters. `[PLAN]`/`[STEP]`/`[DONE]` are double-wrapped as `[STATUS] [PLAN]{json}` and the frontend must special-case that nesting (parsers.ts:54). Emit and parse are coupled by convention across a language boundary with no schema and no golden fixture — the exact "protocol in two parsers' heads" the charter names. `[CONTEXT]`/`[STATS]` tags exist in the legacyTags list but are NOT emitted as text any more (only typed), so the parser carries dead branches for them.
- **Fix sketch:** This finding is discharged by CONTRACTS.md seam 2 (the grammar is now written down there). Follow-up: generate the tag parser from the same schema that generates events, or delete the legacy text path once dual-emit is retired.
- **Cross-territory?** yes — the grammar doc is Team H (done here); retiring dual-emit is Team A+D+F.

## F-H-7: Codex proxy drops the request-scoped tool `input schema` for tool NAMES only — argument shape is narrated as free text, so `_normalize_tool_calls` must guess the exec-tool arg key
- **File:** codex_engine/engine.py:1100-1132 (`_render_codex_tools` renders schema into prose), :1141-1200 (`_normalize_tool_calls` heuristically snaps names + coerces `command`→`cmd`), :1292-1325 (`_extract_prompt_from_responses_request`)
- **Severity:** Medium
- **Class:** contract
- **Evidence:** The `_CodexProxy` is an OpenAI-Responses/-Chat endpoint that relays to a browser LLM which cannot receive a real `tools` array — so codex's structured tool definitions are flattened into a `[AVAILABLE CODEX TOOLS]` prose block and the model is asked to emit `{"text":…,"tool_calls":[{"name","input"}]}` JSON. The fidelity loss is one-directional and lossy: (a) the model routinely invents tool names/arg keys, requiring `_normalize_tool_calls` to prefix-match names against the request tools and to coerce a `command` arg (string OR argv list) into the `cmd` string codex's exec tool wants — a guess that silently mis-maps any tool whose real arg key is neither `command` nor `cmd`; (b) this is the same class as the already-fixed instruction-drop bug — `instructions` (system prompt) IS forwarded (engine.py:1296-1298), but per-tool JSON schema is rendered best-effort and non-string tool results are `" ".join(str(...))`-flattened (engine.py:1083-1085), so structured `function_call_output` content is lossily stringified before the browser sees it. There is no schema conformance check on the model's emitted tool_call against the declared `parameters`.
- **Fix sketch:** Validate the model's `tool_calls[].input` against the request tool's `parameters` schema before returning to codex; when the exec-tool arg key is neither `cmd` nor `command`, read it from the tool schema instead of the hardcoded pair. Golden-transcript test (CONTRACTS.md seam 4) pins the render+normalize round-trip.
- **Cross-territory?** yes — Team A/G (codex_engine) owns the fix; Team H documents the seam.

## F-H-8: `KimRunEnvelope.session_id`/`run_id` optional-by-design, and the codex-bridge spawn spec never exports `KIM_RUN_ID`/`KIM_SESSION_ID` — so codex-browser runs emit typed events with NO envelope (root cause of F-F-2/F-F-8)
- **File:** desktop/src-tauri/src/task_spec.rs:258-263 (`chat_task_spec` sets KIM_RUN_ID/KIM_SESSION_ID) vs :309-315 (`codex_browser_spec` sets neither); orchestrator/events_gen.py:84-89 (envelope added only when the env vars are set); desktop/src/types/events.gen.ts:42-50 (`KimRunEnvelope` both fields optional)
- **Severity:** Medium (root cause; compounds Team F F-F-2 High and F-F-8 Medium)
- **Class:** contract | race
- **Evidence:** The RUN-IDENTITY envelope only appears when `KIM_RUN_ID`/`KIM_SESSION_ID` are in the child's env. `chat_task_spec` exports both; `codex_browser_spec` exports neither (it sets PYTHONPATH/CODEX_BIN/KIM_TAURI_MODE only), and `codex_direct_spec` runs a non-Kim binary that emits no typed events at all. So every typed event from a Code-tab browser-bridge run (`kim:status`, `kim:answer`, `kim:tool`, `kim:hitl-approval-request` — all emitted by codex_bridge_service via events_gen) travels WITHOUT an envelope. On the frontend `belongsToView(undefined) === true` (Team F F-F-8), so those events route to whatever view is mounted — the documented routing guarantee is void precisely for the one spawn path the charter calls out (the codex/kimctl-bridge path). The type system encodes the hole as `session_id?` rather than the codegen forcing the bridge to stamp it. subprocess.rs:703-708 computes a fallback `run_id` for the `kim-run-id` announce, but that fallback is NOT injected into the child env, so the child's events still carry nothing.
- **Fix sketch:** Have `codex_browser_spec` set `KIM_RUN_ID`/`KIM_SESSION_ID` (identical to `chat_task_spec`) so the bridge's typed events self-stamp; then `session_id` can become required in the schema and `belongsToView` can reject un-owned events. Add a task_spec test asserting both env vars are present on every orchestrator-backed spawn shape.
- **Cross-territory?** yes — env stamping is Team D (task_spec.rs), the required-field flip + guard is Team F, the schema/contract is Team H.

## F-H-9: HTTP-bridge auth boundary is correct but UNDOCUMENTED — the token-gate/exemption contract lives only in an inline comment; the exemption list must be pinned so a future route can't silently slip the gate
- **File:** desktop/src-tauri/src/http_bridge/mod.rs:72-93 (gate: every path except `GET /v1/health` requires `X-Kim-Token`, constant-time compared), :95-100 (`/v1/result/{id}` handled AFTER the gate → correctly authenticated)
- **Severity:** Low
- **Class:** contract | docs
- **Evidence:** Verified NOT a vuln: the token gate at :74 runs before the dynamic `/v1/result/` branch, so result polling IS authenticated (and results.rs re-checks the token). But the entire auth contract of the bridge — "all `/v1/*` require `X-Kim-Token` except `GET /v1/health`" — exists only as a two-line code comment. There is no test asserting the exemption set, so a future route added above the gate (the exact shape of the `/v1/result` early-return) could bypass it unnoticed. This is the contract half of Team D's F-D-3 (health-route reconnaissance): the seam's authN rule should be written down (CONTRACTS.md seam 2) and guarded by a test enumerating gated vs exempt routes.
- **Fix sketch:** Add a test that hits every `/v1/*` route without a token and asserts 401 except `/v1/health`; document the exemption list in CONTRACTS.md (done here).
- **Cross-territory?** yes — Team D owns the bridge; Team H/K own the contract doc + test.

---

## Summary
9 findings: 2 High (F-H-1 run-lifecycle events off-schema/un-enveloped, F-H-2 codex bridge dual-protocol termination), 6 Medium (F-H-3 silent chat-stdout drop, F-H-4 unenforced MCP required-args, F-H-5 orphaned event channels, F-H-6 undocumented tag grammar, F-H-7 codex proxy tool-schema loss, F-H-8 codex spec missing run-identity env), 1 Low (F-H-9 bridge auth-contract undocumented). Cross-territory roots for Team F's F-F-2/F-F-5/F-F-8/F-F-9 and inherited F-INH-6 are pinned here. Deliverable `docs/CONTRACTS.md` documents all four seams + the golden-transcript test plan (finishes V-3).

### 3 scariest contract mismatches
1. **F-H-2 / F-H-8** — the Code-tab (codex browser-bridge) path is a second-class citizen on TWO seams at once: it never emits typed `run_done`/`run_failed` (termination is magic strings) AND its typed events carry no run-identity envelope (they route to the wrong view on a session switch). The tab most likely to run long, unattended, background work has the weakest run-attribution contract.
2. **F-H-1** — the events that CLEAR the "running" state (`kim-agent-done`/`-cancelled`) are the only lifecycle events NOT in the typed schema and NOT enveloped; combined with the frontend having no watchdog (F-F-5), a backend that dies after `run-failed` but before the bare-bool `done` strands the UI forever. The terminal signal is the least-typed signal.
3. **F-H-4** — the MCP JSON-RPC seam advertises an argument contract (`required`, types) it never enforces; a malformed call from the agent returns a one-word `ERROR: 'path'` indistinguishable from a real runtime failure, and the `bad_args` error code the system was designed around is still unpopulated.
