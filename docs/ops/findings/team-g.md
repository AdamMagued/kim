# Team G — Wave 1 findings (satellites & repo hygiene)

Territory: codex_engine/, pythonExperimentTool/, relay_server/, scripts/, root configs
(config.yaml, install.sh, justfile, requirements.txt). Branch: integration/audit-fixes.

Status: PRELIMINARY PASS (fast first commit). Deep evidence follows in later commits.

## SATELLITE VERDICTS (preliminary)

### relay_server/ — ALREADY DELETED (verdict: no action / keep guard test)
- The directory does not exist on this branch: `ls relay_server` → "No such file or directory".
- Decommissioned 2026-07-06 in `9f6371f` ("A5/S6: decommission relay + voice scaffold +
  dead Tauri commands; Q6 file-size CI gate").
- Only remaining reference in code: `tests/test_invariants.py` (to verify: likely a
  stays-deleted guard). Docs references remain in docs/ (fine).
- Impact: 0 — work already done. Charter question "keep-with-tests or archive?" is moot.

### codex_engine/ — KEEP (preliminary; ownership+size audit pending)
- 3.4M on disk, last touched 2026-07-10 (`cb24cc4`, cross-process locking fix) — actively
  maintained, not stale.
- Referenced by production code outside itself:
  `orchestrator/codex_appserver_transport.py`, `orchestrator/codex_bridge_service.py`,
  `orchestrator/events_gen.py`, `desktop/src-tauri/src/schedule_commands.rs`,
  `scripts/probe_appserver.py`, `scripts/gen-events.js`.
- Test coverage exists: 15+ test files reference it (test_appserver_*, test_codex_*,
  test_e2e_smoke, codex_bridge_harness, ...).
- Open question (next commit): what makes it 3.4M — code vs vendored data/binaries.

### pythonExperimentTool/ — SPAWN PATH CONFIRMED; dir == claw-code entirely
- `pythonExperimentTool/` contains exactly one child: `claw-code/` (8.9M, 246 tracked
  files). There is NO "rest" to carve claw out of — the carve-vs-delete fork in the
  charter collapses to a single KEEP/QUARANTINE/DELETE decision on claw-code itself.
- Spawn path EXISTS in current code: `desktop/src-tauri/src/subprocess.rs`
  - L531-532: bundled backend roots `"pythonExperimentTool/codex-code/rust/target"`
    (Codex) and `"pythonExperimentTool/claw-code/rust/target"` (Claw).
  - L565-569: resolution chain `CLAW_BIN` env → bundled claw binary → `claw` on PATH.
  - L856: user-facing error tells users to "build pythonExperimentTool/claw-code/rust
    ... or set CODEX_BIN/CLAW_BIN".
  - Claw is the FALLBACK when no Codex binary is found; browser-backed Code mode
    explicitly rejects Claw (L862-863).
- Note: `pythonExperimentTool/codex-code/` does NOT exist in the tree even though
  subprocess.rs looks for it (F-G finding to be formalized).
- Preliminary verdict: cannot flat-delete (would break the documented Code-tab fallback
  and the bundled-binary resolution). Deep pass will weigh QUARANTINE (bundle a prebuilt
  binary / move source out of repo) vs KEEP. ~86k LOC (43% of repo) is at stake.

## FINDINGS (F-G-N) — to be expanded

(placeholder — formal findings follow in subsequent commits)
