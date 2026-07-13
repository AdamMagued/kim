# Kim: Roadmap to 10/10

**Status:** adopted plan (read-only audit, 2026-07-06, branch `feat/browser-stateful-threads`)
**Baseline:** overall **6.5/10** — architecture 7, code quality 6.5, testing 6.5, security 5.5,
robustness 7, docs 6 (full assessment recorded in the session audit; the worst/best lists are
summarized inline below).
**Companion docs:** `docs/PROPOSAL_codex_appserver_parity.md` (Phase 3 uses it as-is),
`docs/APPENDIX_appserver_probe_findings.md`.
**Execution state:** tracked in `docs/ROADMAP_PROGRESS.md` — as of 2026-07-06 Phases 0–2 are
done and Phase 3 is code-complete there; check it before treating any backlog row below as
pending (this file is the plan snapshot, not the progress ledger).

An executable improvement program grounded in the actual code. Every item carries file:line
evidence, an effort tag (S ≤0.5d, M ~1–2d, L ~3–5d agent-days), and the score delta it buys.

A note on the ceiling before anything else: **security and robustness can never hit a true 10
while the core LLM transport is DOM-scraping a chat UI you don't control**
(`orchestrator/providers/browser/provider.py`, `site_configs.py` selectors). The realistic
ceiling for those two dimensions on the browser premise is ~9. The single highest-leverage
move — already scoped in `docs/PROPOSAL_codex_appserver_parity.md` — is to make the browser
the *model* transport only and move *tool execution + approvals* onto Codex's native
`app-server` protocol, which restores a real structured contract. That proposal is the
backbone of Phase 3 below; it is referenced rather than redesigned.

---

## The 5 keystone items (do these first — each unlocks multiple downstream fixes)

| # | Keystone | Why it is a keystone | Unlocks |
|---|----------|---------------------|---------|
| **K1** | **Server-side allowlist + HITL enforcement inside `mcp_server/`** (new `mcp_server/policy.py`, enforced in `server.py:call_tool`) | Today enforcement is a bypassable denylist in `shell.py` and an *orchestrator-side* gate in `agent.py:1250` that the MCP server itself does not enforce. Moving the chokepoint into `call_tool` makes every caller (agent, CLI, codex bridge, future connectors) safe by construction. | Fixes security items S1–S5; makes the "shell theater" problem structural instead of whack-a-mole; is the hook the app-server approvals plug into (Phase 3) |
| **K2** | **Decompose `subprocess.rs::send_task`** (1507-line file; `send_task` spans 445→1012) into a `TaskSpec` builder + `EnvBuilder` + `SpawnSupervisor` | The 568-line multiplexer is the single worst maintainability hotspot and the reason the GUI and `/v1/task` (`http_bridge.rs:1128`) spawn paths have diverged on HITL/env/stdin. One `TaskSpec` builder collapses both. | Fixes REFACTOR_ROADMAP R-3 (dual-spawn unification); makes env/HITL bugs testable; precondition for Phase 3 approval plumbing |
| **K3** | **Injectable transport seam in `BrowserProvider`** — extract a `PageDriver` protocol so `complete()` no longer creates the Playwright context inline (`provider.py:387`) | The `Page` object being non-injectable is *the* reason the driver loop has zero tests and the response-wait heuristics can't be regression-tested. A seam turns the untestable 1257-line file into fixture-driven contract tests. | Unlocks the entire browser testing program (T3), recorded-DOM contract tests, and the fallback/re-emit robustness work (Rb2) |
| **K4** | **Delete dead `run_codex_subtask` + rewrite its 3 source-grepping test files as behavioral tests** (`engine.py:118-280`; `tests/test_codex_env_scoping.py`, `test_codex_bridge_tool.py`, `test_codex_process_cleanup.py`) | These AST/`getsource` tests *pin the weaker `**os.environ` env contract* (`engine.py:179`) that contradicts the hardened live path (`codex_bridge_service.py:392`). They actively block the security fix. Deleting the dead code + rewriting the tests removes a landmine and models the pattern for the other 25 grep-tests. | Resolves the "contradictory env construction" finding; establishes the behavioral-test pattern that fixes ~34% of the suite |
| **K5** | **Unify the agent-log event protocol via the existing codegen** (`scripts/gen-events.js` → `events.gen.ts`/`events.gen.rs`/`events_gen.py`; REFACTOR_ROADMAP R-1) | `[STATUS]/[TOOL]/[PLAN]/[STEP]/[DONE]` is hand-parsed in 3 languages today. The codegen seam already exists and CI already drift-checks it (`ci.yml` "Events schema drift check"). Extending it to the log lines kills a whole class of cross-runtime drift bugs and is the vocabulary the app-server UX events (Phase 3) extend. | Robustness across IPC; precondition for the typed-event flip (R-2) and the new approval/plan/diff events in the parity proposal |

