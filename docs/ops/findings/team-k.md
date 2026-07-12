# Team K — Tests & CI (Wave 1 findings)

**Territory:** `tests/`, `desktop/src/**/*.test.*`, cli tests, `.github/workflows/`, pyrightconfig.json, lint configs, justfile.
**Baseline:** BASELINE.md @ `6f69adb` — pytest 2,057 · vitest 242 · cargo desktop 164 · cli 183 · py coverage 70% · 1 known flake.
**Status:** IN PROGRESS (preliminary pass committed early per resilience protocol; findings appended as confirmed).

---

## F-K-1: No Windows CI job — the app ships to Windows but CI never compiles or tests there
- **File:** .github/workflows/ci.yml (all four jobs: `runs-on: ubuntu-latest`)
- **Severity:** High
- **Class:** test-gap
- **Evidence:** release.yml has a `windows-latest` matrix leg that builds the desktop app and `kim.exe` — but it runs only on version tags or manual dispatch. ci.yml has zero Windows coverage: no `cargo check/test` on windows-latest, no pytest, nothing. The codebase carries Windows-specific logic (per-user Chrome path resolution, CRLF handling in `cli/src/provider/codex_stream.rs`, `pywin32` sys_platform deps, path handling in `mcp_server/config.py`). Any Windows-only compile break or behavior regression is discovered at release time or by end users, never by CI. This is exit-criterion G3/Wave-4-R1 material but the compile-check portion is cheap now.
- **Fix sketch:** Add a `windows` job to ci.yml: `cargo check -p desktop -p kim-cli` + `cargo test -p kim-cli` on windows-latest (desktop cargo test may need WebView2 — start with check + CLI tests). Optionally a pytest smoke subset.
- **Cross-territory?** no

## F-K-2: Coverage "gate" is 10 points below reality and is not a ratchet
- **File:** .github/workflows/ci.yml:253-266
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** `--cov-fail-under=60` while measured baseline is **70%** (BASELINE.md). The in-file comment says "The floor is a ratchet — raise it as coverage grows" but nothing enforces raising it; a PR can erase 10 percentage points of coverage (~1,150 statements) and CI stays green. Note also the CI number differs from the baseline number because CI adds `--cov=codex_engine --cov=kimctl` (baseline measured only orchestrator+mcp_server), so the gate's own comment ("measured baseline is ~62%") and BASELINE.md's 70% are measuring different things — nobody can tell at a glance what the real floor is.
- **Fix sketch:** Raise floor to the actual current CI-measured value minus 1; document which cov targets the number covers; Wave 4: replace with a per-file ratchet file (see Wave-4 ratchet plan below).
- **Cross-territory?** no

## F-K-3: pyright CI gate ignores warnings, checks with an unpinned pyright version, and excludes all of tests/ (31.5k LOC)
- **File:** .github/workflows/ci.yml:191-227, pyrightconfig.json
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** (a) The gate filters `severity == 'error'` only — pyright warnings can accumulate unbounded. (b) `pip install pyright` is unpinned: local baseline is 1.1.411, CI gets whatever shipped that morning; a new pyright release can break CI on an unrelated PR (or newly *allow* something locally flagged). (c) pyrightconfig.json includes only `orchestrator, mcp_server, codex_engine, kimctl` — `tests/` (31,544 LOC, the largest Python dir in the repo) is entirely untype-checked, and `typeCheckingMode` is `basic` with `reportMissingImports: none` repo-wide (which also masks genuinely broken imports in first-party code, not just missing third-party stubs).
- **Fix sketch:** Pin `pyright==1.1.411` in the CI install; add `tests` to include (fix fallout); Wave 4: strict mode per-directory ratchet (plan below).
- **Cross-territory?** no

