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

*(hunt in progress — further findings appended below as confirmed)*