Sequence: **K4 → K1 → K2 → K3 → K5**. K4 is pure cleanup that de-risks K1. K1 and K2 are the
security+arch spine. K3 unlocks testing. K5 is the IPC spine feeding Phase 3.

---

## Dimension 1 — Architecture (7 → 9)

**Current strengths:** clean cargo workspace (`Cargo.toml` members `desktop/src-tauri` + `cli`),
codex engine already relocated to its own package (`codex_engine/`, REFACTOR R-4 done), tiered
MCP tools (`tool_tiers.py`).

| Item | Evidence | Effort | Δ |
|------|----------|--------|---|
| **A1 (=K2)** Decompose `send_task` | `subprocess.rs:445-1012` — one fn handles provider promotion, codex-vs-chat routing, browser bridge, env, Chrome launch, spawn, HITL stdin, wait, cleanup | **L** | +0.6 |
| **A2 (=K5)** One event manifest → 3 runtimes | R-1; hand-parsers in `desktop/src/components/chat/parsers.ts`, Python emitters, `subprocess.rs:forward_agent_stdout_line` | **M** | +0.4 |
| **A3** Unify the two spawn paths behind one `TaskRuntime` owner | `subprocess.rs:send_task` (GUI) vs `http_bridge.rs:1128` (`/v1/task`) duplicate reservation/env/HITL; `task_runtime.rs` already exists as the seam | **M** | +0.3 |
| **A4** Split `provider.rs` (2246 lines) — separate the browser route (`stream_codex_subprocess`) from the local-ollama route (`start_responses_proxy` + embedded `responses_proxy.py`) | `cli/src/provider.rs:934-1105` and `1260-1379` are two unrelated proxies in one file | **M** | +0.3 |
| **A5** Adjudicate dormant subsystems (product decision, REFACTOR R-7) | `relay_server/` (RELAY_ENABLED=false, deployable attack surface), voice scaffold, ~25/82 Rust commands with no UI caller, vendored `pythonExperimentTool/claw-code` | **S** (decision) / **M** (removal) | +0.2 |
| **A6** Flip `ipc_protocol` default to typed `kim:*` (R-2, pairs with A2) *[shipped in Phase 2 — default is already `typed` (`config.rs default_ipc_protocol()`); see ROADMAP_PROGRESS.md "A6 / R-2"; live-app verification still outstanding]* | REFACTOR_ROADMAP R-2; requires live-app verification | **S** | +0.2 |

**Ceiling note:** architecture can reach 9. The last point (→10) requires the app-server
migration (Phase 3), which structurally fixes the "spawn a fresh Codex per message that forgets
its own tool outputs" flaw (`PROPOSAL_codex_appserver_parity.md` §0.3).

---

## Dimension 2 — Code Quality (6.5 → 9)

| Item | Evidence | Effort | Δ |
|------|----------|--------|---|
| **Q1 (=K4)** Delete dead `run_codex_subtask` | `engine.py:118-280`; only callers are its own docstring + 3 grep-test files (no runtime caller) | **S** | +0.5 |
| **Q2** Break up `agent.py` (2225 lines) `KimAgent` | `agent.py:131` — `run()` (483), `_handle_tool_response` (825), `_execute_tool` (1238), `_compact_*` (1826-2032) are separable into a `ToolLoop`, `CompactionManager`, `RetryPolicy` | **L** | +0.4 |
| **Q3** Split `useChatStream.ts` (794 lines, ~16 Tauri listeners, 20+ `useState`/`useRef`) into `useTaskLifecycle`, `useActivityFeed`, `useHitlApproval`, `useContextMeter` | `desktop/src/hooks/useChatStream.ts:88-161` | **M** | +0.3 |
| **Q4** Reconcile the two `_CodexProxy` env constructions so only the hardened one survives (falls out of Q1) | `engine.py:179` (`**os.environ`) vs `codex_bridge_service.py:392` (minimal allowlist) | **S** | +0.2 |
| **Q5** Extract `web.py` (1561 lines, 13 handlers) and `github.py` (568) into per-concern modules | `mcp_server/tools/web.py`, `github.py` | **M** | +0.2 |
| **Q6** Enforce a max-file-lint gate in CI (fail >800 lines for new code) to prevent regrowth | new `ci.yml` step | **S** | +0.1 |

