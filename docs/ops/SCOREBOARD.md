# Operation Google-Level — Scoreboard

Updated after each wave (plan §Scoreboard). Campaign ends when §0's ten exit criteria are green.

| Metric | W0 baseline | after W1 | after W2 | after W3 | after W4 |
|---|---|---|---|---|---|
| Findings filed | 8 (inherited survivors) | | | | |
| Findings accepted | 8 | | | | |
| Findings fixed | 0 | | | | |
| LOC deleted | 0 | | | | |
| Python line coverage (orchestrator+mcp_server) | 70% | | | | |
| pyright errors/warnings | 0 / 0 | | | | |
| clippy warnings (desktop / cli) | 0 / 0 | | | | |
| eslint problems | N/A (not configured — G3 gap) | | | | |
| pytest / vitest / cargo(desktop) / cargo(cli) | 2057 / 242 / 164 / 183 | | | | |
| Known flaky tests | 1 (test_codex_process_cleanup timeout test) | | | | |
| Process-leak count (10-task census) | not yet measured | | | | |
| Stale branches | 184 refs recommended DELETE (awaiting sign-off) | | | | |

## Exit criteria (G1–G10) status

| # | Criterion | Status |
|---|---|---|
| G1 | zero known ≥Medium bugs | ❌ 1 inherited Medium open (F-INH-1) + Wave 1 pending |
| G2 | zero dead code | ❌ detectors not yet run repo-wide |
| G3 | CI a real gate | ❌ eslint absent; pyright not strict; clippy not -D warnings |
| G4 | coverage ratchet | ❌ 70% measured, no ratchet in CI |
| G5 | owner docs | ❌ per-dir CLAUDE.md exist; accuracy unaudited (Team L) |
| G6 | security audited | ❌ threat model doc pending (Team I) |
| G7 | no leaks | ❌ unmeasured (Team J) |
| G8 | contracts pinned | ❌ CONTRACTS.md pending (Team H) |
| G9 | repo hygiene | 🟡 junk removed, gitignore guarded; branch prune awaits sign-off |
| G10 | 30-min stranger | ❌ unverified (Wave 3) |
