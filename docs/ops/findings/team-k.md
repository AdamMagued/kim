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

*(Continued below as confirmed — flake census, test-quality audit, coverage hot-spots, cli test serialization, ratchet plan.)*