---

## Dimension 3 — Testing (6.5 → 9)

**Current state (verified):** 82 Python test files, but **28 of them are source-grepping tests**
(`read_text`/`getsource` — e.g. `test_codex_env_scoping.py:37`, `test_codex_bridge_tool.py:57`,
`test_invariants.py`, `test_config_parity.py`) that assert on *source text*, not behavior. Rust
tests are all inline `#[cfg(test)]` unit tests (cli: 136 across 7 files; desktop: ~30 in
`subprocess.rs:1151+`) — **no `cli/tests/` or `desktop/src-tauri/tests/` integration dirs
exist**. Frontend: Vitest covers `parsers.ts`, `codexEvents.ts`, `agentProtocol.ts`,
`ErrorBoundary` — but **`useChatStream.ts` (the god-object) has zero tests** (only
`useTaskRunner` and `useTheme` in `hooks/__tests__/`). **No recorded DOM fixtures** for the
browser provider — `tests/fixtures/` holds only `golden_transcript.json`. **No E2E anywhere.**

| Item | Evidence | Effort | Δ |
|------|----------|--------|---|
| **T1 (=K4 tail)** Convert the 28 source-grepping tests to behavioral tests | e.g. `test_codex_env_scoping.py:52` asserts `run_codex_subtask` *string* spreads `os.environ` — rewrite to spawn a fake binary and assert the actual `env` dict passed | **L** (batched) | +0.6 |
| **T2** Rust integration harnesses: `cli/tests/cli_flow.rs` driving arg/event plumbing against a fake `codex_bridge_service`; `desktop/src-tauri/tests/task_spawn.rs` on the decomposed `TaskSpec` (needs K2) | no integration dirs exist today | **M** | +0.4 |
| **T3 (needs K3)** Browser-provider contract tests against **recorded DOM snapshots** — capture real Gemini/ChatGPT response HTML into `tests/fixtures/dom/`, feed a `FakePageDriver` (the K3 seam), assert the two-phase wait (`provider.py:1141`/`1155`) and `response_parser.parse_response` produce correct tool calls | `response_parser.py` is already pure/testable; the driver loop is not | **L** | +0.5 |
| **T4** Golden translation tests for the codex proxy: canned browser reply → `_provider_response_to_responses_api` (`engine.py:1179`) → assert SSE frames | proxy is pure given a fake provider | **M** | +0.3 |
| **T5** `useChatStream.ts` Vitest suite with mocked Tauri `listen()` — approval flow, activity dedup (`recentRawRef`), context-meter state | `useChatStream.ts:88-161` | **M** | +0.3 |
| **T6** A single local **E2E smoke** driven by `scripts/probe_appserver.py` (proposed in the parity doc §0.4) + a `FakeProvider` returning canned tool calls — proves the full CLI→bridge→proxy→codex loop without a real browser | feasible locally; no network | **M** | +0.3 |
| **T7** CI: add pytest **coverage gate** and `cargo test -p kim-cli` integration run; today `ci.yml` runs unit tests only | `.github/workflows/ci.yml` | **S** | +0.2 |

Top-10 riskiest untested modules (by size × criticality): `provider.rs` (2246), `agent.py`
(2225 — partial coverage), `subprocess.rs::send_task` (568-line fn), `useChatStream.ts` (794,
zero), `browser/provider.py` (1257, driver loop untested), `web.py` (1561),
`engine.py::_CodexProxy` (untested end-to-end), `http_bridge.rs` (2189, route handlers),
`session_store.py` (943), `codex_bridge_service.py::_run_async` (only pinned by grep-tests).

**Ceiling:** testing reaches 9. True 10 needs live browser E2E against real Gemini/ChatGPT,
which is inherently flaky (auth walls, UI drift) — so the *right* target is contract tests
against recorded DOM (T3) + version-drift detection, accepting that the last mile stays manual.

---

## Dimension 4 — Security (5.5 → 9) — the detailed design