## F-K-4: flake8 lints only orchestrator/ + kimctl/ — mcp_server, codex_engine, tests are lint-free zones
- **File:** .github/workflows/ci.yml:229-240
- **Severity:** Low
- **Class:** test-gap
- **Evidence:** `flake8 orchestrator/ kimctl/` — that's 16.7k of the 65k Python LOC. `mcp_server/` (10.5k, the security-sensitive tool layer), `codex_engine/` (2.7k), and `tests/` (31.5k) get no lint at all. Dead imports, undefined names (F821 — a real bug class flake8 catches), and shadowing in those trees are invisible.
- **Fix sketch:** Extend to `flake8 orchestrator/ kimctl/ mcp_server/ codex_engine/ tests/` with the same config; burn down the initial hits (or start with `--select=F` errors-only for the new dirs).
- **Cross-territory?** no

## F-K-5: eslint does not exist — 19.7k LOC of TS/TSX has no lint gate (folds in Team F handoff F-F-1)
- **File:** desktop/ (no eslint.config.* / .eslintrc* anywhere), .github/workflows/ci.yml frontend job
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** Confirmed at baseline and re-confirmed: zero eslint config in the repo, no `lint` script wired to CI. The only TS gates are `tsc --noEmit` and vitest. tsc does not catch: unused vars beyond noUnusedLocals config, react-hooks/exhaustive-deps violations (Team F's charter predicts stale-closure bugs in useChatStream — exactly the class `eslint-plugin-react-hooks` exists for), floating promises, accidental `any` spread. Team F's F-F-1 asks CI to wire `npm lint` once config lands — this finding is the CI half of that pair.
- **Fix sketch:** Wave 4 R1: add flat-config eslint with `typescript-eslint` recommended-type-checked + `react-hooks` plugin; `npm run lint` step in the frontend CI job; error-budget = 0 from day one (auto-fix + targeted disables with comments).
- **Cross-territory?** yes — config file itself is Team F territory (desktop/); CI wiring is Team K.

## F-K-6: Release signing gaps — installer archives are unsigned, checksums are self-attested, and unsigned releases proceed silently
- **File:** .github/workflows/release.yml:113-141, 260-303
- **Severity:** Medium
- **Class:** security
- **Evidence:** (a) cosign keyless signing covers ONLY the bare CLI binary (`cli-stage.outputs.path`) — the `.tar.gz`/`.zip` archives that `scripts/install-kim.sh`/`.ps1` actually download are NOT signed; their only integrity check is a `.sha256` sidecar uploaded by the same pipeline to the same release (self-attestation: anyone who can replace the archive can replace the sidecar). (b) When `APPLE_CERTIFICATE` is unset the macOS leg prints a notice and exits 0 — a tagged release can ship unsigned/un-notarized desktop bundles with no failure signal. (c) No Tauri updater is configured (`tauri.conf.json` has no updater/pubkey section), so there is no auto-update signature path at all — users re-download installers manually, making (a) the live attack surface. Ties to Team I F-I-1.
- **Fix sketch:** cosign-sign the archives + sha256 sidecars too (3 extra sign-blob lines); make the installers verify the cosign cert (or at minimum pin the expected identity); consider failing tag builds when signing secrets are absent (or requiring an explicit `unsigned: true` input).
- **Cross-territory?** yes — Team I owns the threat-model framing; workflow edits are Team K.

## F-K-7: The real-binary parity suite never runs in CI — fake fixtures can drift unnoticed
- **File:** tests/test_appserver_real_binary.py:18-21, .github/workflows/ci.yml python job
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** `test_appserver_real_binary.py` (the only test that exercises the app-server transport against the REAL codex binary) auto-skips when `codex` is not on PATH — which is always true in CI (`CODEX = shutil.which(...)`; docstring says "Skipped automatically when no codex binary is on PATH (e.g. CI)"). Everything else goes through `fake_app_server.py` / `codex_bridge_harness.py`. So the fixture-vs-reality contract is enforced by nothing: a codex binary protocol change breaks users while the fakes keep CI green. Skips are also silent — nobody notices the suite has never once run in CI.
- **Fix sketch:** Add a (possibly nightly / non-required) CI job that `npm i -g @openai/codex` (or downloads a pinned release) and runs this file un-skipped; at minimum, make the python job print a loud summary of skipped-due-to-missing-binary tests so the gap stays visible.
- **Cross-territory?** no

---

## F-K-8: The git tool's path-validation security gate has ZERO test coverage
- **File:** mcp_server/tools/git.py:28-46 (`_validate_git_paths`), whole-file coverage 28.7%
- **Severity:** High
- **Class:** test-gap | security
- **Evidence:** Fresh coverage run (2,057 tests): `_validate_git_paths` — the function that stops `git diff`/`git add`/etc. from touching paths outside ALLOWED_PATHS (and, via `validate_path`, the secret-file sandbox that CLAUDE.md marks as a standing constraint) — shows lines 36-46 **never executed by any test**. Every git tool handler body (129-235: git_diff, git_add, git_commit, ...) is likewise uncovered. Team C's charter explicitly names the git tool as an arbitrary-command vector; the entire enforcement seam is pinned by nothing. A refactor could silently drop the `validate_path` call and all suites stay green.
- **Fix sketch:** A `test_git_tool_sandbox.py`: for each handler, assert a path outside ALLOWED_PATHS / a secret-file path returns `PERMISSION_ERROR` without spawning git; plus one happy-path per handler against a tmp repo.
- **Cross-territory?** no (tests/), though findings about the gate's logic itself belong to Team C.

## F-K-9: The known flake is root-caused in its own file — the fix pattern was applied to a sibling test but not to the flaky one
- **File:** tests/test_codex_process_cleanup.py:63-78 (`test_timeout_kills_the_codex_subprocess`, `task_timeout_s: 1`) vs :111-128 (sibling using `task_timeout_s: 8`)
- **Severity:** Medium
- **Class:** test-gap (flake)
- **Evidence:** The flaky test gives the fake codex binary a **1-second** whole-run budget. The fake binary is a Python interpreter; under full-suite load its startup can exceed 1s, so the run times out before `pid.txt` is written → `assertTrue(pid_file.exists(), "fake codex never started")` fails. The sibling test `test_timeout_kills_grandchild_shell_subprocess_too` already uses `task_timeout_s: 8` with the in-code comment: "a tight budget races interpreter startup under a loaded machine" — the diagnosis exists in the same file; it was never back-ported to the 1s test.
  **Census status:** did not reproduce in 3 full-suite runs (1 solo + 2 concurrent, 2,054 passed each) nor in a 15× loop under 6-core CPU load — it is a genuine low-probability load flake, consistent with Wave 0 seeing it once. No OTHER nondeterminism found: repo-wide, `time.sleep`-based waiting in tests is limited to bounded 0.05s polls (`_wait_for_death`) — the good pattern; zero snapshot tests; zero raw `setTimeout` waits in vitest tests.
- **Fix sketch:** One line: raise `task_timeout_s` to 5-8 in the flaky test (the assertion is about *kill-on-timeout*, not about the budget being 1s). Optionally have the harness wait for `pid.txt` before starting the budget clock.
- **Cross-territory?** no

## F-K-10: Frontend test map has 800-LOC blind spots and vitest coverage cannot even be measured
- **File:** desktop/vitest.config.ts (no coverage config), desktop/src/components/chat/StreamRenderer.tsx (862 LOC, 0 tests), settings-panes/PaneAI.tsx (693), PaneAccount.tsx (557), WorkedForPill.tsx (514), ConnectorsPanel.tsx (469), ToolCallCard.tsx (346), OnboardingFlow.tsx (305), useSessionLoader.ts (199)
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** 15 test files / 242 tests cover ~20k LOC of TS/TSX. Reference-scan of `__tests__/` shows the files above are imported by no test. StreamRenderer.tsx is the *rendering half* of the stdout-protocol pipeline (useChatStream parses — it does have a 692-line test — but what the user actually sees is untested). RevampSidebar.tsx (1,173 LOC) has only a date-formatting util test. And `@vitest/coverage-v8` is not installed/configured, so unlike Python there is no number to ratchet — exit criterion G4 is currently *unmeasurable* for the frontend.
- **Fix sketch:** `npm i -D @vitest/coverage-v8` + `coverage` block in vitest.config.ts; add StreamRenderer golden-render tests fed by the same transcript fixtures as useChatStream's tests (pairs with Team H §6 plan).
- **Cross-territory?** yes — test authoring is Team F territory; CI wiring Team K.

## F-K-11: Rust coverage has never been measured — subprocess kill paths are unmeasurable blind spots
- **File:** .github/workflows/ci.yml (rust jobs), BASELINE.md ("tarpaulin/llvm-cov not installed — skipped")
- **Severity:** Medium
- **Class:** test-gap
- **Evidence:** 164 desktop + 183 cli tests over 26k LOC of Rust, but no line-coverage has ever been produced (Wave 0 skipped it; CI doesn't run it). The charter's predicted hot spot — subprocess.rs (1,510 LOC) kill/timeout/reaper paths — cannot be confirmed or refuted; nobody knows whether the 164 desktop tests touch them. G4 ("core paths ≥ 80%") is unmeasurable for a third of the codebase.
- **Fix sketch:** `cargo install cargo-llvm-cov` locally, produce the first per-file report, append to BASELINE.md; Wave 4: `cargo llvm-cov --fail-under-lines N` job (or at least report artifact) in CI.
- **Cross-territory?** no

## F-K-12: CLI test suite is parallel-unsafe (`--test-threads=1` in CI and justfile) with no documented reason
- **File:** .github/workflows/ci.yml:167, justfile:61
- **Severity:** Low
- **Class:** test-gap
- **Evidence:** `cargo test -p kim-cli -- --test-threads=1` — the desktop crate runs parallel but the CLI must run serial. No comment in either place says why. The crate has no `env::set_var` in tests; likely culprits are tests sharing `~/.kim/sessions`/cwd-derived paths (sessions.rs resolves `dirs::home_dir()` and `env::current_dir()` at many sites). Serial execution masks whatever the shared-state hazard is (a dev running plain `cargo test -p kim-cli` gets nondeterministic failures — the flag is tribal knowledge) and roughly doubles CI wall time for the job.
- **Fix sketch:** Bisect: run parallel, catalogue collisions, isolate via injected roots (the `save_session_messages_in(root, …)` seam already exists); or at minimum comment WHY at both flag sites.
- **Cross-territory?** fix itself is Team E territory.

## F-K-13: CI's pip install intentionally diverges from the shipped dependency graph (`--no-deps --ignore-requires-python`)
- **File:** .github/workflows/ci.yml:182-214
- **Severity:** Low
- **Class:** test-gap
- **Evidence:** The python job installs `requirements.txt` with `--no-deps --ignore-requires-python`, then hand-reinstalls a curated 15-package subset "to get transitive graph correct for the core subset". So CI validates a hand-maintained approximation, not the real environment: a broken transitive pin, a dep newly required by orchestrator code but missing from the curated list (works via some other package's transitives), or a `requires-python` conflict all pass CI and fail on user machines. Local venv is Python 3.12.12 while CI pins 3.11 — version skew in the *other* direction from pyrightconfig's `"pythonVersion": "3.11"`.
- **Fix sketch:** Split requirements into `requirements-core.txt` (CI installs it WITH deps, no escape hatches) + platform extras; add a weekly job that does the full install to catch drift.
- **Cross-territory?** no

## F-K-14: Registered `slow` pytest marker is used by zero tests — `just check`'s "fast" filter filters nothing
- **File:** pytest.ini:8-9, justfile:22 (`-m "not slow"`)
- **Severity:** Low
- **Class:** test-gap
- **Evidence:** pytest.ini registers `slow: marks tests as slow (require real network, browser, or API keys)`; `grep -rn "pytest.mark.slow" tests/` → **0 hits**. The `just check` fast loop's `-m "not slow"` deselects nothing; the "<30 seconds" target comment sits atop what is actually the full 85s suite. Harmless today, but the marker convention everyone assumes exists is a no-op — the first genuinely network-needing test added without the mark will break offline CI.
- **Fix sketch:** Either mark the actually-slow files (evals/, real_binary) or delete the marker and the `-m` filter so the justfile stops promising a fast path it doesn't have.
- **Cross-territory?** no

---

## Test-quality audit — verdict: unusually clean, three small smells

Checked: assertion-free tests (AST scan over all 2,057), mock-the-SUT, snapshot rot, sleep-flakes, order dependence.

- **Assertion-free scan:** 20 raw hits; on inspection 17 are false positives (they delegate to `_assert_denied`-style helpers or use raises-if-broken idioms like `source.encode("ascii")`). Real items: `tests/test_policy_enforce.py:435 test_dump_decision_for_debug` — a `print()`-only pseudo-test (env-gated by `KIM_POLICY_TEST_DEBUG`, so it never runs in CI; move it to a script or delete).
- **Sleep patterns:** all waiting is bounded 0.05s polling (`_wait_for_death`) — correct pattern. The only real-time-budget flake bomb is F-K-9.
- **Snapshot tests:** zero (`toMatchSnapshot` count = 0). No snapshot rot possible.
- **Order dependence:** pytest side — conftest stubs heavy modules (`mss`, `pyautogui`, …) into `sys.modules` process-wide at import; tests that want the REAL modules would order-depend on that, but none currently do. Rust side — the CLI crate's serial-only requirement (F-K-12) IS an order/parallelism dependence, unresolved.
- **Mock-the-SUT:** spot-checked the codex bridge suites — they mock the *proxy* and *binary* boundaries, not the service under test; `codex_bridge_harness.run_bridge` spawns a REAL fake binary and observes REAL pids. Good.

## Coverage hot-spots (fresh run @ this commit — TOTAL 69.7%, 2,054 passed)

Modules ≥100 stmts, ranked by coverage; **bold** = on a security or kill path:

| cov% | stmts | miss | module | note |
|---|---|---|---|---|
| **23.6** | 220 | 168 | **mcp_server/tools/windows.py** | window-control tool, essentially untested |
| **28.7** | 143 | 102 | **mcp_server/tools/git.py** | F-K-8: security gate at 0% |
| 43.7 | 103 | 58 | mcp_server/tools/screen.py | |
| 44.8 | 145 | 80 | orchestrator/providers/browser/bridge_client.py | CDP client |
| 49.6 | 480 | 242 | orchestrator/providers/ollama.py | worst big module |
| 59.2 | 191 | 78 | mcp_server/tools/code.py | run_python/run_node escape hatches |
| 60.3 | 224 | 89 | mcp_server/tools/web/observation.py | |
| **60.4** | 313 | 124 | **mcp_server/tools/shell.py** | missed lines are mostly the **Windows sandbox-env + run_powershell branches** — untestable on mac/linux runners; pairs with F-K-1 |
| 60.7 | 736 | 289 | kimctl/__main__.py | legacy CLI (Team E existential question) |
| 60.8 | 237 | 93 | orchestrator/ui_bridge.py | |
| 63.4 | 153 | 56 | mcp_server/tools/ui_observe.py | |
| 64.4 | 998 | 355 | codex_engine/engine.py | |
| 64.4 | 180 | 64 | mcp_server/tools/search.py | |
| 66.2 | 275 | 93 | mcp_server/tools/web/browser.py | zombie-chromium lifecycle |
| 66.7 | 216 | 72 | mcp_server/tools/web/actions.py | |
| **67.7** | 838 | 271 | **orchestrator/providers/browser/provider.py** | salvage-ladder/reconnect branches in the missed ranges (as the charter predicted) |
| 68.3 | 1022 | 324 | orchestrator/agent.py | biggest absolute miss count |
| 68.4 | 206 | 65 | mcp_server/tools/github.py | |
| 70.1 | 384 | 115 | orchestrator/codex_bridge_service.py | |
| 71.8 | 305 | 86 | orchestrator/providers/gemini.py | |
| **72.6** | 583 | 160 | **orchestrator/codex_appserver_transport.py** | timeout/partial-frame paths |
| 72.7 | 293 | 80 | orchestrator/scheduled_runner.py | |

Unmeasured entirely: all 26k LOC of Rust (F-K-11), all 20k LOC of TS (F-K-10).

---

*(Continued — CI cli-tests question, Wave-4 ratchet plan.)*
