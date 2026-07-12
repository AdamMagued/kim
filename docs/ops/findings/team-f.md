# Team F — Frontend (React/TS/CSS) — Wave 1 findings

**Territory:** `desktop/src/` (components, hooks, types, styles). Read-only hunt on
`integration/audit-fixes`. Format per OPERATION_GOOGLE_LEVEL.md §3. Most severe first
within each batch; batches are append-only so numbering is stable across commits.

---

## F-F-1: No ESLint at all — no config, no dependency, no CI job; CLAUDE.md claims it exists
- **File:** desktop/package.json (no eslint dep/script); no eslint.config.* / .eslintrc* anywhere; .github/workflows/* never runs eslint; desktop/src/CLAUDE.md:23 ("**No new `any`**: ESLint warns on new `any` types in this directory")
- **Severity:** High
- **Class:** test-gap | docs
- **Evidence:** `ls .eslintrc* eslint.config.*` → no matches; `grep eslint desktop/package.json` → nothing; CI workflows contain no eslint step. Yet the master plan's exit criterion G3 requires "eslint errors=0" and desktop/src/CLAUDE.md tells contributors ESLint enforces the no-new-`any` rule — it cannot, it doesn't exist. Every React-hooks bug class Team F hunts below (missing deps, listener leaks) is exactly what `eslint-plugin-react-hooks` catches mechanically; 20k LOC of TS/TSX have never been linted.
- **Fix sketch:** Add `eslint` + `typescript-eslint` (recommended-type-checked) + `eslint-plugin-react-hooks` flat config; wire `npm run lint` into ci.yml; fix or allowlist the initial burn-down. Correct CLAUDE.md meanwhile.
- **Cross-territory?** yes for the CI wiring (Team K owns workflows); config + burn-down is Team F.

## F-F-2: Session-envelope route guard treats missing `session_id` as "belongs to this view" — legacy/codex stream events bleed across a mid-run session switch
- **File:** desktop/src/hooks/useChatStream.ts:214-217 (`belongsToView`), :778-784 (`kim-agent-output`/`kim-agent-error` raw listeners have NO guard at all)
- **Severity:** High
- **Class:** race | bug
- **Evidence:** `belongsToView(sid)` returns true when `sid === undefined || sid === null` — by design for "legacy/codex/bridge streams" that carry no session envelope. But those are exactly the streams that keep flowing after the user switches session mid-run: every un-enveloped typed event and ALL raw `kim-agent-output`/`kim-agent-error` lines are appended to whatever view is currently on screen. Concrete trigger: start a Code-tab codex run in session A, switch to session B (or New Chat) while it streams → B's activity feed fills with A's reasoning/shell lines and `[err]` lines, and an error line sets B's `taskError` + `lastFailedTask` (appendRaw 'error' branch, :390-399) offering "Retry" of a task B never ran. The run-identity work (D1/B4/B5) fixed history filing and the done/cancel signals but not the per-line stream routing.
- **Fix sketch:** Rust already knows the owning session at spawn — stamp `session_id` onto the raw output/error events (or wrap them in the typed envelope) and drop un-enveloped lines when `runOwnerSessionIdRef` ≠ view session; short-term, gate `appendRaw` behind `ownsActiveRun`.
- **Cross-territory?** yes — event stamping is Team D (Rust emits `kim-agent-output`); frontend gating is Team F.

## F-F-3: Elapsed timer and run duration reset to zero when a view remounts mid-run (switch-back)
- **File:** desktop/src/hooks/useChatStream.ts:339-347 (timer effect), :823-824 (durationSec from startTimeRef)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** The hook deliberately supports remount-mid-run: `isRunning` initializes to `ownsActiveRun` (:113). The timer effect runs on mount with `isRunning === true` and unconditionally does `startTimeRef.current = Date.now(); setElapsed(0)`. Trigger: start a task, switch to another session, switch back → the elapsed pill restarts from 0:00, and when the run finishes `durationSec = now - startTimeRef.current` records only the time since switch-back, so the persisted run history (`save_run_history`, :842) and the "worked for" pill under-report duration.
- **Fix sketch:** Persist run start time with the active-run identity at App level (it already tracks `activeRunSessionId`/`activeRunId`) and initialize `startTimeRef` from it on re-attach instead of `Date.now()`.
- **Cross-territory?** no

## F-F-4: Second `[PLAN]` mid-run silently resets progress; `[STEP]`/`[DONE]` indices unvalidated against steps length
- **File:** desktop/src/hooks/useChatStream.ts:508-536
- **Severity:** Low
- **Class:** bug | contract
- **Evidence:** A re-plan (agent emits a new PLAN after completing steps) replaces `typedLivePlan` with `doneSteps: []`, wiping checkmarks the user watched accrue — plausible but arguably intended. Sharper: a PLAN with <2 valid steps is dropped (`if (steps.length < 2) return`) but the PREVIOUS plan card stays up, so subsequent STEP/DONE events for the new plan mutate the stale card's steps (index mismatch: `Math.min(e.payload.n, prev.steps.length)` clamps rather than rejects, and DONE pushes any `n` into `doneSteps` unbounded). Steps are also truncated to 12 while STEP/DONE indices from the backend can exceed 12 — clamped to `steps.length`, marking the wrong step active.
- **Fix sketch:** On any PLAN event (even <2 steps) clear or replace the live plan; ignore STEP/DONE with `n > steps.length`; document the 12-step truncation in the protocol or remove it.
- **Cross-territory?** protocol doc side pairs with Team H.

## F-F-5: Spinner-forever + suppressed failure banner when a run ends without `kim-agent-done`
- **File:** desktop/src/hooks/useChatStream.ts:615-618 (RUN_FAILED handler), :816/:915 (only two `setIsRunning(false)` inside listeners); desktop/src/components/chat/StreamRenderer.tsx:440 (`if (!runFailure || isRunning) return null`)
- **Severity:** High
- **Class:** bug | contract
- **Evidence:** `isRunning` is cleared in exactly three places: the `kim-agent-done` handler, the `kim-agent-cancelled` handler, and the `send_task` invoke() rejection in useTaskRunner (:228). There is NO frontend watchdog/timeout. The typed `kim:run-failed` handler only calls `setRunFailure` — it does NOT clear `isRunning`. So if the backend emits `kim:run-failed` (or dies) but never emits the global `kim-agent-done` (subprocess killed, HTTP-bridge crash, panic before the done signal), the view is stuck: the "thinking…" spinner + Stop button stay forever, AND the recovery banner is actively hidden because StreamRenderer returns null for the failure card while `isRunning` is true. The user's only escape is Stop (if cancel still works) or reload. Concrete trigger: kill the python orchestrator mid-run, or any code path that emits run-failed without the terminal done.
- **Fix sketch:** Have the RUN_FAILED handler (and a client-side inactivity watchdog, e.g. no event for N s) clear `isRunning`; treat `kim:run-failed` as terminal. Guarantee `kim-agent-done` always follows a failure (Team D/H contract).
- **Cross-territory?** yes — the "done must always fire" guarantee is Team D/H; the frontend watchdog + terminal-run-failed handling is Team F.

## F-F-6: `index.css` @imports Inter from Google Fonts CDN — a network fetch on every launch of a local desktop app
- **File:** desktop/src/index.css:1 (`@import url('https://fonts.googleapis.com/css2?family=Inter…')`)
- **Severity:** Medium
- **Class:** perf | security
- **Evidence:** Kim is a local Tauri app, but the very first CSS line makes a render-blocking cross-origin request to `fonts.googleapis.com` (which in turn pulls font files from `fonts.gstatic.com`) on every window open. Offline (a documented common state for this app) → the request fails and the UI falls back to system fonts, causing a visible FOUT and a layout that was never designed/tested against the fallback. It also leaks the user's IP + a load signal to Google on every launch (privacy), and if a strict Tauri CSP is ever added (Team D/I hardening) this import silently breaks all typography. `@import` at the top of the sheet is also the slowest possible way to load a font (blocks all subsequent CSS).
- **Fix sketch:** Self-host the Inter woff2 subset under `public/` (or `@font-face` with a bundled asset) and drop the remote `@import`; the app already ships assets locally.
- **Cross-territory?** partial — CSP policy is Team D/I; the import itself is Team F.

## F-F-7: Hard-coded provider price table is stale (marked "2025-Q2") — cost chip shows wrong USD
- **File:** desktop/src/components/chat/utils.ts:938-964 (`PRICE_PER_1M`, comment "Last refreshed: 2025-Q2")
- **Severity:** Low
- **Class:** bug | docs
- **Evidence:** The per-1M token USD rates are hardcoded (`claude 3/15`, `openai 2.50/10`, `gemini 1.25/5`, `deepseek 0.27/1.10`) with a self-admitted "2025-Q2" refresh date and model IDs (gpt-4o, gemini-1.5-pro, claude-sonnet-4.x) that no longer match what the app actually calls in mid-2026. The cost chip therefore reports figures that drift from reality with no in-UI caveat that they are estimates. This is exactly the "fabricated cost figures" the `estimateCostUsd` null-guard was written to avoid, but for known providers the stale number is shown confidently.
- **Fix sketch:** Move rates to config the backend already knows (or fetch), add a visible "≈ est." qualifier, and add a test asserting the model IDs in comments match the providers actually dispatched.
- **Cross-territory?** no

## F-F-8: Optional `session_id` in the run envelope defeats the route guard for ALL typed bridge/legacy events (not just raw lines)
- **File:** desktop/src/types/events.gen.ts:42-50 (`KimRunEnvelope` — both fields optional "so events from legacy/bridge streams that predate the envelope still type-check"); desktop/src/hooks/useChatStream.ts:214-217 (`belongsToView` returns true for null/undefined)
- **Severity:** Medium (compounds F-F-2)
- **Class:** contract | race
- **Evidence:** The generated envelope's own docstring states the design intent: "The frontend routes and files run output by these fields, never by which view happens to be mounted." But `session_id` is optional by design for bridge/legacy streams, and `belongsToView(undefined) === true` routes exactly those un-enveloped typed events (kim:status, kim:answer, kim:tool, kim:activity, …) to whatever view is currently mounted. So the codex/kimctl-bridge path — the one that emits typed events WITHOUT a session envelope — violates the stated routing guarantee on a mid-run session switch: its status/answer/tool events land in the wrong view. The type system actively encodes the hole (`session_id?`) rather than catching it.
- **Fix sketch:** Make the bridge stamp the envelope (Team H contract) so `session_id` can become required; until then, route un-enveloped typed events only when `runOwnerSessionIdRef.current === viewSessionId`, not unconditionally.
- **Cross-territory?** yes — envelope emission is Team D/H; the guard is Team F.

## F-F-9: `ToolResultBlock` type omits the `output` field the runtime actually sends — worked around by `as unknown as {output}` casts in three places
- **File:** desktop/src/types/index.ts:51-55 (`ToolResultBlock` has only `content`); desktop/src/components/chat/utils.ts:161-162, :620 (`(trb as unknown as { output?: string }).output`)
- **Severity:** Low
- **Class:** contract | dead-code
- **Evidence:** The declared `ToolResultBlock` is `{ type, tool_use_id, content }`, but `extractTouchedFiles` and `synthesizeActivityFromMessages` both fall back to reading `.output` off the same block via a double `as unknown as` cast — meaning the real serialized shape (from Python/Rust session JSONL) carries an `output` field the type doesn't model. A double-unknown cast is the exact pattern that hides a genuine schema mismatch from the compiler; if the backend ever renames `output`, tsc stays green and file-diff attribution silently breaks. Pairs with Team H's Frontend⇄Rust contract table.
- **Fix sketch:** Model the actual union (`content` OR `output`) in `ToolResultBlock` and delete the casts; add a golden-transcript fixture asserting the field name.
- **Cross-territory?** yes — canonical shape is Team H; the type + cast removal is Team F.

## F-F-10: Meaningful `invoke()` rejections silently swallowed at several call sites (delete-token, save-run-history)
- **File:** desktop/src/components/kim-ui/settings-panes/PaneAccount.tsx:250 (`delete_github_token`), :175 (`show_browser_window`); desktop/src/hooks/useChatStream.ts:842-850 (`save_run_history`); desktop/src/hooks/runSnapshotStore.ts:104 (snapshot persist)
- **Severity:** Medium
- **Class:** bug (error-UX)
- **Evidence:** Inventory of the 23 `.catch(() => {})` sites: most (set_task_active_mode, show_main_window, show_screenshot_flash, stop_schedule_timer) are legitimately fire-and-forget UI toggles. But a few swallow user-consequential failures with zero surfacing: (1) `delete_github_token` — the user clicks "Disconnect GitHub"; if the invoke rejects, the UI proceeds as if disconnected while the token persists on disk (a security-relevant false confirmation). (2) `save_run_history` and the orphaned-run snapshot persist — a rejection means the completed run's activity/cost is silently lost from session history with no toast, so the user sees an empty "worked for" pill and assumes nothing ran. Contrast `handleSteer` (useTaskRunner.ts:318-320) and `hitlRespond` (useChatStream.ts:1067-1072), which DO toast on rejection — the pattern exists, it's just not applied consistently.
- **Fix sketch:** Toast on rejection for the security-/data-relevant invokes (delete_github_token, save_run_history); keep silent-catch only for idempotent UI-mode toggles, and add a short comment at each intentionally-silent site.
- **Cross-territory?** no

*(hunt in progress — further findings appended below as confirmed)*
