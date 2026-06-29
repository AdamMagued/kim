# Kim — Refactor Roadmap (runtime-contract changes; require app testing + sign-off)

Status as of branch `audit-fixes`. The bug-fix campaign and the **gate-verifiable** refactors
(god-file splits of `agent.py`, `lib.rs`, `http_bridge.rs`; pyright cleanup) are **done and green**.

**Update (2026-06-29):** R-4 (#17) and R-5 (#18) are now **implemented, committed, and pushed** to
`origin/audit-fixes` (commits `a59578a`, `e5820a7`) — static-gate-verified but their **live-Tauri
verification is still pending** (see each section). R-1/R-2/R-3/R-6/R-7 remain open (= issues
#14/#15/#16, #19, #20; plus #13 risky-runtime checklist and #21 ollama window-list).

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
**✅ DONE — commit `a59578a` (issue #17). Static gates green; live-app run STILL PENDING.**
- `git mv mcp_server/tools/codex_bridge.py → codex_engine/engine.py` (new top-level package + empty
  `__init__`). `orchestrator/codex_bridge_service.py` now forward-imports `from codex_engine.engine
  import …`; the `sys.path` insert and the `# noqa: E402` markers are gone.
- ⚠️ Kept `_HERE`/`_REPO` in `codex_bridge_service.py` — `_REPO` also resolves the default
  `config.yaml` (line ~93), so deleting lines 34–38 wholesale (as the issue text literally says) would
  `NameError`. Only the `sys.path` insertion was removed.
- Also added `codex_engine` to `pyrightconfig.json` `include` + `kim-orchestrator.spec` `datas`
  (both outside the issue's file list) to preserve type-check + PyInstaller-bundle parity.
- Verified: codex suite 44/44, full pytest green, pyright 0/0, and `python -m
  orchestrator.codex_bridge_service --help` resolves from the repo-root cwd AND a foreign cwd with
  `PYTHONPATH=kim_root` (proves the hack was dead weight). flake8 clean on touched files.
- LEFT (VERIFY): `npm run tauri dev` → run a Code-tab Codex task end-to-end; confirm the bridge spawns
  and routes through Kim's BrowserProvider, and the Code tab never uses OpenAI auth/gpt-5.5.

## R-5. Unify the 4 divergent config loaders → single source  (MEDIUM risk)
**✅ DONE — commit `e5820a7` (issue #18). Static gates green; live-app run STILL PENDING.**
- Canonical defaults chosen: `provider=ollama`, `use_real_browser=false`. Shared
  `DEFAULT_PROVIDER="ollama"` in `orchestrator/agent_config.py` (used by `agent.py` + `cli.py`, was
  inline `"claude"` at both); `DEFAULT_USE_REAL_BROWSER=False` in `mcp_server/config.py` (flipped from
  `True`); added the missing `claude` entry to `config.rs default_model_map()`; `config.yaml.example`
  provider `browser → ollama`; new `tests/test_config_parity.py` (11 tests) locking provider +
  use_real_browser across Python constants, the YAML template, `subprocess.rs`, `cli/src/config.rs`,
  `index.ts`, and the Rust model map. Mutation-tested + CI-deterministic.
- ⚠️ Gotcha for whoever continues: **`config.yaml` is GITIGNORED** (`.gitignore:26`). The tracked
  template is `config.yaml.example`; on a fresh install the in-code defaults govern — NOT `config.yaml`,
  contrary to the issue's "config.yaml ships with every install". The runtime `config.yaml` is the
  user's local instance (left untouched).
- Behavior change: the no-explicit-provider default goes `claude`/`browser` → `ollama` (the decided value).
- LEFT (VERIFY): `npm run tauri dev`; delete the `provider`/`use_real_browser` keys (or rename the local
  config.yaml) to exercise the fresh-config path; confirm `ollama` wins and the dedicated Kim Chromium is
  used (not the user's real Chrome over CDP). A `tauri dev` restart is required after the `.rs` change.

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
