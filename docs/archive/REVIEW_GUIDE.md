> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Reviewer's Guide — `production-roadmap` branch

Hey — thanks for reviewing. This branch is ~34 commits of work on Kim (a desktop AI
agent: Tauri 2 + React 19 frontend, Python orchestrator, MCP tool server) that we want
a second pair of human eyes on before merging to main. Everything below tells you what
changed, how to verify it, and where the bodies would be buried if there are any.

## TL;DR of what this branch does

1. **13 correctness bugs fixed** (agent exit paths, provider message formatting,
   regex parsers, a Rust unwrap panic, etc.)
2. **New capability:** `web_fill_form` — fills an entire web form in one LLM
   round-trip instead of 7+, with a resolver that has an eval suite
3. **God files split:** `lib.rs` 4090→~2100, `ChatView.tsx` 3300→486,
   `RevampSettings.tsx` 2449→278, `index.css` 6790→manifest, `chat.css` 2448→7 files,
   `agent.py` decomposed
4. **Legacy IPC killed:** the Kim-agent path now emits only typed JSON events
   (`kim:*`); the old `[STATUS]`-style text protocol survives only for the Codex CLI
   passthrough (deliberate — that path can't be schema'd)
5. **Production plumbing:** approval gates for risky tools, typed error cards,
   session retention/pruning, rotating file logs, cost meter, OS notifications,
   PyInstaller sidecar spec, CI that actually runs (it was broken-at-parse for the
   branch's first 18 commits — true story, see EXECUTION_REPORT.md)
6. **Deliberate removals:** voice subsystem killed (was half-dead), relay pane
   feature-flagged off (code preserved), all recoverable from git history

Full item-by-item evidence: `EXECUTION_REPORT.md`. The plan it executed:
`PRODUCTION_ROADMAP.md`.

## Setup (10 min)

```bash
git clone https://github.com/AdamMagued/kim.git && cd kim
git checkout production-roadmap
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install pytest pytest-asyncio
cd desktop && npm ci && cd ..
```

## Verification pass 1 — the suites (15 min)

Expected results (all verified green at commit `06dd736`, CI also green):

```bash
python -m pytest tests/ -q          # expect: 927 passed, 8 skipped
cd desktop && npx tsc --noEmit && npm run test   # expect: 73 passed, tsc silent
npm run build                        # expect: clean production build
cd src-tauri && cargo test           # expect: 54 passed
cd ../../cli && cargo test           # expect: 90 passed  (separate crate!)
```

Also check the remote: `gh run list --branch production-roadmap --limit 3` — should
be green. If anything above deviates, that alone is a finding.

## Verification pass 2 — run the actual app (20 min)

```bash
cd desktop && npm run tauri dev
```

- Send a trivial task with whatever provider you can configure (Ollama local is the
  zero-credential option; `provider: fake` exists for offline testing — `KIM_FAKE=1`)
- Watch the activity feed during a run — this exercises the new typed-event-only path
  (the riskiest change on the branch). Tool cards, plan checklist, context ring, and
  the final result card should all render. Anything blank/missing = the IPC kill
  (commit `5a8268f`) broke a consumer we missed.
- Open Settings — every pane should render (they were split into separate files).
  There should be NO Voice pane and NO Relay pane (both removed/flagged off on purpose).
- After a run completes: cost chip on the "worked for" pill, OS notification fires.

## Verification pass 3 — where to actually look hard (the risky diffs)

In priority order:

1. **`git show 5a8268f`** (V-1, legacy IPC kill) — the highest-risk change. Question
   to answer: is anything in `desktop/src/` still expecting a text format that the
   Python side no longer emits? The seam test (`git show e0aca6d`) is supposed to
   guarantee this; check whether you believe its fixture covers the real protocol.
2. **`git show a08e1b4` and `4a47108`** (agent.py decomposition) — refactor claimed
   to be zero-behavior-change. Skim for anything that changed semantics: reordered
   awaits, swallowed exceptions, altered early returns.
3. **`git show 72fca3e`** (ChatView split) — same question, React edition: lost
   effects, changed hook order, props that silently became optional.
4. **chat.css split** (`beedf1f`) — already machine-verified byte-identical when
   reassembled in import order, so just confirm the UI doesn't look broken in dev.
5. **`git show f8a3e37`** (approval gates) — security-relevant: can a high-risk tool
   call execute *without* the gate when the mode says it should ask? Check the
   threshold plumbing in `orchestrator/interaction_policy.py` + `tool_risk.py`.
6. **Session pruning** (`ac10b77`) — destructive code path: confirm retention
   only deletes what it claims (date-bucketed JSONL under `kim_sessions/`) and the
   "delete all" can't escape that directory.

## Known/deliberate things — do NOT file these as findings

- **MessageList extraction is PARTIAL** (documented in EXECUTION_REPORT.md) — ~250
  lines of JSX left in StreamRenderer on purpose; extracting needed an ugly 10-var
  props bag.
- **Codex CLI path still uses the legacy text/JSONL protocol** — intentional, it
  cannot be schema-first'd.
- **`PairingModal.tsx` qrcode.react type warning** — pre-existing, known.
- **Unsigned builds, no auto-updater, no LICENSE file** — human-blocked items
  (certs/keys/decisions), tracked in EXECUTION_REPORT.md, not regressions.
- **Voice/relay removal** — product decisions, recoverable from history.
- **Standing constraint you should *verify* rather than question:** the Code tab must
  never use OpenAI auth or gpt-5.5 — there's a test for it (`tests/test_invariants.py`).

## How to report

Whatever format you like, but per finding: file/commit, what you expected, what you
saw, severity (blocks-merge / should-fix / nit). If all three passes come back clean,
say so explicitly — "verified, no findings" is the signal we need to merge.
