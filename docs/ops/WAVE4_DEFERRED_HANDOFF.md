# Wave 4 deferred work — continuation handoff

## Branch state

- Branch: `wave-4/deferred`
- Base: `main` at `d1b71e3`
- Push status: **pending conductor push**
- Pull request: not opened
- Full branch gate: not run
- `.claude/` is untracked, user-owned state. Do not edit, stage, or delete it.
- Do not rewrite accepted commits. Every new worker result still requires a separate adversarial reviewer and an explicit PASS.

Before continuing, read the root `AGENTS.md`, `ARCHITECTURE.md`, `ROADMAP.md`, the relevant directory `CLAUDE.md`, the issue body, and its referenced file under `docs/ops/findings/`.

## Issue status and accepted commits

| Issue | Status | Accepted commits / evidence |
|---|---|---|
| #52 | Checklist accepted; live QA **PENDING/BLOCKED** | `6533f10`, `fdd92ae`. The running Tauri app still needs human/E2E execution; do not claim it passed. |
| #53 | Implementation accepted; hosted run pending | `7a68c91`, `8e348c0`, `e720fe7`. Windows Actions evidence is unavailable until push and workflow execution. |
| #54 | **OWNER DECISION** | Design accepted in `721034b`, `9285e02`. Do not implement signing/key custody without explicit owner approval. |
| #55 | Accepted | `a84bfaf`, `de839d9`, `52d70b8`, `b0e70fb`, `f7dc0c4`, `b895870`. Zero-behavior-change refactors; CLI binary gate passed 192 tests and `cargo check`. In `cargo test -p kim-cli -- --test-threads=1`, 192 unit tests passed before `cli_flow`; managed runs then passed 0/9 `cli_flow` tests in the sandbox and 1/9 when escalated. The reviewer attributed the failures to unavailable browser/Python bridge and provider-gate/non-git environment categories; this handoff does not independently establish baseline certainty. |
| #56 | E/F/G accepted; H BLOCKED | A: `a139501`, `35c7e3a`; B: `fde266c`, `780600a`; C: `dc4e7af`, `b4f78e8`; D: `4b6d922`; E: `7623ccb`; F: `6e0ff0a`; G: `6bb145f` (each with separate adversarial reviewer PASS, 2026-07-13). Hosted workflows remain pending. |
| #57 | **DONE — all three units accepted** (2026-07-13) | Unit 1 (F-L-7/F-L-4): `484be0a` — generated `docs/ENVIRONMENT.md` (56 KIM_* vars, 39 hand-verified descriptions, `scripts/gen_env_reference.py` with `--check`) + de-hardcoded counts in README/CLAUDE.md/AGENTS.md. Unit 2 (F-L-2/5/6/8/9/13): `c291a8c` + round-1 fix `c89bdfa` — HOW_TO pointer truth (8 rots fixed), mcp_server/CLAUDE.md tables regenerated, src-tauri python-resolution order corrected, README quick-install + Troubleshooting, cli.py log-name comment. Unit 3 (F-L-14): `9630ead` + round-1 fix `ced38a9` — status headers on all 7 proposals (evidence-derived), A6 shipped annotation in ROADMAP_TO_10, archive provenance banners (16 files). Each unit reviewer-PASSed; units 2 and 3 each took one correction round. |
| #58 | **NOT UPDATED** | GitHub CLI was unavailable. Update the tracker only after accepted outcomes are final. |

### #56 remaining work

- #56-E (F-K-12): **ACCEPTED** `7623ccb` — comment-only documentation at all three `--test-threads=1` sites. Root cause proven empirically (reviewer independently reproduced 3/5 parallel failures): `cli/src/config.rs:213-221` names test temp dirs by `SystemTime` nanos alone, so same-microsecond tests share a dir and `remove_dir_all` teardown deletes a sibling's dir mid-save. Same hazard latent in `sessions.rs`/`provider.rs`/`file_refs.rs` test helpers. Flag retained.
- #56-F (F-K-2 follow-up): **ACCEPTED** `6e0ff0a` — `coverage-ratchet.json` (per-package floors: orchestrator 72, mcp_server 68, codex_engine 68, kimctl 68, total 70; measured macOS 74.5/70.7/70.7/70.8/72.7, floor(measured)−2 for Linux platform skew) + stdlib-only `scripts/check_coverage_ratchet.py` + CI wiring; flat `--cov-fail-under=61` dropped (total floor 70 subsumes it). Reviewer-noted risk: codex_engine margin is ~2.5 pts — if the first hosted Linux run trips a floor, do a one-time documented calibration correction, not a silent edit.
- #56-G (F-K-3 follow-up): **ACCEPTED** `6bb145f` — pyrightconfig ratchet: `tests/` included (standard mode, 8 rules as warning), global basic→standard, `reportMissingImports` none→warning; CI step now prints warning count (310 at commit). Net-tighter everywhere except one justified carve-out: `orchestrator/agent.py` has `reportAttributeAccessIssue`/`reportArgumentType` file-scoped to warning because main's pyright gate was ALREADY RED (CI run 29215483024: mcp 1.28.1 types flag 5 latent errors at agent.py:1416/:2349). Proper fix for a future wave: fix agent.py types or pin mcp minor in requirements.txt, then delete the exec env. Measured strict-mode debt: 13,484 errors repo-wide (burn-down input).
- #56-H: **BLOCKED by territory — recorded, not dispatched (2026-07-13).** The required regression test belongs in `desktop/src-tauri/src/http_bridge/mod.rs`, which is Rust source and outside the config/CI-only authorization. No exception was granted this session. Obtain explicit expanded authority before dispatching it.
- #56-D verification caveat: the reviewer passed the config diff. The full Python suite was stopped after more than 90 seconds and at least three failures; no complete result was obtained. Diff review proved pytest selection was unchanged. Do not report a green full pytest run.
- #56-A/#56-C and Windows CI have no hosted proof until the branch is pushed and Actions run.

