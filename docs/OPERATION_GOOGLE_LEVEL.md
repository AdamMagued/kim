# OPERATION GOOGLE-LEVEL — Total Quality Campaign for Kim

**Status:** PLANNED (dispatch-ready)
**Baseline:** `integration/audit-fixes` @ `6f69adb` (== `origin/main`)
**Date:** 2026-07-11
**Repo scale:** ~58k LOC Python · ~26k LOC Rust · ~20k LOC TS/TSX · ~7k LOC CSS · 60+ test files · 2 CI workflows

This is the master plan to take Kim from "very good side project" to "an engineer at Google would sign off on this." It is organized as **waves of parallel agent teams**, each with a non-overlapping territory, an explicit hunt checklist, deliverables, and a verification gate. Any agent can be handed one team charter and work independently.

---

## 0. Definition of Done ("Google-level" means, measurably)

The campaign is complete when ALL of these hold:

| # | Exit criterion | Measured by |
|---|---|---|
| G1 | Zero known correctness bugs of severity ≥ Medium | All Wave-1 findings triaged; all accepted findings fixed & tested |
| G2 | Zero dead code | vulture/knip/cargo-machete clean (with a curated allowlist) |
| G3 | CI is a real gate | pyright **strict** on core dirs, `clippy -D warnings`, eslint errors=0, all suites required to merge |
| G4 | Coverage ratchet in place | Line coverage measured in CI; ratchet file forbids regression; core paths ≥ 80% |
| G5 | Every subsystem has an owner doc | Per-directory CLAUDE.md accurate + one ARCHITECTURE.md with diagrams |
| G6 | Security posture audited | Threat-model doc; injection/traversal/secrets sweeps clean; sandbox gates tested |
| G7 | No process/resource leaks | Every spawn has a reaper; every timeout kills the process group; leak tests exist |
| G8 | Cross-language contracts pinned | IPC/bridge protocols have schema docs + golden tests on both sides |
| G9 | Repo hygiene | No orphan dirs, no committed junk, .gitignore complete, branches pruned |
| G10 | A stranger can ship in 30 min | README quickstart verified from scratch on a clean machine (KIM_FAKE=1 path) |

---

## 1. Ground truth — what is ALREADY done (do not re-burn this ground)

Prior campaigns have fixed a lot. Hunt teams must read this so they chase new game, not ghosts:

