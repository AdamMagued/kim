# Team G — Wave 1 findings (satellites & repo hygiene)

Territory: codex_engine/, pythonExperimentTool/, relay_server/, scripts/, root configs
(config.yaml, install.sh, justfile, requirements.txt). Branch: integration/audit-fixes.

## SATELLITE VERDICTS

### pythonExperimentTool/ — DELETE (all 8.9 MB / 88,961 LOC; recoverable from git)

Owner steer asked four questions. Authoritative answers with evidence:

**Q1 — Is it an alternative to codex_engine? What is each one's role?**
They live at DIFFERENT layers; claw is an alternative to the *codex CLI binary*, not to
codex_engine:

- `codex_engine/` is the Python proxy runtime that routes the real OpenAI `codex` CLI's
  model traffic through Kim's BrowserProvider (local aiohttp proxy + app-server client).
  Its own docstring (`codex_engine/engine.py` L1-12): "consumed by the orchestrator-side
  launcher `orchestrator/codex_bridge_service.py` … imports this module as a normal
  sibling package". Spawned as `python -m orchestrator.codex_bridge_service` by
  `desktop/src-tauri/src/subprocess.rs`. It does NOT compete with claw — it wraps codex.
- `pythonExperimentTool/` contains exactly ONE child, `claw-code/` (246 tracked files,
  8.9 MB, 88,961 LOC of .rs+.py). It is a vendored claude-code-style CLI ("claw") whose
  *compiled Rust binary* can serve as a legacy fallback Code-tab backend in direct-CLI
  mode. It also contains a 488 KB Python implementation (`claw-code/src/` — the actual
  "python experiment") that NOTHING in Kim imports — repo-wide grep for imports of it
  outside the vendored tree returns zero code hits (only `tests/claw_test_suite.py`,
  a manual harness, and a TTY-chatter comment in `tests/test_chat_stream_filtering.py`).

**Q2 — Which backend is wired live; is claw reachable?**
The live Code-tab resolution chain is `find_code_backend()` in
`desktop/src-tauri/src/subprocess.rs` L549-570:
`$CODEX_BIN` → `$CLAW_BIN` → bundled codex (`pythonExperimentTool/codex-code/rust/target`)
→ `codex` on PATH → bundled claw (`pythonExperimentTool/claw-code/rust/target/{release,debug}/claw`)
→ `claw` on PATH.

- The bundled-codex directory `pythonExperimentTool/codex-code/` DOES NOT EXIST in the
  tree (see F-G-1) — that arm is dead.
- The bundled-claw arm requires a manually built binary: no `rust/target/` exists
  (`ls … /rust/target` → No such file or directory), root `Cargo.toml` L4
  `exclude = ["pythonExperimentTool"]` keeps it out of the workspace build, and grep of
  `justfile`, `install.sh`, `install.bat`, `kim.sh`, and `.github/workflows/` shows NO
  automation ever builds it. Zero `target/` paths are git-tracked.
- No `config.yaml` key selects a backend (full key audit below) — selection is purely
  env-var + binary discovery.