### The problem, precisely
Enforcement today is split and bypassable:
1. **`shell.py` is a basename denylist** (`_DENY_COMMANDS`, `shell.py:39`). It matches
   `_basename(tokens[0])` (`shell.py:241`), so a copied/renamed binary, an interpreter
   one-liner (`python -c "import os;os.remove(...)"`), or a novel tool sails through. It is an
   explicit deny set, not an allowlist — anything not named is permitted.
2. **HITL is orchestrator-side, not server-side.** The gate lives in `agent.py:_execute_tool`
   (`agent.py:1250-1264`) via `classify_tool_risk`. The MCP server's `call_tool`
   (`server.py:122-134`) enforces *nothing* — it dispatches directly. Any client that talks to
   the MCP server (CLI, a future connector, a misconfigured bridge) skips the gate entirely.
3. **Env leakage when unsandboxed.** `shell.py:_filtered_env` (`shell.py:101`) strips a
   hardcoded `_DANGEROUS_ENV_VARS` set but otherwise inherits the full parent env
   (`shell.py:407`) — API keys and tokens in the parent process reach the child.
4. **Codex proxy env inconsistency** (K4/Q4): dead path spreads all of `os.environ`
   (`engine.py:179`).

### The fix: `mcp_server/policy.py` + enforcement in `server.py:call_tool`

**Where it goes:** a new `mcp_server/policy.py`, called as the *first thing* inside `call_tool`
(`server.py:122`), before `_DISPATCH.get(name)`. This makes the MCP server the single
chokepoint — every caller is gated, not just the agent.

**Allowlist policy shape** (replaces the shell denylist as the primary control; keep the
denylist as defense-in-depth):

```python
# mcp_server/policy.py
@dataclass(frozen=True)
class ToolPolicy:
    risk: str                       # reuse tool_risk.classify_tool_risk
    requires_approval: bool         # derived from risk vs configured threshold

# Shell-command allowlist (positive model), loaded from config.yaml:
SHELL_ALLOWLIST = {
  "ls","cat","grep","rg","find","git","python","python3","node","npm",
  "pip","cargo","echo","printf","mkdir","touch","pytest","ruff", ...
}
# argv-level rules keyed by binary — the piece shell.py lacks:
ARG_RULES = {
  "git":    deny_subcommands({"push --force", "reset --hard", "clean -fdx"}),
  "python": deny_flags({"-c"}) unless_approved,          # no inline exec w/o HITL
  "find":   deny_flags({"-delete","-exec","-execdir"}),  # already partly in shell.py:247
}
```

`enforce(name, args) -> PolicyDecision` does: (a) `classify_tool_risk` (`tool_risk.py:165`),
(b) for shell tools, tokenize and require **binary ∈ allowlist AND argv passes ARG_RULES** —
validating *arguments*, not just `tokens[0]`, and *resolving the real path* to defeat
rename/copy tricks, (c) validate every path-typed arg through `validate_path`
(`config.py:144`) — closing the gap where `shell.py` only checks redirect targets, not command
arguments like `cp ~/.ssh/id_rsa /tmp`.

**How HITL decisions flow (server-side):**

```
call_tool(name,args)                    # mcp_server/server.py:122
  └─ policy.enforce(name,args)
       ├─ blocked            → return "BLOCKED: …"           (no dispatch)
       ├─ requires_approval  → emit {"type":"tool_approval_request", id, name, args, risk}
       │                        on the server's control channel; BLOCK on the
       │                        decision (stdin line / callback), default-deny on timeout
       └─ allowed            → handler(args)
```

The approval round-trip reuses the **existing, working plumbing**: the Tauri side already does
`hitl_respond_approval` (`subprocess.rs:161`) writing `{"approved":bool}` to child stdin, and
the Python side already blocks on it (`codex_bridge_service.py:_await_hitl_decision:170`). The
change is *moving the emit/await from `agent.py:1257` down into the MCP server* so it covers
all callers, and generalizing the stdin line to carry a decision id +
`accept`/`acceptForSession`/`decline` (exactly the vocabulary the parity proposal §3
standardizes). `emit_hitl_approval_request`/`_result` (`events_gen.py`) already exist as the
typed events.

