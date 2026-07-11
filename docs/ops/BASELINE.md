# Operation Google-Level — Baseline snapshot (Wave 0)

**Commit:** `integration/audit-fixes` @ `6f69adb` (== `origin/main`)
**Date:** 2026-07-11 · **Machine:** macOS (darwin 25.5.0), Python 3.x venv, cargo stable
Every later wave measures against this "before" photo.

## LOC per top-level dir (source lines, incl. blanks/comments; excludes venv/node_modules/target/dist/.claude/sessions/logs)

| dir | py | rs | ts | tsx | css | total |
|---|---|---|---|---|---|---|
| pythonExperimentTool | 2,908 | 86,053 | 0 | 0 | 0 | 88,961 |
| desktop | 0 | 15,686 | 6,074 | 13,653 | 7,022 | 42,435 |
| tests | 31,544 | 0 | 0 | 0 | 0 | 31,544 |
| orchestrator | 15,600 | 0 | 0 | 0 | 0 | 15,600 |
| cli | 395 | 10,584 | 0 | 0 | 0 | 10,979 |
| mcp_server | 10,573 | 0 | 0 | 0 | 0 | 10,573 |
| codex_engine | 2,713 | 0 | 0 | 0 | 0 | 2,713 |
| kimctl | 1,140 | 0 | 0 | 0 | 0 | 1,140 |
| scripts | 450 | 0 | 0 | 0 | 0 | 450 |
| **TOTAL** | **65,323** | **112,323** | **6,074** | **13,653** | **7,022** | **204,395** |

Note: `pythonExperimentTool/` alone carries 86k LOC of vendored Rust (claw-code) — 43% of the
repo's source lines. Team G's existential question in the master plan is well-founded.

## Test counts (all suites run locally at this commit)

| Suite | Command | Count | Result |
|---|---|---|---|
| Python | `pytest tests/ --collect-only` | 2,057 collected | 2,053 passed · 3 skipped · **1 flake** · 46 subtests |
| Vitest | `cd desktop && npm run test` | 242 tests / 15 files | all pass |
| Rust desktop | `cd desktop/src-tauri && cargo test` | 164 (158 unit + 6 integ) | all pass |
| Rust CLI | `cd cli && cargo test` | 183 (177 unit + 6 integ) | all pass |

**Flake found during baseline:** `tests/test_codex_process_cleanup.py::TestCodexProcessLifecycle::test_timeout_kills_the_codex_subprocess`
failed once in the full-suite run, passes in isolation and within its own file → Team K flake-census seed.

## Coverage (pytest --cov=orchestrator --cov=mcp_server, line)

- **TOTAL: 70%** (11,496 stmts, 3,450 missed)
- Weakest hot spots: `providers/ollama.py` 50%, `tool_utils.py` 46%, `ui_bridge.py` 61%,
  `providers/openai_provider.py` 64%, `providers/browser/provider.py` 68%.
- Not measured: Rust (tarpaulin/llvm-cov not installed — skipped per Wave-0 time budget), vitest
  coverage (vitest runs but `--coverage` provider not configured).

## Static-analysis warning counts

| Tool | Scope | Count |
|---|---|---|
| pyright 1.1.411 | repo config (pyrightconfig.json) | **0 errors, 0 warnings, 0 informations** |
| cargo clippy | desktop/src-tauri (default lints) | **0 warnings** |
| cargo clippy | cli (default lints) | **0 warnings** |
| eslint | desktop/ | **N/A — eslint is not configured** (no eslint.config.*/.eslintrc anywhere; no CI job). CI's TS gate is `tsc --noEmit` + vitest; Python lint gate is flake8. This is a G3 gap for Wave 4/R1. |

## Dependency counts

| Manifest | Count |
|---|---|
| requirements.txt | 22 (non-comment lines) |
| desktop/src-tauri/Cargo.toml | 20 (deps + dev + build) |
| cli/Cargo.toml | 12 |
| desktop/package.json | 7 dependencies + 12 devDependencies |
| (vendored) pythonExperimentTool/claw-code | own Cargo workspace — not counted; Team G decides its fate first |

## Hunt toolchain — installed & recorded (Wave 0 §5)

| Tool | Status | Version |
|---|---|---|
| vulture (py dead code) | installed in `venv/` | 2.16 |
| pip-audit (py CVEs) | installed in `venv/` | 2.10.1 |
| radon (py complexity) | installed in `venv/` | 6.0.1 |
| cargo-machete | **missing** (`cargo install cargo-machete` when needed) | — |
| cargo-udeps | **missing** (needs nightly; defer) | — |
| knip | **missing** (npx would fetch knip@6.26.0; not pre-installed) | — |
| ts-prune | **missing** (npx ts-prune@0.10.3 available on demand) | — |
| depcheck | **missing** (npx depcheck@1.4.7 available on demand) | — |
| cargo audit / npm audit | cargo-audit not installed; `npm audit` available with the stock npm | — |

Per rules, no large installs were performed; Wave-1 teams D/F should `cargo install cargo-machete`
/ `npm i -D knip ts-prune depcheck` (or npx -y) inside their own runs and record versions here.

## Repo hygiene state after Wave 0

- `.gitignore` verified to cover `venv/`, `logs/`, `graphify-out/`, `kim_sessions/`, `sessions/`
  (+ new `/Library/`) — enforced forever by `tests/test_gitignore_hygiene.py` (13 tests, runs in CI's pytest job).
- Untracked `Library/` (macOS com.apple.python pyc cache, 196K) deleted + gitignored.
- Untracked `handoff.md` archived to `docs/archive/HANDOFF_dev_bughunt_2026-07-03.md` (its only
  live item is tracked as GitHub issue #51; its "failed attempts" section is useful dispatch intel).
- Out of scope but flagged: the PARENT folder `kimFork/` (outside the repo) is full of stray HTML
  mockups, screenshots, and zips — user cleanup, not repo work.
