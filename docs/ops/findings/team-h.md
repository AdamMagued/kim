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

---
*(hunt continues — seams 1, 3, 4 census in progress; CONTRACTS.md being written incrementally)*