### #57 proposed scoped units

Read `docs/ops/findings/team-l.md`, then dispatch and review these separately:

1. Canonical/generated environment documentation: replace hand-maintained environment facts and counts with generated or single-source references where possible.
2. Onboarding/log truth audit: reconcile setup, logging, and operator-facing claims with actual behavior.
3. Roadmap/proposal/archive status audit: clearly label current, proposed, completed, superseded, and archived material without rewriting history.

## Branch gate + hosted CI results (2026-07-13, conductor session 2)

Local full gate on macOS (recorded substitutions: `./venv/bin/python` for `.\venv\Scripts\python.exe`; `cd` subshells for Push-Location; `npx`/`npm` for the `.cmd` wrappers):

- pytest: **2388 passed, 3 skipped, 0 failed** (a first-pass failure of `test_github_create_repo.py::test_private_visibility_unresolved_fails_compactly` was environmental — Playwright updated but chromium-1223 never installed; `./venv/bin/playwright install chromium` fixed it and the full suite re-ran green).
- desktop: tsc 0, eslint 0, vitest 279 passed, vite build OK.
- desktop cargo test: all green. cli cargo test (serial): 196 + 9 green.
- `git diff --check main...HEAD`: clean.

Hosted CI (`workflow_dispatch` on the ref — note `wave-4/deferred` matches NO ci.yml push pattern, so pushes get zero CI; dispatch manually or add the pattern): run 29256442298 = 2 green (File-size gate, Frontend) / 4 red, all classified:

- **Python (lint+test)**: pyright now PASSES on CI (the #56-G ratchet + agent.py carve-out worked). Failure moved to the next pre-existing layer: 7 flake8 style hits (E305/E306/E501×2/E261/W293) in orchestrator files **byte-identical to main** — inherited, previously masked because main fails at pyright first. Fix needs .py style edits (out of this branch's scope).
- **Windows smoke (#53's job, first real hosted run)**: 47 pytest failures / 2332 passed — genuine pre-existing Windows incompatibilities (WindowsPath vs POSIX in config hardening tests, cp1252 `charmap` decode in test_invariants, `_filtered_env` dropping HOME, policy allow/deny divergence, codex fake-binary spawn differences). The job is doing exactly what F-K-1 wanted: making Windows breakage visible. Burn-down is future-wave product work.
- **Rust (check+clippy+test)**: 2 clippy `-D warnings` errors in code byte-identical to main (lib.rs:1 unused `base64::Engine` import; subprocess.rs:163 `manual_checked_ops` — a lint NEW in clippy 1.97; hosted stable is newer than the local 1.94 toolchain). Inherited; needs desktop Rust source edits (out of scope).
- **Rust CLI (fmt+check+test)**: `cargo fmt --check` diff introduced by the accepted #55 refactor commits — the one branch-caused failure. **FIXED** in `7503b1a` (pure `cargo fmt` output, reviewer-verified byte-for-byte reproducible; 196+9 tests green). Second dispatch (run 29257503786) verifies.

`nightly-contract.yml` and `rust-coverage.yml` (#56-A/#56-C) **cannot produce hosted runs from this branch at all**: GitHub registers dispatchable/scheduled workflows only from the default branch, and these files exist only here. `gh workflow run` returns 404 for them. Hosted proof requires merging to main first — recorded as a structural limitation, not a failure.

## Invariants

Every worker and reviewer prompt must preserve all nine:

1. Code tab never uses OpenAI authentication or GPT-5.5; only Ollama Cloud or the browser provider.
2. Shell/sandbox deny-lists remain code-owned.
3. `desktop/src/index.css` import order is unchanged.
4. Examples inside system-prompt f-strings use doubled braces (`{{ }}`).
5. Secret-file sandbox globs and sensitive-directory protections are not weakened.
6. Stdout markers `[STATUS]`, `[PLAN]`, `[STEP]`, `[DONE]`, `[CONTEXT]`, and `[UI]` are preserved.
7. The `[END_OF_RESPONSE_{id}]` sentinel is preserved.
8. HITL remains a hard block.
9. The Codex CLI text protocol is preserved.

## Exact continuation commands

Start by confirming the branch and accepted history:

```powershell
git status --short
git branch --show-current
git log --oneline --decorate main..HEAD
git diff --check main...HEAD
```

After #56-E/F/G, authorized #56-H if approved, #57, and #58 are accepted, run the full branch gate once from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest tests/
Push-Location desktop; npx.cmd tsc --noEmit; npm.cmd run lint; npm.cmd run test; npm.cmd run build; Pop-Location
Push-Location desktop/src-tauri; cargo test; Pop-Location
Push-Location cli; cargo test -- --test-threads=1; Pop-Location
git diff --check main...HEAD
```

The commands above use the repository venv interpreter directly; if it is unavailable, record the exact substitution and do not fabricate results. After an authorized conductor push, verify every hosted workflow, including Windows and coverage jobs, before opening a PR. `gh` is unavailable in this environment, so GitHub Actions verification and tracker updates require the GitHub UI or a machine with GitHub CLI installed:

```powershell
gh run list --limit 10
gh run watch <run-id> --exit-status
```

Do not push or open a PR unless the conductor/user explicitly authorizes it.
