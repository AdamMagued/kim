# Operation Google-Level — WAVE 2 DISPATCH PLAN

**Author:** Triage Authority (Fable), 2026-07-12
**Source of truth:** `docs/ops/TRIAGE.md` (verdicts) + `docs/ops/findings/team-*.md` (evidence).
**Baseline for every Wave-2 branch:** `integration/audit-fixes` @ the post-triage tip
(commit after `4d27416` — the triage/license commit; `7075708` already removed
`pythonExperimentTool/`). Branch from this baseline, **not** from each other.

This is what the conductor hands each Wave-2 agent. One agent per branch.

---

## Wave-2 rules (every team, non-negotiable)

1. **Branch:** `ops/w2-<team>` (e.g. `ops/w2-mcp`), cut from the triage baseline.
2. **Territory discipline:** edit ONLY files inside your glob set below. A fix that needs
   another team's file = write a **handoff note**, do not cross the line. This is what keeps
   parallel merges conflict-free.
3. **Every fix ships a failing→passing test.** No test, no merge. (Exceptions: pure deletions,
   pure docs, pure config-key removal.)
4. **Green gate before requesting merge:** `pytest tests/`, `desktop` vitest + `tsc --noEmit`,
   `cargo test` (desktop **and** cli), pyright, clippy, eslint — all green. Use `KIM_FAKE=1`
   for anything needing app behavior offline. (Note: run the full pytest suite **without** a
   global `KIM_FAKE=1` — that env var forces the fake provider and will fail
   `test_contracts` unknown-provider assertions; set it per-test only. Playwright browser
   tests need `playwright install` locally or they env-skip.)
5. **Behavioral invariants that must never regress** (from OPERATION_GOOGLE_LEVEL §2.6):
   Code tab never uses OpenAI auth / gpt-5.5; `[END_OF_RESPONSE_{id}]` sentinel protocol;
   `[STATUS]`/`[PLAN]`/`[STEP]`/`[DONE]`/`[CONTEXT]`/`[UI]` stdout protocol; HITL hard-block
   self-enforcement; shell/sandbox deny-list gates (code-owned, config must never weaken them);
   codex CLI text protocol.
6. **Definition of done per team:** every assigned ACCEPTED finding closed with a linked
   commit + test; findings file annotated with the fix commit hash; `git diff --stat` stays
   inside the territory glob.

---

## Merge order (decided by Triage Authority)

```
ops/w2-hotfix-crit  →  C'  →  A'  →  D'  →  B'  →  E'  →  F'
        (+ G' residual items fold into C'/D'/A' territory passes; the big G' delete is DONE)
```

One integrator agent owns the train, runs full suites after each merge, bisects on breakage.
Rationale for order: the crit RCE hotfix closes the arbitrary-exec hole before anything else
ships; C' hardens the rest of the tool-safety surface; A' fixes the correctness/lifecycle
core that D'/B'/F' build on (the run-identity + lifecycle-event root cause is upstream of the
frontend event-bleed bugs); F' (UI) merges last so it renders against already-fixed events.

---

## ops/w2-hotfix-crit — Criticals (merge FIRST)

