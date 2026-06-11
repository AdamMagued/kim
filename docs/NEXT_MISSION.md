# NEXT MISSION — final two items on `production-roadmap` (V-4 splits + II-G proposal)

> For the executing agent: this file is your full mission brief. Delete this file in
> your final commit when the mission is complete.

You are continuing prior sessions' work with no context. The repo root is this
directory (`kim-pro`). The root `CLAUDE.md` is a short router with standing
constraints — read it plus `HOW_TO.md`, and load per-directory `CLAUDE.md` guides only
as you touch each area.

## Step 0 — Confirm the baseline (before any edit)
1. `git branch --show-current` → must be `production-roadmap`; `git status` → clean;
   in sync with origin. HEAD should be `bfc1be3` or a descendant.
2. Remote CI must be green: `gh run list --branch production-roadmap --limit 1`
   (expected: success). If red, fixing it is your first task. If you cannot reach
   GitHub Actions from your environment, note that in the report and rely on the
   local suites, but say so explicitly.
3. Run all four suites and record numbers (Python needs the project venv or
   `pip install -r requirements.txt` plus pytest/pytest-asyncio):
   - `python -m pytest tests/ -q` → ~885+ passed
   - `cd desktop && npx tsc --noEmit && npm run test`
   - `cd desktop/src-tauri && cargo test`
   - `cd cli && cargo test` → 90 passed  ← separate crate, easy to forget; CI runs it
   Record these as your before-refactor baseline. Not green → stop and report.
4. Read `EXECUTION_REPORT.md` (current status) and `PRODUCTION_ROADMAP.md` Part V
   (V-4) and Part II-G. Note: V-1 through V-8 and the legacy IPC kill are DONE — do
   not redo them. Typed `kim:*` events are now the only protocol on the Kim-agent
   path; the Codex CLI path still uses text/JSONL deliberately.

## Item 1 — V-4: final decomposition (pure refactors, ZERO behavior change)
One commit per sub-item, in this order:

a. **`desktop/src/components/ChatView.tsx`** (~3,300 lines) → container plus
   `MessageList`, `ActivityFeed`, `Composer` components and a `codexEvents.ts` module
   (the Codex JSONL `item.completed` parsing deserves its own file + tests). Follow
   the pattern used for the RevampSettings split
   (`components/kim-ui/settings-panes/`). After: `npx tsc --noEmit`, vitest, and a
   production `npm run build` must pass.

b. **`orchestrator/agent.py`** (~1,800 lines): extract the run-loop body into named
   phase methods (perceive / decide / act / settle or similar), and move the
   stuck-detection cluster (screenshot signatures, loop guard, repeated-action
   tracking) into `orchestrator/stuck_detection.py`. Mind the local invariants in
   `orchestrator/CLAUDE.md` (f-string doubled braces; all exits via AgentTermination;
   stdout is the IPC channel — nothing prints directly).

c. **`desktop/src/styles/chat.css`** (~2,400 lines) → per-component files under
   `desktop/src/styles/`. CSS import order is load-bearing (cascade) — add the new
   files to the `index.css` manifest preserving exact order, and update the
   order-check test. While splitting, delete only selectors you can PROVE are dead
   (grep the class name across desktop/src with zero hits); when unsure, keep.

Refactor discipline for all three: test counts identical before/after; no public
behavior, props, event names, or CSS specificity changes; if a piece resists clean
extraction, leave it in place and note it in the report rather than forcing it.

## Item 2 — II-G: proposal document (writing only, NO code changes)
Create `docs/PROPOSAL_code_tab_backend.md` comparing three options for the Code tab
backend: (a) keep Codex CLI as-is, (b) bundled claw only, (c) replace both with the
Kim agent itself running a code-tools-only toolset (code.py, git.py, files.py,
search.py already exist in mcp_server/tools/). For each: architecture sketch,
migration steps, what gets deleted, risks, and effort estimate. End with a
recommendation. Constraint that must hold in every option: the Code tab must NEVER
use OpenAI auth or gpt-5.5 — only ollama cloud or browser provider. Commit it.

## Rules (non-negotiable)
- Standing constraints in root `CLAUDE.md` apply at all times.
- `just check` before every commit (or the four suites manually if `just` is
  unavailable); all FOUR suites before every push.
- **After every push, confirm the remote CI run completes green.** Local green is not
  done — this branch once hid 18 commits of CI failure behind a broken workflow file.
  Do not start the next sub-item while the previous push's run is red.
- Never weaken, skip, or delete a test to get green. Never delete code without
  grepping for consumers first.
- Timebox: if a sub-item fights back hard, mark it BLOCKED in the report with
  specifics and move on. A clean partial split beats a broken full one.
- Do NOT merge into `kim-improvement` or `main` — a human merges after final review.
- Work directly on `production-roadmap` (or a branch off it that you PR back into
  `production-roadmap` if your environment requires PRs — never target main).

## Reporting
Update `EXECUTION_REPORT.md` after every sub-item: status, files, commit hash, exact
verification commands with real output summaries, remote CI conclusion. If you must
stop mid-item, write `HANDOFF.md` (exact git state, verbatim commands for the
half-finished step, remaining queue) and stop cleanly. Final message = faithful
summary of EXECUTION_REPORT.md: final counts for all four suites,
`git log --oneline kim-improvement..production-roadmap`, and anything a reviewer
should double-check. Claims without evidence in the report count as not done.
Delete this file (docs/NEXT_MISSION.md) in your final commit.