- Browser-backed Code mode explicitly REJECTS claw (`subprocess.rs` L862-863: "Browser-
  backed Code mode needs the Codex binary…").
- CI excludes claw's tests (`.github/workflows/ci.yml` L260 `--ignore=tests/claw_test_suite.py`)
  and `pytest.ini` L2 excludes the vendored tree.
- Packaging: `tauri.conf.json`, `desktop/package.json`, `kim-orchestrator.spec` contain
  zero claw/pythonExperimentTool references — nothing ships it.

So: **`codex` (PATH or CODEX_BIN) is the only backend reachable in any standard
install.** Claw is reachable only if a developer hand-builds the vendored tree or has an
external `claw` on PATH — nothing the repo ships or documents ever produces that state.
It is latent-dead: the hook exists in Kim's code, the artifact is never provided.

**Q3 — Which to keep?** **Keep the codex path (codex_engine + codex_bridge_service),
DELETE pythonExperimentTool/.** Evidence: codex path last touched 2026-07-10
(`cb24cc4`), covered by 15+ test files, referenced by orchestrator, desktop, and
scripts. pythonExperimentTool last touched 2026-05-24 (`4e437ff`), excluded from
workspace/CI/pytest/packaging, never built by automation. Bonus staleness proof: the
vendored `claw-code/rust/crates/kim-cli/` is an outdated duplicate of the live root
`cli/` crate (`diff -q cli/src/sessions.rs …claw-code/rust/crates/kim-cli/src/sessions.rs`
→ differ; root `cli/` kept evolving, the vendored copy froze). Savings: **8.9 MB tracked
/ 88,961 LOC = ~43% of repo LOC.** Also 4.9 MB of it is README marketing images
(`assets/wsj-feature.png` 876K, `tweet-screenshot.png` 816K, `star-history.png` 316K,
`claw-hero.jpeg` 236K, omx/ screenshots 2.7M) — pure bloat in any scenario.

**Q4 — Carve vs full delete?** Full delete. The "still-used part" is NOT any file inside
pythonExperimentTool — it is the spawn *hook* in Kim's own `subprocess.rs`, which reads
a compiled binary that is never present. Nothing inside the directory is imported, read,
or executed at runtime by Kim. Recommended cleanup package:
1. `git rm -r pythonExperimentTool/` (recoverable from git history).
2. `git rm tests/claw_test_suite.py` (manual harness for the deleted binary; already
   CI-ignored).
3. Follow-up for the desktop owner (Team B/D territory): strip the Claw arms from
   `find_code_backend`, `CodeBackendKind::Claw`, `codex_direct_spec` claw shape,
   `mirror_latest_claw_session_to_codex` (`codex_projects.rs` L203-270), or keep the
   `CLAW_BIN`/PATH fallback for externally installed claw binaries — either way that
   choice is independent of deleting the vendored source, since the bundled-dir lookup
   is the only reference into the repo tree.
   Caution: `bundled_code_backend` also probes `kim_root.parent()`, so a sibling
   checkout could theoretically supply the binary — still nothing the repo ships.

### codex_engine/ — KEEP (needs stated ownership; tests already exist)
- Actively maintained: last commit 2026-07-10 (`cb24cc4`, cross-process locking for the
  thread-state sidecar).
- Production consumers: `orchestrator/codex_appserver_transport.py`,
  `orchestrator/codex_bridge_service.py`, `orchestrator/events_gen.py`,
  `desktop/src-tauri/src/schedule_commands.rs`, `scripts/probe_appserver.py`,
  `scripts/gen-events.js`.
- Test coverage: 15+ suites (test_appserver_bridge/golden/real_binary,
  test_codex_proxy_golden, test_codex_stateful_threads, test_codex_stderr_drain,
  test_app_server_client, test_e2e_smoke, …).
- Size breakdown of the 3.4 MB: code is only ~120 KB (`engine.py` 88K,
  `app_server.py` 20K, `thread_state.py` 12K); **3.1 MB is `appserver_schema/`** —
  ~230 JSON schema files snapshotting the Codex app-server protocol. These are
  load-bearing data, not dead weight: `app_server.py` L53 loads `_SCHEMA_DIR` at
  runtime, `tests/test_appserver_golden.py` replays `SAMPLE_TURN.jsonl`, and
  `scripts/probe_appserver.py` diffs live protocol against the snapshot. No vendored
  binaries/models found in the directory (all .json/.jsonl/.py). Untracked local
  `__pycache__/` only.
- Verdict: KEEP as-is. Optional nice-to-have: note in docs which team owns protocol-
  snapshot refreshes (VERSION file exists for this).

### relay_server/ — ALREADY DELETED (no action; guard exists)
- Directory absent on this branch (`ls relay_server` → No such file or directory).
  Decommissioned 2026-07-06 in `9f6371f` ("A5/S6: decommission relay + voice scaffold +
  dead Tauri commands; Q6 file-size CI gate").
- Only code reference left: `tests/test_invariants.py` (stays-deleted guard).
- Charter question "keep-with-tests or archive?" is moot. But see F-G-3: the `relay:`
  section still lives in config.yaml.

## FINDINGS (F-G-N), most severe first

### F-G-1 — `find_code_backend` probes a directory that does not exist (dead resolution arm, misleading error text)
- **Where**: `desktop/src-tauri/src/subprocess.rs` L531
  (`CodeBackendKind::Codex => "pythonExperimentTool/codex-code/rust/target"`) and the
  user-facing error at L856 telling users to "Build `pythonExperimentTool/codex-code/rust`
  for Codex".
- **What**: `pythonExperimentTool/` contains only `claw-code/`; `codex-code/` does not
  exist anywhere in the tree (`git ls-files | grep codex-code` → 0). The bundled-Codex
  arm can never match, and the error message sends users to build a directory that
  isn't there.
- **Impact**: Medium — misleading operator guidance on the primary failure path of the
  Code tab; dead code arm.
- **Fix shape**: Remove the bundled-codex arm (and, per the pythonExperimentTool
  verdict, the bundled-claw arm), reword the error to "install codex or set CODEX_BIN".

### F-G-2 — Stale CI comment references deleted `run_claw_bridge.py`
- **Where**: `.github/workflows/ci.yml` L235 comment "(incl. run_claw_bridge.py and
  providers/*) stays strict at 120".
- **What**: `run_claw_bridge.py` no longer exists anywhere (repo-wide find → 0 hits;
  it was merged into `orchestrator/codex_bridge_service.py` per that module's
  docstring). Comment describes a lint scope that no longer matches reality.
- **Impact**: Low — doc rot in CI config.

### F-G-3 — `relay:` section survives in config.yaml after relay decommission
- **Where**: `config.yaml` (`relay:` with `pc_api_key`, `poll_interval`, `url`).
- **What**: relay_server was deleted in `9f6371f` (2026-07-06) but the config section
  remains. (Reader audit in the config.yaml section below.)
- **Impact**: Low-Medium — dead config invites confusion and cargo-cult copying;
  `pc_api_key` is a credential-shaped key that suggests a live feature.

(further findings continue in later commits)