| Item | Evidence | Effort | Δ |
|------|----------|--------|---|
| **S1 (=K1)** `mcp_server/policy.py` + `call_tool` enforcement | `server.py:122`, `tool_risk.py:165` | **L** | +1.0 |
| **S2** Shell allowlist + argv/path rules replacing basename denylist | `shell.py:39,241` | **M** | +0.6 |
| **S3** Path-validate all path-typed tool args (not just shell redirects) | `config.py:144`, gap at `shell.py:221` | **M** | +0.4 |
| **S4** Minimal-allowlist env for *all* subprocess tools (adopt `code.py:_minimal_env` pattern everywhere; retire full-inherit in `shell.py:407`) | `code.py:191` already does this right; `shell.py` does not | **S** | +0.3 |
| **S5** Retire `KIM_CODEX_BYPASS_SANDBOX` on the app-server path (approvals become native) | `subprocess.rs:617`, `codex_bridge_service.py:420`; parity §5.4 | **S** (Phase 3) | +0.3 |
| **S6** Decommission or firewall `relay_server/` (deployable even when disabled) | REFACTOR R-7 | **S** | +0.2 |

**Ceiling:** security tops out at ~9 on the browser premise. The residual: shell allowlists are
still a policy you maintain, and the browser transport means an LLM's raw prose reaches a JSON
parser (`response_parser.py`) — the injection guard (`response_parser.py:79`, rejecting
unregistered tool names) is good but the surface exists. App-server migration (Phase 3) moves
tool *execution* into Codex's own workspace-write sandbox with per-command approval
(`PROPOSAL §0.4`), which is the real path toward 9→9.5.

---

## Dimension 5 — Robustness / UX (7 → 9)

The browser response-wait heuristics are the fragility center:
- Two-phase wait polls at 0.5s/0.75s up to **600s each** (`site_configs.py:20-21`,
  `provider.py:1152/1233`).
- Completion is inferred from a **sentinel hash the UI model is *begged* to echo**
  (`prompt_builder.py:188-193`); if it doesn't, an adaptive idle heuristic
  (`idle_needed = 20 vs 8`, `provider.py:1214`) waits ~15s of stable text, then "scrapes anyway
  (may be truncated)" (`provider.py:1217`).
- **No malformed-output re-emit negotiation** — a parse miss silently degrades to plain text
  (`response_parser.py:186`).
- **No login-wall/Cloudflare detection** at the provider layer — walls surface as generic
  timeouts.
- Selectors are hardcoded 2-deep fallback lists per site (`site_configs.py:37-159`) — brittle
  to UI redesigns, no self-heal.

*(Note: the frozen-idle-counter, shrink-resync, styled-sentinel matching, reused-thread
detection, and preferred-site enforcement issues found in the same audit were fixed in commit
`6483dea` — the items below are the remaining program.)*

| Item | Evidence | Effort | Δ |
|------|----------|--------|---|
| **Rb1** Structured-output re-emit: on parse failure, re-ask once with "reply ONLY with the tool-call JSON" before degrading | gap after `response_parser.py:186`; parity §7.1 prescribes exactly this | **M** | +0.4 |
| **Rb2 (needs K3)** Contract-test the wait heuristics against recorded DOM so tuning is safe; add an early-exit when the send button re-enables (a stronger completion signal than idle-text) | `provider.py:1155-1237` | **M** | +0.3 |
| **Rb3** Explicit auth-wall detection in `_find_chat_page`/scrape (URL contains `/login`, known Cloudflare markers) → emit a specific `AUTH_REQUIRED` UX event instead of a 600s timeout | `provider.py:684`, `401-409` | **M** | +0.3 |
| **Rb4** Selector health-check + telemetry: log which fallback index matched; surface "selector drift" when the primary misses N times | `provider.py:1124-1139` | **S** | +0.2 |
| **Rb5** K5 typed-event flip removes IPC edge-case drift in the activity feed | R-1/R-2 | **M** | +0.2 |
| **Rb6** Codex proxy: make `MAX_RELAYS` per-turn (parity §2.3) so long legit sessions aren't cut at 50 | `engine.py:68,582` | **S** | +0.1 |

**Ceiling:** ~9. You cannot make scraping a foreign UI fully deterministic; the mitigations
that get closest are re-emit negotiation (Rb1), recorded-DOM contract tests (Rb2), and
ultimately the app-server transport where completion is a real protocol event
(`turn/completed`), not an idle-text guess.

---

## Dimension 6 — Docs (6 → 9) — consolidation plan

