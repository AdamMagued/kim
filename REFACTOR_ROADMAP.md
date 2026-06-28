# Kim — Refactor Roadmap (runtime-contract changes; require app testing + sign-off)

Status as of branch `audit-fixes`. The bug-fix campaign and the **gate-verifiable** refactors
(god-file splits of `agent.py`, `lib.rs`, `http_bridge.rs`; pyright cleanup) are **done and green**.

The items below were deliberately **NOT auto-applied**. They change runtime contracts (IPC wire
format, process control flow, build layout, or config defaults) that `cargo`/`tsc`/`pytest`
**cannot** verify — a wrong change here compiles and passes unit tests but breaks the running app.
The architecture assessment (`scratchpad/audit/arch/ARCHITECTURE_ASSESSMENT.md`) sequences these as
Phase 1–3, explicitly "needs observability + owner sign-off first." Each must be verified by
launching the Tauri app (`npm run tauri dev`) and exercising the affected flow.

## R-1. Unify the agent-log protocol across TS + Python + Rust  (HIGH leverage, HIGH risk)
- Today `[STATUS]/[TOOL]/[PLAN]/[STEP]/[DONE]/[CONTEXT]` is hand-parsed in 3 languages
  (`desktop/src/.../utils.ts`+`parsers.ts`, Python emitters, `subprocess.rs`). Drifts on edge cases.
- Cure pattern already exists: `scripts/gen-events.js` → `events.gen.ts`. Extend codegen so one
  manifest (`events.schema.json`) is the single source consumed by all three runtimes; replace the
  hand-parsers with generated decoders.
- VERIFY: run the app, confirm every status/plan/step/tool/context line still renders in ChatView.

## R-2. Flip `ipc_protocol` default to the typed `kim:*` path  (HIGH risk)
- Wave-2 left the safe default in place. Flipping it makes the typed events authoritative.
- VERIFY IN APP: context meter, token pill, screenshot flash, HITL approval card, and
  rate_limited/run_failed/provider_error events all fire. (No static test can observe IPC delivery.)
- Pairs with R-1 — do R-1 first.

## R-3. Introduce a `TaskRuntime` owner unifying the two spawn paths  (HIGH risk)
- `send_task` (GUI) and `/v1/task` (kimctl/CLI bridge) duplicate orchestrator-spawn logic and have
  diverged (HITL/stdin/env). Centralize into one owner so both paths share reservation, cancel, env.
- VERIFY IN APP: GUI send + kimctl run; concurrent trigger reserves a single runner; cancel kills cleanly.

## R-4. Relocate the codex engine out of `mcp_server/tools/`  (MEDIUM risk)
- The 1207-LOC codex engine lives downstream of the orchestrator that imports it via a `sys.path`
  hack (dependency runs backwards). Move to a proper package; fix the import path.
- VERIFY: codex/Code-tab flow runs end-to-end after the move.

## R-5. Unify the 4 divergent config loaders → single source  (MEDIUM risk)
- `config.rs` (hand-rolled YAML), Python loaders, etc. disagree on defaults (`use_real_browser`
  opposite polarity; `provider` differs 4 ways). Pick canonical defaults (a behavior decision),
  centralize. VERIFY: defaults match intended behavior on a fresh config.

## R-6. Cargo workspace for `desktop/src-tauri` + `cli`  (MEDIUM risk, build-system)
- Share deps/lockfile, single `cargo test`. VERIFY: `cargo build`, Tauri bundle, and `cli` install
  still work.

## R-7. Adjudicate dormant / fabricated features — PRODUCT DECISION (ship / flag / delete)
- `relay_server/` (RELAY_ENABLED=false; fully deployable attack surface), `voice` (config scaffold),
  connectors panel "Linear connected" fabrication, ~25/82 Rust commands with no UI caller,
  `pythonExperimentTool/claw-code` (vendored dead weight). Each needs an explicit keep/flag/remove
  call from the owner. Do NOT delete relay/voice without sign-off (they're intentionally flagged).

---

## Risky-runtime checklist (fixes ALREADY APPLIED — verify in the running app)
These are correct in code but only observable at runtime:
- ipc typed-event delivery (R-2 context); codex HITL stdin approval round-trip (no 120s stall);
  dual-spawn mutex (one runner, cancel works); codex explicit user-approval gate;
  GitHub PAT → OS keychain (leaves account.json, still works for gist/GitHub);
  CDP port/profile configurable; schedule timer guard (no 2nd agent while one runs);
  React ErrorBoundary / unhandledrejection fallback; a11y keyboard + focus-trap (sidebar/settings/modals);
  connectors panel open after event-bus rewiring; StreamRenderer perf/animation after retry-collapse.

## Open design call
- ollama "what's on my screen": gpt-oss is text-only → real vision impossible. Only fix is extending
  the proactive window-list TEXT fallback (currently BrowserProvider-only in `agent.py:run()`) to
  ollama. Behavior change — owner decision.