**Owner territory:** `mcp_server/tools/shell.py`, `mcp_server/tools/git.py`, and their tests.
**Findings:** **F-C-1** (`git -c alias.*=!sh` RCE), **F-C-2** (awk/tar/sed/make exec escapes),
**F-K-8** (git path-validation gate has zero test coverage — ships the missing regression pack).
**Approach:** argv-level policy on known escape flags + default the HITL threshold on; NOT a
name-blocklist patch (allowed programs can exec other programs — that's the root cause).
**Why separate + first:** blast radius is un-approved arbitrary RCE + absolute-path secret read
under default config; the fix is self-contained and must not wait on the full C' pass.

---

## A' — Orchestrator core  (`ops/w2-orchestrator`)

**Territory:** `orchestrator/**` EXCLUDING `orchestrator/providers/**`
(agent.py, session_store.py, memory.py, compaction.py, context_meter.py, context_loader.py,
task_queue.py, codex_appserver_transport.py, codex_bridge_service.py, scheduled_runner.py,
run-lifecycle/checkpoint modules) + their tests under `tests/`.

**Owned ACCEPTED findings:**
- Core: F-A-1, F-A-2 (+B' guard), F-A-3, F-A-4, F-A-5, F-A-6, F-A-7, F-A-8
- Inherited: F-INH-5, F-INH-7, F-INH-8
- Concurrency (Team J): F-J-1 (scheduled_runs log leak), F-J-4 (fsync offload), F-J-6 (self-watchdog)
- Contracts (Team H) — emitter side: F-H-1 (run-lifecycle CLEAR events off-schema/un-enveloped),
  F-H-2 + F-H-8 (codex-bridge typed mode emits no run-done/failed; spawn exports no run identity —
  **root cause of F-F-2 & F-F-5**), F-H-7 (codex-proxy schema loss), F-H-3 (silent chat-stdout drop)
- Security (Team I): F-I-2 (KIM_CODEX_BYPASS_SANDBOX unsandboxed RCE — orchestrator/codex spawn side;
  pair with C'), F-I-3 (session JSONL transcripts world-readable — session_store perms)
- Docs code-half: F-L-1 (screenshot-retention: the real number/mechanism lives in memory.py)

**Handoffs:** F-H-1/F-H-2 consumer rendering → F'; F-I-2 shell layer → C'.

---

## B' — Providers  (`ops/w2-providers`)

**Territory:** `orchestrator/providers/**` (base.py, claude.py, openai_provider.py, gemini.py,
deepseek.py, ollama.py, `browser/**`) + provider tests.

**Owned ACCEPTED findings:**
- F-B-1 (Gemini OAuth failure misclassified), F-B-2 (+A' root), F-B-3, F-B-4, F-B-5, F-B-6,
  F-B-7 (sentinel-echo race), F-B-8 (non-idempotent browser retries), F-B-9, F-B-10, F-B-11,
  F-B-12, F-B-13, F-B-14
- Inherited: F-INH-1 (Gemini OAuth token frozen at spawn; +D'), F-INH-2, F-INH-3, F-INH-4
- Concurrency: F-J-3 (reap orphan CDP Chrome)
- Security: F-I-4 (CDP :9222 unauth — browser launch flags; **pair with D'** for the desktop
  launch-code half)
- **Also executes:** the provider conformance matrix / suite (Team B's V-3 finish).

---

## C' — MCP server & tools  (`ops/w2-mcp`)

**Territory:** `mcp_server/**` (server.py, tool_registry.py, config.py, logger.py, `tools/**`,
`sites/**`) + MCP tests. (shell.py/git.py already touched by the hotfix — rebase on it.)

**Owned ACCEPTED findings:**
- F-C-3 (`gh auth token` exfiltration), F-C-4 (SSRF via subresource/XHR — **one guard with D'**),
  F-C-5 (unclamped run_python/run_node/web_wait timeouts), F-C-6 (code.py no pgroup kill), F-C-7
- Inherited cobweb: scalar `allowed_paths`→"/" clamp, null-config-section boot crash,
  client/server tool-timeout double-execution (per Team C territory in TRIAGE)
- Contracts: F-H-4 (unenforced MCP required-args), F-INH-6 (MCP errors plain-text contract)
- Dead config (Team G, C-side): **F-G-4 — delete the `shell.blocked_commands` key + document
  the deny-list as code-owned. REJECTED: wiring it into the deny-set** (config must never weaken
  a code-owned gate — CLAUDE.md invariant).
- DX: F-L-10 (shared `tool_error` helper for ~20 bare `ERROR:` sites)
- **Also executes:** the safety-gate regression test pack.

---

## D' — Desktop Rust backend  (`ops/w2-desktop`)

**Territory:** `desktop/src-tauri/src/**` (subprocess.rs, lib.rs, browser_bridge.rs,
http_bridge/**, google_oauth.rs, provider_auth.rs, codex_projects.rs, task_spec.rs, codex_route.rs,
build.rs), `desktop/src-tauri/tauri.conf.json`, `capabilities/**` + Rust tests.

**Owned ACCEPTED findings:**
- F-D-1 (`/v1/open` SSRF), F-D-2 (paths.rs env-override ignored), F-D-4 (bridge token in provider
  webviews), F-D-5 (**ACCEPT-stretch** — stdout back-pressure; if it runs long, slip the full
  bounded-channel rework to a follow-up and prioritize the lifecycle fixes)
- F-C-4 SSRF guard (desktop half, pair with C'); F-I-4 CDP launch half (pair with B')
- Concurrency: F-J-5 (spawn_blocking run_update)
- Contracts: F-H-2/F-H-8 spawn-identity export (Rust side), F-H-5 (orphaned event channels)
- Dead-code cleanup (Team G → D' territory): **F-G-1** (strip the dead bundled-codex/bundled-claw
  arms in `find_code_backend`; reword the "Build pythonExperimentTool/…" error to "install codex
  or set CODEX_BIN") and the residual Claw dead-arm strip (`CodeBackendKind::Claw`,
  `mirror_latest_claw_session_to_codex`, `codex_direct_spec` claw shape) — the vendored source is
  already deleted, this removes the now-dangling hooks. **F-L-9** (dead `~/.kim_root` arm).
- DX: F-L-2 desktop half (venv-missing friendly-fail surfaced in the UI)
- ~~F-D-3 `/v1/health` unauth~~ — **REJECTED (by design; loopback liveness). Do not fix.**

---

## E' — CLI  (`ops/w2-cli`)

**Territory:** `cli/**` (main.rs, commands.rs, provider/codex_stream.rs, …) and `kimctl/**`.

**Owned ACCEPTED findings:**
- F-E-1, F-E-2, F-E-3, F-E-4 (one-shot exits 0 on FAILED/Ctrl-C), F-E-5, F-E-6,
  F-E-7 (`kimctl send --session` false success from stale TASK_COMPLETE), F-E-8, F-E-9, F-E-10,
  F-E-11, F-E-12, F-E-13, F-E-14, F-E-15
- **DEFERRED (not this wave):** the main.rs/commands.rs god-file split — do it as its own isolated
  PR after Wave 2 (high churn, zero behavior change, collides with every E' fix edit).

---

## F' — Frontend  (`ops/w2-frontend`)

**Territory:** `desktop/src/**` (RevampSidebar.tsx, useChatStream.ts, ChatView, kim-ui, hooks,
types, `styles/**`) + `*.test.ts(x)`.

**Owned ACCEPTED findings:**
- F-F-1 (no ESLint / false no-`any` claim — wire eslint; **pair with K'/L' docs**), F-F-2
  (cross-session event bleed — consumer half; pair with A'/H), F-F-3, F-F-4, F-F-5 (spinner-forever
  + hidden recovery banner), F-F-6 (Google-CDN font import), F-F-7, F-F-8 (envelope), F-F-9
  (ToolResultBlock shape), F-F-10 (swallowed invoke rejections), F-F-11 (O(n²) re-render), F-F-12,
  F-F-13
- Concurrency: F-J-2 (cap runHistory)
- Contracts consumer side: F-H-1 (render the lifecycle CLEAR events once A' emits them on-schema),
  F-H-6 (undocumented tag grammar — align the parser)
- Docs: F-L-12 (eslint no-`any` doc claim)
- **Also executes:** dead-CSS purge, `any` elimination, a11y quick wins.

---

## G' — Satellites & root configs  (`ops/w2-satellites`)

**Big win already DONE** (Triage Authority, commit `7075708`): `pythonExperimentTool/` +
`tests/claw_test_suite.py` + `scripts/claw-via-browser` deleted; suites green.

**Territory:** `config.yaml`, `config.yaml.example`, `requirements.txt`, `install.sh`, `justfile`,
`.github/workflows/**` (comment/ignore lines only), `codex_engine/**` (KEEP — protocol snapshots).

**Owned ACCEPTED findings (small residual pass):**
- F-G-6 (**Pillow CVE bump** ~=10 → >=12.1.1; re-run all four suites), F-G-8 (drop unused
  `aiosqlite`/`pynput` deps + conftest stub), F-G-9 (install.sh min-Python-3.11 check),
  F-G-3 (delete dead `relay:` config block), F-G-5 (delete dead `voice:` config block),
  F-G-2 (fix stale CI comment referencing deleted `run_claw_bridge.py`),
  and clean the now-stale post-deletion refs: `Cargo.toml` `exclude=["pythonExperimentTool"]`,
  `pytest.ini` vendored-tree note, `ci.yml` `--ignore=tests/claw_test_suite.py`.
- F-G-4 config-key delete coordinated with C' (C' owns the mcp_server side).
- F-G-1 lives in `subprocess.rs` → executed by **D'** (Rust territory), not here.

**Note:** `codex_engine/` = KEEP (actively maintained, 15+ suites; the 3.1 MB is load-bearing
appserver protocol snapshots). Only add an owner note for who refreshes the snapshots.

---

## Cross-cutting findings — where each landed

- **Team H (contracts):** H-1→A'(emit)/F'(render); H-2/H-8→A'+D'; H-3→A'; H-4→C'; H-5→D';
  H-6→F'; H-7→A'; H-9→(doc, folded into owner).
- **Team I (security):** I-2→C'+A'; I-3→A' (session_store perms); I-4→B'+D';
  **I-1→DEFER** (cosign signing — owner infra decision).
- **Team J (concurrency):** J-1/J-4/J-6→A'; J-2→F'; J-3→B'; J-5→D'.
- **Team K (tests/CI):** K-8→hotfix; test-addition findings fold into the territory team that
  owns the code under test; **K-1 (Windows CI)→DEFER to Wave 4 R1**; **K-6 (cosign)→DEFER**;
  the flake one-liner (task_timeout_s 1→8) and real-binary parity auto-skip (K-7) fold into
  whichever territory touches that suite.
- **Team L (docs):** L-1→A'(code)+doc; L-2→D'; L-9→D'; L-10→C'; L-12→F'; **L-11 DONE (MIT)**;
  L-3..8/13/14→ fold into each team's territory-doc pass (or a Wave-4 R3 docs-canon sweep).

## Deferred / rejected — explicit, so nothing is silently dropped

- **DEFER:** F-I-1/F-K-6 (cosign signing — owner), F-K-1 (Windows CI — Wave 4 R1),
  F-E god-file split (post-Wave-2 isolated PR), F-D-5 full rework (stretch),
  L-docs-consolidation (Wave 4 R3).
- **REJECT:** F-D-3 (`/v1/health` by-design loopback liveness), F-G-4 wire-up half (config
  must not weaken code-owned deny-list), cobweb 5.2 + the prior already-fixed cobweb block.

---

**Ready to dispatch.** Cut `ops/w2-hotfix-crit` first; the other seven branch in parallel from the
same baseline and merge in the order above.