**Verified state:** 18 root `.md` files, 5706 lines. `AGENTS.md` is **broken**: its
"Per-directory guides" table (`AGENTS.md:22-27`) points to `orchestrator/AGENTS.md`,
`mcp_server/AGENTS.md`, `desktop/src/AGENTS.md`, `desktop/src-tauri/AGENTS.md` — **none of
which exist**; the real per-directory guides are `CLAUDE.md` files. It also claims "31
OS-control tools" while the registry exposes ~50. Many roadmap docs overlap:
`IMPROVEMENT_PLAN.md` (642), `HARNESS_ROADMAP.md` (640), `PRODUCTION_ROADMAP.md` (456),
`REFACTOR_ROADMAP.md` (95), `DEEP_DIVE_AUDIT.md` (429), `EXECUTION_REPORT.md` (373) are all
historical/partly-stale planning docs. `MISSION_PROMPTS.md` (376) and `AGENT_PROMPTS.md` (719)
are agent-prompt scratchpads. `repomap.md` (737) duplicates what a generated map should own.

| Action | Files | Rationale |
|--------|-------|-----------|
| **REWRITE** | `AGENTS.md` | Fix the dead pointers (→ `CLAUDE.md`), fix tool count, make it the canonical agent router |
| **KEEP (canonical)** | `README.md`, `ARCHITECTURE.md`, `HOW_TO.md`, `SECURITY_NOTES.md`, `CHANGELOG.md`, `CLAUDE.md` (+ per-dir `CLAUDE.md`), `docs/PROPOSAL_codex_appserver_parity.md` | User + contributor + agent facing; current |
| **MERGE** into one `ROADMAP.md` | `IMPROVEMENT_PLAN.md`, `HARNESS_ROADMAP.md`, `PRODUCTION_ROADMAP.md`, `REFACTOR_ROADMAP.md` | 4 overlapping backlogs → 1 sequenced plan (this document seeds it) |
| **ARCHIVE** to `docs/archive/` | `DEEP_DIVE_AUDIT.md`, `EXECUTION_REPORT.md`, `REVIEW_GUIDE.md`, `handoff.md` | Historical snapshots; keep for provenance, off the root |
| **DELETE/regenerate** | `MISSION_PROMPTS.md`, `AGENT_PROMPTS.md`, `repomap.md` | Prompt scratchpads (not docs); `repomap.md` should be generated, not hand-maintained (it references dead `run_codex_subtask`) |

Effort **M** total. Δ +2.0 (docs are cheap to fix). Add a CI doc-link-check step so pointers
can't silently rot again (**S**, +0.5).

---

## Sequenced 4-phase execution program

Each phase is a coherent, shippable milestone with entry/exit gates. Total ≈ **26–34
agent-days** to reach ~9/9.5 across the board.

### Phase 0 — De-risk & clean (entry: green CI on the working branch) — ~3–4 days
**Scope:** K4 (delete `run_codex_subtask` + rewrite its 3 grep-tests behaviorally, Q1/Q4/T1
subset), docs consolidation (all of Dimension 6), A5/S6 product decision on relay/voice/dead
commands, Q6 file-size CI gate.
**Exit criteria:** dead code gone; env contradiction resolved to the single hardened path; the
3 codex grep-tests are now behavioral (spawn fake binary, assert argv+env dict); `AGENTS.md`
pointers resolve; root `.md` count ≤ 10; full pytest + cargo + vitest green.
**Buys:** quality 6.5→7.5, docs 6→8.5.

### Phase 1 — Security spine (entry: Phase 0 merged) — ~7–9 days
**Scope:** K1 (`mcp_server/policy.py` + `call_tool` enforcement, S1), S2 shell allowlist, S3
arg path-validation, S4 minimal-env everywhere. Behavioral tests for policy
(allow/deny/approve matrix) + the argv-rule table. K2 **starts** here because the server-side
approval emit needs the decomposed spawn/stdin path.
**Exit criteria:** every MCP tool call passes through `policy.enforce`; a renamed `rm`, a
`python -c` one-liner, and `cp ~/.ssh/id_rsa /tmp` are all blocked or gated by tests; no tool
inherits full parent env; approval round-trip works from both GUI and CLI callers.
**Buys:** security 5.5→8.