- **audit-fixes campaign** (merged): ~244 fixes, ~500 new tests, god-file splits done (agent.py, lib.rs, http_bridge.rs), pyright errors = 0.
- **dev-branch audit** (merged, `c0525b4`): 24 findings all fixed (issues #22–#45 closed) — fake ollama sign-in, TaskRuntime race, typed-IPC failure-text loss, codex proxy instruction drop, etc.
- **shell/sandbox hunt** (merged @ `4b3e992`): inline env-assignment bypass, /dev/null redirect over-blocking, credentials.json glob gap — all fixed, #46–#48 closed.
- **latency/hang/preferred-site/timeout bugs** fixed @ `6483dea`.
- **Stateful browser threads + ChatGPT terminal mode** merged @ `327bad2`.

### Known UNFIXED debt (this is Wave-2 seed material — free kills)

1. **Cobweb-plumbing hunt (~25 findings, REPORTED ONLY, never fixed)** — found on `feat/roadmap-to-10` @ `cf319b7`. Includes: scalar `allowed_paths` → `/` full-filesystem access, null-config-section boot crash, client/server tool-timeout double-execution. **First task of Wave 0: locate the findings report on that branch and re-validate each against current main.**
2. **REFACTOR_ROADMAP.md runtime-contract refactors** + risky-runtime checklist — designed, never executed, needs app-level testing sign-off.
3. **ROADMAP_TO_10.md** phases (K1–K5 keystones) — the feature roadmap; this campaign is orthogonal (quality, not features) but Wave 4 should align with it.
4. `docs/PROPOSAL_codex_appserver_parity.md` — open design work.

---

## 2. Rules of engagement (every agent, every wave)

1. **Territory discipline.** You edit ONLY files inside your team's territory. If a fix requires touching another team's files, write it up as a handoff finding instead. This is what makes parallel dispatch merge-safe.
2. **One branch per team**, named `ops/<wave>-<team>`, e.g. `ops/w2-orchestrator`. Branch from the integration baseline commit, not from each other.
3. **Findings before fixes.** Wave 1 is read-only. Every finding goes in `docs/ops/findings/<team>.md` using the format in §3. Wave 2 fixes only triaged-accepted findings.
4. **Every fix ships with a test** that fails before and passes after. No test, no merge. (Exceptions: pure deletions, docs.)
5. **Green gate:** `just check` equivalent — pytest, vitest, `cargo test` (desktop + cli), pyright, clippy, eslint — all green before requesting merge. Use `KIM_FAKE=1` offline mode for anything needing app behavior without live LLMs.
6. **Behavioral invariants that must NEVER regress:**
   - Code tab must never use OpenAI API auth or gpt-5.5 (user hard rule).
   - `BrowserProvider` sentinel `[END_OF_RESPONSE_{id}]` protocol.
   - `[STATUS]`/`[PLAN]`/`[STEP]`/`[DONE]`/`[CONTEXT]`/`[UI]` stdout log-line protocol (frontend parses it live).
   - HITL hard-block self-enforcement (commit `7f37580`).
   - Shell/sandbox blocklist gates (issues #46–#48 fixes).
   - Codex CLI path must keep the text protocol (V-1 constraint).
7. **Rust changes require a `tauri dev` restart to test** — no hot reload.
8. **Severity scale:** Critical (security/data loss) · High (crash/wrong result on common path) · Medium (wrong result on edge path, leak) · Low (smell, perf micro, style).

### §3 Finding format

```markdown
## F-<TEAM>-<N>: <one-line title>
- **File:** path:line
- **Severity:** Critical/High/Medium/Low
- **Class:** bug | dead-code | security | race | leak | perf | contract | test-gap | docs
- **Evidence:** what the code does, concrete failing scenario
- **Fix sketch:** 1–3 lines
- **Cross-territory?** yes/no (if yes: which team owns the fix)
```

---

## WAVE 0 — Recon & Hygiene (1 agent, sequential, ~half a day)

Runs alone BEFORE parallel dispatch so every later team starts from a clean field.

**Branch:** `ops/w0-recon`

1. **Resurrect the cobweb findings.** `git show`/`git log` on `feat/roadmap-to-10` @ `cf319b7` to find the ~25-finding report; re-validate each against current main; file surviving ones into `docs/ops/findings/inherited.md` pre-triaged.
2. **Repo hygiene sweep:**
   - `Library/` (196K, untracked, at repo root — almost certainly accidental macOS junk): investigate and delete or ignore.
   - `handoff.md` (untracked at root): triage — keep, move to docs/, or delete.
   - Verify `.gitignore` covers: `venv/`, `logs/`, `graphify-out/`, `kim_sessions/`, `sessions/` (it does today — assert with a test or CI check so it stays true).
   - Root of the PARENT folder (`kimFork/`) is full of stray HTML mockups, screenshots, zips — out of repo scope but flag to user.
3. **Branch graveyard:** 20+ stale local/remote branches (`add-shell-validation-tests-…`, `code-health-…`, `copilot/…`, etc.). Produce a delete/keep list for user sign-off. Do NOT delete without approval.
4. **Baseline metrics snapshot** into `docs/ops/BASELINE.md`: LOC per dir, test counts per suite, current coverage (run pytest --cov, vitest --coverage, cargo tarpaulin or llvm-cov if feasible), pyright/clippy/eslint warning counts, dependency counts. This is the "before" photo every wave measures against.
5. **Install the hunt toolchain** and record versions: `vulture` (py dead code), `cargo-machete` + `cargo-udeps` (rust), `knip` + `ts-prune` + `depcheck` (TS), `pip-audit` + `cargo audit` + `npm audit` (CVEs), `radon` (py complexity).
6. **Create `docs/ops/` scaffolding**: findings/, triage board (TRIAGE.md), this plan linked from README.

**Gate:** BASELINE.md exists; inherited findings triaged; hygiene PRs merged.

---

## WAVE 1 — The Great Hunt (12 teams, ALL PARALLEL, read-only)

Read-only ⇒ zero merge conflicts ⇒ dispatch all simultaneously. Each team writes `docs/ops/findings/<team>.md` only.

Teams A–G are **territory hunters** (own a directory, hunt everything in it).
Teams H–L are **cross-cutting hunters** (own a bug class, sweep the whole repo for it).
A finding found by both is fine — triage dedupes.

---

### TEAM A — Orchestrator Core (Python)
**Territory:** `orchestrator/` EXCLUDING `providers/` — agent.py (2,360 LOC), session_store.py (982), codex_appserver_transport.py (1,051), codex_bridge_service, memory.py, compaction.py, context_meter.py, context_loader.py, task_queue.py, checkpoint/run-lifecycle modules.

**Hunt checklist:**
- Agent loop state machine: every exit path routes through completion; cancellation mid-tool-call; exception → session-file consistency.
- Session store: JSONL corruption on crash mid-write (atomic writes?), date-bucket rollover at midnight, concurrent session access, summary regeneration idempotency.
- Memory/compaction: token-count drift vs real tokenizer, screenshot pruning ordering (recent fix `bbe044d` walked back through tool-results — hunt for sibling bugs in the same trim logic), compaction losing tool_use/tool_result pairing (breaks Anthropic API!).
- Context meter: cache-key correctness (two recent fixes here — `6f69adb`, `88ada4a` — smell of a fragile area; audit the whole cache design).
- codex_appserver_transport: request/response ID matching, timeout paths, partial-frame handling, reconnect behavior.
- Task queue: ordering, starvation, duplicate execution, cancellation races.
- asyncio hygiene: un-awaited coroutines, tasks created without exception handlers, `asyncio.gather` without `return_exceptions` reasoning, event-loop-blocking sync calls (file IO, subprocess) in async paths.
- Dead code: run `vulture orchestrator/ --min-confidence 80`; verify each hit by hand.

### TEAM B — Providers (Python)
**Territory:** `orchestrator/providers/` — base.py, claude.py, openai_provider.py, gemini.py, deepseek.py, ollama.py, `browser/` package (provider.py 1,745 LOC + siblings).

**Hunt checklist:**
- Contract conformance: every provider returns identical shapes for text/tool_call/error/empty; write a conformance matrix (this was V-3, never finished — flag gaps as findings).
- Retry/backoff: which providers retry, on what codes, with what jitter; hunt for retry-on-non-idempotent and infinite-retry paths.
- Streaming: partial chunk handling, mid-stream error surfacing, sentinel parsing in browser provider (`[END_OF_RESPONSE_{hash}]` — what if the model echoes the sentinel early or mangles it?).
- Browser provider deep-dive (the gnarliest file): CDP reconnect, thread/session-scoped state leakage between runs, selector drift resilience for Claude/ChatGPT/Gemini/Grok, auth-wall detection false positives, salvage-ladder correctness, stuck-detection thresholds.
- Auth flows: token refresh races, expired-key error messages (actionable vs cryptic), key material never logged.
- API drift: model IDs, deprecated params, response fields each SDK/REST call assumes — verify against current provider APIs.
- Dead providers/params: config keys nothing reads, model entries nothing selects.

### TEAM C — MCP Server & Tools (Python)
**Territory:** `mcp_server/` — server.py, tool_registry.py (1,089 LOC), config.py, logger.py, `tools/` (files, shell, keyboard, mouse, screen, windows, web, ui_observe, code, git, search, codex_bridge, sites/).

**Hunt checklist:**
- **Safety gates round 3** (rounds 1–2 fixed inline-env bypass, /dev/null over-block, credentials glob): command substitution `$(…)`/backticks, `;`/`&&` chaining past validators, `bash -c` wrapping, symlink escapes from allowed_paths, unicode/homoglyph path tricks, `git` tool as arbitrary-command vector (`git -c core.fsmonitor=…`, hooks), `run_python`/`run_node` as trivially unsandboxed escapes — document the intended trust model even where "allowed by design."
- allowed_paths semantics: the inherited scalar→"/" finding; path normalization (.., ~, relative, Windows drive letters, UNC).
- Tool registry: dispatch on unknown tool, schema validation of args, timeout enforcement (inherited double-execution finding!), result truncation limits, error → MCP error mapping (never a hung client).
- web.py Playwright tools: browser lifecycle leaks (zombie chromium), page-closed races, `web_wait_for` unbounded waits, screenshot temp-file cleanup.
- keyboard/mouse/screen: coordinates out of bounds, multi-display math, permission-denied (macOS accessibility) error quality.
- sites/ connectors (guc_cms, guc_mail): dead? gated? credentialed safely?
- Config: null/missing-section handling (inherited boot-crash finding), unknown-key warnings, env-var override precedence documented.
- Dead tools: registered but unreachable, or reachable but no caller in any prompt/agent path.

### TEAM D — Desktop Rust Backend
**Territory:** `desktop/src-tauri/src/` — subprocess.rs (1,510 LOC), lib.rs (1,354), browser_bridge.rs (1,118), http_bridge modules, google_oauth.rs, provider_auth.rs, build.rs, tauri.conf.json, capabilities/permissions.

**Hunt checklist:**
- Subprocess lifecycle: every spawn has a kill path; timeout kills the **process group** (recent fix `5957659` did this for codex exec — audit every OTHER spawn site for the same bug); zombie reaping; stdout/stderr reader threads exiting cleanly; back-pressure when python floods stdout.
- The /v1/send–/v1/result HTTP bridge: port collision handling, auth between app↔bridge (can another local process hit it? that's a finding), request body limits, error propagation to UI (typed-IPC failure-text fix `c0525b4` — hunt siblings).
- Tauri command surface: every `#[tauri::command]` audited for: input validation, panics reachable from frontend input (`.unwrap()`/`.expect()`/indexing on user data), blocking calls on the main thread, path args escaping intended roots.
- lock discipline: `Mutex`/`RwLock` held across `.await`, lock ordering, poisoned-lock handling.
- google_oauth/provider_auth: token storage (Keychain usage correct?), refresh races, secrets in logs.
- tauri.conf.json + capabilities: CSP, asset scope, shell scope, updater config — least privilege?
- build.rs KIM_COMPILE_TIME_ROOT baking: what breaks when the app is moved/installed vs dev tree?
- `unsafe` blocks (if any): justify or remove. `clippy::pedantic` dry-run: harvest the signal, ignore the noise.
- Dead code: `cargo machete` (unused deps), `#[allow(dead_code)]` inventory — each one is a finding (justify or delete).

### TEAM E — Rust CLI
**Territory:** `cli/` — main.rs (2,155 LOC), commands.rs (1,684), provider/codex_stream.rs (993), the other ~90-test crate files. Also `kimctl/` (Python, 1,139 LOC `__main__.py`) since it's the same product surface.

**Hunt checklist:**
- CLI/desktop drift: same task through `kim` CLI vs desktop app — enumerate behavior differences (HITL was disabled in CLI once, fixed; hunt for the next drift).
- codex_stream.rs: JSONL frame parsing on truncated/interleaved/oversized lines, backpressure, CR/LF-on-Windows handling.
- Session-id generation (millisecond-collision bug was fixed once — verify and hunt neighbors).
- Signal handling: Ctrl-C mid-run cleans up child processes and writes session state.
- Arg parsing: conflicting flags, `--help` accuracy vs actual behavior, exit codes (0 on failure anywhere?).
- kimctl vs cli/ overlap: are these two CLIs? If kimctl is legacy → dead-code case for deletion; if not → document the split.
- god-file check: main.rs 2,155 + commands.rs 1,684 — propose split plan (execute in Wave 2 only if low-risk).

### TEAM F — Frontend (React/TS/CSS)
**Territory:** `desktop/src/` — RevampSidebar.tsx (1,173 LOC), useChatStream.ts (1,076), ChatView + kim-ui components, hooks, types, 7k LOC CSS across ~15 files.

**Hunt checklist:**
- useChatStream: the stdout-protocol parser — fuzz it mentally against interleaved/partial/malformed `[STATUS]`/`[PLAN]`/`[STEP]`/JSONL lines; state-machine dead ends (plan card stuck open, spinner forever); event-listener leaks on unmount; stale-closure bugs on session switch mid-stream.
- Race: user switches session while a task streams — do events from the old run bleed into the new view?
- React hygiene: missing/wrong dependency arrays, setState-after-unmount, unnecessary re-render hotspots (profile the chat with 500+ messages), missing keys, uncontrolled↔controlled flips.
- Error UX: every Tauri `invoke()` rejection — surfaced to user or swallowed? Inventory each call site.
- Types: `any` census (each one a finding), `as` casts hiding real mismatches, types/index.ts drift vs what Rust actually emits (pairs with Team H contract work).
- Dead code: `knip` + `ts-prune` — unused components (design-mocks wired to nothing?), unused hooks, unused CSS (audit the 15-file cascade for selectors matching nothing; note cascade ORDER is load-bearing).
- Accessibility pass: keyboard nav through chat/settings, focus traps in modals, contrast on both themes, aria on interactive divs.
- CSS: duplicated tokens vs CSS variables, hardcoded colors bypassing the theme, z-index anarchy.

### TEAM G — Satellites & Legacy
**Territory:** `codex_engine/` (engine.py 2,035 LOC, 3.4M tracked!), `pythonExperimentTool/` (8.9M tracked!), `relay_server/`, `scripts/`, root-level configs (config.yaml, install.sh, justfile, requirements.txt).

**Hunt checklist:**
- **The existential question per satellite: should this exist?** For each: who imports/spawns it (grep the whole repo), when it last changed, whether tests cover it. Verdict: KEEP (then it gets real ownership + tests) / QUARANTINE (move to archive/ with a README) / DELETE (recoverable from git).
- `pythonExperimentTool/` is 8.9M of tracked code with "Experiment" in the name — memory says claw-code inside is the Code-tab fallback backend. Verify that spawn path still exists in current code; if yes, carve claw out and delete the rest; if no, delete it all.
- `codex_engine/` 3.4M tracked: what's data vs code? Vendored binaries or models in git → findings (git history bloat).
- relay_server: feature-flagged off since June — dormant code rots; decide keep-with-tests or archive.
- install.sh: run it in a container/VM mentally — error handling, idempotency, macOS vs Linux paths.
- requirements.txt (46 lines): pin audit — unpinned deps, unused deps (cross-check imports), known-CVE versions (`pip-audit`).
- config.yaml: every key — read by code? documented? sane default? (pairs with inherited config findings).

### TEAM H — Contracts & IPC (cross-cutting, read-only)
**Owns the seams, not the dirs.** The #1 source of production bugs in this architecture is the four process boundaries.

**Hunt checklist per boundary:**
1. **Frontend ⇄ Rust (Tauri invoke/events):** enumerate every command + event; build a table of TS type vs Rust struct; every mismatch/optionality difference is a finding.
2. **Rust ⇄ Python (stdout protocol + HTTP bridge):** the `[TAG]` line protocol — write the grammar down (it exists only in two parsers' heads); find emit sites that violate it; find parse sites that guess.
3. **Python ⇄ MCP server (stdio JSON-RPC):** tool schemas vs actual arg handling; error-shape consistency.
4. **codex bridge (proxy ⇄ codex binary ⇄ browser provider):** OpenAI-format fidelity of `_CodexProxy`, instruction-drop class of bugs (one already fixed — hunt siblings), appserver transport vs docs/PROPOSAL_codex_appserver_parity.md gaps.

**Deliverable extra:** `docs/CONTRACTS.md` — the authoritative schema doc for all four seams + a golden-transcript test plan (finishes the abandoned V-3).

### TEAM I — Security & Trust (cross-cutting, read-only)
**Hunt checklist:**
- Secrets: full-repo sweep for keys/tokens in code, logs, session JSONLs (do session files capture API keys from env dumps?), git history (`gitleaks`).
- Injection: everything Team C finds plus — prompt injection via scraped browser-LLM responses driving tool calls (the codex bridge executes shell with `--dangerously-bypass-approvals-and-sandbox`!); a malicious webpage read by web_text steering the agent. Write the **threat model doc**: what do we trust, what do we not, what's the blast radius of each tool.
- Local attack surface: HTTP bridge on localhost (any auth?), CDP port 9222 (any local process can drive the browser), MCP stdio (fine), updater signature verification in release.yml.
- Filesystem: everything the app writes (sessions, logs, config, temp Codex homes) — permissions, world-readable secrets, predictable temp paths.
- Dependencies: `pip-audit`, `cargo audit`, `npm audit` — triage every hit.
- **Deliverable extra:** `docs/THREAT_MODEL.md` + prioritized hardening list.

### TEAM J — Concurrency, Resources & Performance (cross-cutting, read-only)
**Hunt checklist:**
- Process census: every spawn site in all three languages → table: who spawns, who kills, what happens on parent crash. Orphan chromium/python/codex processes are a known user-visible wart class.
- File handles/temp files: every `open()`/`tempfile`/screenshot write → paired cleanup? Run the app under `lsof` deltas for a 10-task session.
- Memory: unbounded growth candidates — message arrays in useChatStream, screenshot buffers in memory.py, session caches in Rust, log files without rotation.
- Latency: instrument (or reason through) the send-task→first-token path; enumerate sync-blocking calls on hot paths; startup time (what's eagerly imported/initialized that could be lazy).
- Timeouts: build the timeout table — every network/subprocess/wait call and its timeout (or lack). "No timeout" on anything user-facing = finding.
- Races: revisit every `threading`/`asyncio`/`tokio` shared-state site not already covered by A/D; TaskRuntime clear() race was real once — hunt the pattern, not the instance.

### TEAM K — Tests & CI (cross-cutting)
**Territory:** `tests/`, `desktop/src/**/*.test.*`, cli tests, `.github/workflows/`, pyrightconfig.json, lint configs, justfile.

**Hunt checklist:**
- Coverage mapping: per-module coverage report → rank the 20 most under-tested critical modules (predict: browser provider salvage paths, subprocess.rs kill paths, useChatStream parser).
- Test QUALITY audit: tests that assert nothing meaningful, tests that mock the thing under test, snapshot tests nobody reads, `time.sleep`-based flake bombs, tests order-dependent on shared fixtures.
- Flake census: run pytest 5× (`-p no:randomly` off/on), vitest 5×, cargo 5× — anything non-deterministic is a finding.
- CI audit: does ci.yml actually FAIL on pyright/clippy/eslint issues or just run them? Are cli tests still CI-only? Cache correctness, matrix gaps (Windows! — the app targets Windows but does CI build it?), release.yml signing/updater path dry-run.
- Strictness gap analysis: current pyright config scope vs full-strict; clippy allow-list inventory; eslint disabled-rule inventory. Produce the ratchet plan for Wave 4.
- Fixture realism: fake_app_server.py / codex_bridge_harness.py drift vs real binaries (test_appserver_real_binary.py exists — does CI run it?).

### TEAM L — Docs, DX & Product Polish (cross-cutting)
**Hunt checklist:**
- Truth audit: README, HOW_TO.md, per-directory CLAUDE.md, FEATURE_FLAGS.md, all docs/PROPOSAL_*.md — every claim checked against code; stale claims are findings (stale docs are worse than none).
- Onboarding test: follow README on a clean checkout, note every stumble, missing prereq, and undocumented env var. The `venv` missing → instant-task-error gotcha MUST be a documented (or auto-detected) failure with a friendly message.
- Error-message quality sweep: grep for user-facing error strings — cryptic, blame-y, or dead-end messages (no "what to do next") are findings.
- Logging audit: levels used consistently? Can a user produce a support bundle? logs/ rotation?
- docs/archive/ + ROADMAP_PROGRESS.md: consolidate; one living roadmap, everything else archived.
- justfile completeness: `just setup`, `just check`, `just dev`, `just test-all` — the four verbs a stranger needs.

---

**Wave 1 gate:** All 12 findings files delivered → **TRIAGE session** (you + one agent): dedupe, accept/reject, assign each accepted finding to a Wave-2 team, rank by severity. Output: `docs/ops/TRIAGE.md` — the single work queue.

---

## WAVE 2 — The Fix (7 parallel fix teams, worktree-isolated)

Fix teams map 1:1 to territories A–G. Cross-cutting findings (H–L) were assigned to territory owners at triage. All teams branch from the same post-triage baseline; territory discipline (§2.1) guarantees mergeability.

| Team | Branch | Fixes | Also executes |
|---|---|---|---|
| A' Orchestrator | `ops/w2-orchestrator` | All accepted A/H/I/J findings in orchestrator/ | REFACTOR_ROADMAP runtime-contract items that fall in-territory |
| B' Providers | `ops/w2-providers` | B findings | Provider conformance suite (from Team B matrix) — finishes V-3 |
| C' MCP | `ops/w2-mcp` | C findings incl. inherited cobweb config/path items | Safety-gate regression test pack |
| D' Desktop Rust | `ops/w2-desktop` | D findings | Process-group-kill audit fixes; HTTP bridge auth if triaged in |
| E' CLI | `ops/w2-cli` | E findings | main.rs/commands.rs split if triage approved |
| F' Frontend | `ops/w2-frontend` | F findings | Dead CSS purge, `any` elimination, a11y quick wins |
| G' Satellites | `ops/w2-satellites` | G verdicts | Deletions/archives (BIG diffs — merge this branch FIRST while others rebase easily, or LAST in isolation; deletions touch nothing others edit if G territory was respected) |

**Per-team definition of done:** every assigned finding closed with linked commit + test; suites green locally; findings file updated with fix commit hashes; no cross-territory edits (CI check: `git diff --stat` vs territory glob).

**Merge order:** G' (deletions) → C' (safety) → A' → D' → B' → E' → F'. One integrator agent owns the merges, runs full suites after each, and bisects on any breakage.

---

## WAVE 3 — Integration & Live Verification (2 agents, mostly sequential)

1. **Integrator:** merge train per order above into `ops/integration-w2`; full suite after every merge; CI green remotely.
2. **Live-app verifier** (Sonnet + Playwright, like the roadmap-to-10 flow): scripted end-to-end passes on the REAL app —
   - Normal agent task (screenshot→tool loop) with KIM_FAKE=1 and with one real provider.
   - Code-tab codex-bridge run against a scratch project (browser provider path).
   - Session save/load/switch mid-stream; cancel mid-task; kill -9 the app mid-task and verify recovery.
   - Settings round-trip for every pane; theme switch; provider switch.
   - Process-leak check: `pgrep` census before/after 10 tasks.
   - The 30-minute-stranger test (G10) from a clean clone.
3. Regression findings loop back as hotfix commits on the integration branch.
4. **Merge to main** — only with user sign-off (standing rule).

---

## WAVE 4 — The Excellence Ratchet (3 parallel teams, post-merge)

What separates "all bugs fixed" from "Google-level": the machinery that keeps it that way.

### Team R1 — CI/Quality Infrastructure
- Coverage in CI with a **ratchet file** (coverage may only go up); fail PRs that lower it.
- pyright → strict on orchestrator/ + mcp_server/ (expand from current scoped-basic); burn down remaining suppressions.
- `clippy -D warnings` with a curated, commented allow-list; eslint strict + `@typescript-eslint` recommended-type-checked.
- Dead-code detectors (vulture/knip/machete) as CI jobs with allowlists — G2 stays true forever.
- Windows CI job (build + unit tests) — the app ships to Windows; CI must prove it builds there.
- PR template + CODEOWNERS mapping the team territories; branch protection requiring green CI.
- Commit-lint or lightweight conventional-commit check.

### Team R2 — Observability & Reliability
- Structured logging (one format across py/rust/ts), log rotation, `kim support-bundle` command.
- Crash reporting hook points (Sentry DSN is human-blocked — build the seam, flag it off).
- Health self-check on startup: venv present, python version, chrome/CDP reachable, config valid — every failure produces a user-actionable message in the UI (kills the #1 runtime gotcha class).
- Metrics seam: task duration, tokens, tool latency histograms → local JSONL now, exporter later.

### Team R3 — Architecture & Docs Canon
- `docs/ARCHITECTURE.md`: the four-process diagram, the four contracts (from Team H's CONTRACTS.md), data-flow for both agent modes.
- ADR directory (`docs/adr/`): retro-ADRs for the 6 biggest standing decisions (browser-provider design, codex proxy, stdout protocol, session JSONL format, MCP tool trust model, dual CLI).
- Public API discipline: `__all__` in python packages, `pub(crate)` sweep in Rust, barrel exports in TS.
- CONTRIBUTING.md wired to the justfile verbs; release checklist doc (finishes what's not human-blocked in release eng).

---

## Dispatch cheat-sheet (copy-paste team assignments)

| Wave | Agents | Parallel? | Est. effort each |
|---|---|---|---|
| W0 Recon | 1 | — | 0.5 day |
| W1 Hunt | 12 (A–L) | ✅ all at once, read-only | 0.5–1.5 days each (B, C, D are the heavy ones) |
| Triage | you + 1 | — | 0.5 day |
| W2 Fix | 7 (A'–G') | ✅ worktrees, territory-disciplined | 1–3 days each |
| W3 Integrate | 2 | mostly sequential | 1 day |
| W4 Ratchet | 3 (R1–R3) | ✅ | 1–2 days each |

**Suggested agent prompt skeleton (per team):**
> You are Team <X> of Operation Google-Level for the Kim repo (`kim-pro/`). Read `docs/OPERATION_GOOGLE_LEVEL.md` §2 (rules of engagement) and your team charter in §Wave-1 Team <X>. Your territory is <paths>. [Wave 1: You are READ-ONLY; produce `docs/ops/findings/<x>.md` in the specified format.] [Wave 2: Fix exactly the findings assigned to you in `docs/ops/TRIAGE.md`; every fix needs a failing-then-passing test; run the full check suite before finishing.] Do not touch files outside your territory; file cross-territory issues as handoff findings.

---

## Scoreboard

Track in `docs/ops/SCOREBOARD.md` after each wave: findings filed / accepted / fixed, LOC deleted, coverage %, warning counts, process-leak count, flake count. The campaign ends when §0's ten exit criteria are all green.
