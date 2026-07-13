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
| #55 | Accepted | `a84bfaf`, `de839d9`, `52d70b8`, `b0e70fb`, `f7dc0c4`, `b895870`. Zero-behavior-change refactors; CLI binary gate passed 192 tests and `cargo check`. Full `cli_flow` environment failures are documented; do not recast them as refactor failures or successes. |
| #56 | Partially accepted | A: `a139501`, `35c7e3a`; B: `fde266c`, `780600a`; C: `dc4e7af`, `b4f78e8`; D: `4b6d922` (reviewer PASS). Hosted workflows remain pending. |
| #57 | **NOT STARTED** | Split into the three scoped units below. |
| #58 | **NOT UPDATED** | GitHub CLI was unavailable. Update the tracker only after accepted outcomes are final. |

### #56 remaining work

- #56-E, #56-F, and #56-G: **NOT STARTED**. Scope each independently from issue #56 and `docs/ops/findings/team-k.md` before dispatch.
- #56-H: **BLOCKED by territory**. The required regression test belongs in `desktop/src-tauri/src/http_bridge/mod.rs`, which is Rust source and outside the config/CI-only authorization. Obtain explicit expanded authority before dispatching it.
- #56-D verification caveat: the reviewer passed the config diff. The full Python suite was stopped after more than 90 seconds and at least three failures; no complete result was obtained. Diff review proved pytest selection was unchanged. Do not report a green full pytest run.
- #56-A/#56-C and Windows CI have no hosted proof until the branch is pushed and Actions run.

### #57 proposed scoped units

Read `docs/ops/findings/team-l.md`, then dispatch and review these separately:

1. Canonical/generated environment documentation: replace hand-maintained environment facts and counts with generated or single-source references where possible.
2. Onboarding/log truth audit: reconcile setup, logging, and operator-facing claims with actual behavior.
3. Roadmap/proposal/archive status audit: clearly label current, proposed, completed, superseded, and archived material without rewriting history.

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
python -m pytest tests/
Push-Location desktop; npx tsc --noEmit; npm run lint; npm run test; npm run build; Pop-Location
Push-Location desktop/src-tauri; cargo test; Pop-Location
Push-Location cli; cargo test -- --test-threads=1; Pop-Location
git diff --check main...HEAD
```

If the configured Python command is unavailable, use the repository venv interpreter and record the exact substitution; do not fabricate results. After an authorized conductor push, verify every hosted workflow, including Windows and coverage jobs, before opening a PR:

```powershell
gh run list --limit 10
gh run watch <run-id> --exit-status
```

Do not push or open a PR unless the conductor/user explicitly authorizes it.