### Phase 2 — Architecture & testability spine (entry: Phase 1 merged) — ~8–10 days
**Scope:** K2 finish (`TaskSpec`/`EnvBuilder`/`SpawnSupervisor`, A1/A3 unify dual spawn), K3
(`PageDriver` seam in `BrowserProvider`), K5 (event-manifest codegen for log lines, A2),
A6/R-2 typed-IPC flip, A4 `provider.rs` split. Then the testing payload: T2 (Rust integration
dirs), T3 (recorded-DOM browser contract tests), T4 (proxy golden tests), T5 (`useChatStream`
tests), T6 (local E2E smoke), T7 (CI coverage gate). Q2/Q3/Q5 file splits ride along.
**Exit criteria:** `send_task` < 150 lines orchestrating named builders; one `TaskRuntime` owns
both spawn paths; `BrowserProvider.complete()` accepts an injected driver;
`tests/fixtures/dom/` exists with ≥3 recorded snapshots and passing contract tests;
`useChatStream` has a Vitest suite; CI runs integration + coverage; live-app verification of
the IPC flip done.
**Buys:** architecture 7→9, quality →8.5, testing 6.5→9, robustness partial (Rb2/Rb5).

### Phase 3 — App-server parity (entry: Phase 2 merged; this is
`PROPOSAL_codex_appserver_parity.md` Parts 0–5) — ~8–12 days
**Scope:** exactly the parity proposal — feature flag + schema snapshot + probe (Part 0),
`codex_engine/app_server.py` JSON-RPC client with fake-server tests (Part 1), app-server
transport in `codex_bridge_service.py` with sidecar thread-id + native approvals over the
existing stdin channel (Part 2, the keystone part), typed Kim events (Part 3, extends K5),
CLI + Tauri approval/plan/diff UX (Parts 4–5). Fold in Rb1 (proxy re-emit repair, §7.1), Rb3
(auth-wall detection), Rb6 (per-turn relay cap), S5 (retire bypass flag).
**Exit criteria:** with `transport: app-server`, a real "make pong.html and open it" task
completes end-to-end with native per-command approval, session resumes across messages
(persisted `codex_thread_id`), legacy exec path still green; browser is now *model-only*, tool
execution + approvals are native + sandboxed.
**Buys:** security 8→9, robustness 7→9, architecture →9.5. This is the phase that lifts the
browser-premise ceiling.

---

## What "as close to 10 as possible" looks like at the end

| Dimension | Now | After P0–P3 | Hard ceiling | Why not 10 |
|-----------|-----|-------------|--------------|------------|
| Architecture | 7 | 9.5 | 10 | reachable |
| Code quality | 6.5 | 9 | 10 | reachable with sustained hygiene |
| Testing | 6.5 | 9 | ~9.5 | live-browser E2E is inherently flaky; recorded-DOM contract tests are the mitigation |
| Security | 5.5 | 9 | ~9.5 | LLM prose → JSON parser surface remains; app-server sandbox mitigates |
| Robustness/UX | 7 | 9 | ~9 | scraping a foreign UI can't be fully deterministic; re-emit + protocol events mitigate |
| Docs | 6 | 9 | 10 | reachable; keep the link-check gate |

**The through-line:** K4 clears the landmine, K1+K2 build the security+arch spine, K3+K5 unlock
testing and IPC, and the app-server migration (Phase 3, already fully designed in
`docs/PROPOSAL_codex_appserver_parity.md`) is what converts the browser from an untrustworthy
tool-executor into a trustworthy model-transport — which is the only way security and
robustness get past ~8.

---

## Notes for the implementer

- Key files to start in: `mcp_server/server.py:122` (K1 hook), `mcp_server/tools/shell.py:39-359`
  (S2), `desktop/src-tauri/src/subprocess.rs:445-1012` (K2), `codex_engine/engine.py:118-280`
  (K4 delete), `orchestrator/providers/browser/provider.py` (K3+Rb),
  `docs/PROPOSAL_codex_appserver_parity.md` (Phase 3, use as-is).
- The 3 grep-tests to rewrite first (they block the security fix by pinning the weaker env
  contract): `tests/test_codex_env_scoping.py`, `tests/test_codex_bridge_tool.py`,
  `tests/test_codex_process_cleanup.py`.
- Existing assets to reuse rather than rebuild: `scripts/gen-events.js` codegen (K5),
  `code.py:_minimal_env` (S4 pattern), the `hitl_respond_approval`↔`_await_hitl_decision`
  stdin round-trip (K1 approval flow), `response_parser.py` purity + injection guard (T3/Rb1),
  `tests/fixtures/golden_transcript.json` pattern (extend to DOM fixtures).
