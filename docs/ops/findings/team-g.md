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

### F-G-3 — `relay:` section survives in config.yaml after relay decommission (CONFIRMED dead)
- **Where**: `config.yaml` (`relay:` with `pc_api_key`, `poll_interval`, `url`).
- **What**: relay_server was deleted in `9f6371f` (2026-07-06) but the config section
  remains. Reader audit: grep for `pc_api_key` / `poll_interval` / `"relay"` across
  orchestrator/, mcp_server/, desktop/src-tauri/, cli/, codex_engine/ → **0 readers**.
- **Impact**: Low-Medium — dead config invites confusion and cargo-cult copying;
  `pc_api_key` is a credential-shaped key that suggests a live feature.

### F-G-4 — `shell.blocked_commands` in config.yaml is read by NOTHING (false-security config)
- **Where**: `config.yaml` L48-53 (`shell.blocked_commands:` listing `rm -rf /`,
  `format c:`, …) and `config.yaml.example` L86.
- **What**: repo-wide grep for `blocked_commands` across orchestrator/, mcp_server/,
  desktop/, cli/, tests/ → **0 readers**. `mcp_server/config.py` reads only
  `shell.timeout` (L162) and `shell.sandbox_mode` (L165). The real deny-list is
  hard-coded in `mcp_server/tools/shell.py` (`_DENY_COMMANDS` L64, `_DENY_PATTERNS`
  L84) and cannot be extended via config.
- **Impact**: **High** (safety illusion) — an operator who adds a command to
  `blocked_commands` reasonably believes it is now blocked; it is silently ignored.
  Either wire the key into shell.py's deny-set (additively — never let config REMOVE
  hard-coded entries) or delete the key from both YAML files and document the deny-list
  as code-owned.

### F-G-5 — Entire `voice:` section in config.yaml is dead, and the live key has a name mismatch
- **Where**: `config.yaml` `voice:` block (13 lines: enabled/engine/human_quirks/hume/
  maya1/speed/voice_id) vs `mcp_server/config.py` L183.
- **What**: the only voice key the code reads is the FLAT key
  `voice_enabled` (`VOICE_ENABLED: bool = _as_bool(_cfg.get("voice_enabled", False), False)`) —
  which does not exist in config.yaml. The nested `voice.enabled` and every subkey
  (`engine`, `human_quirks`, `hume.*`, `maya1.*`, `speed`, `voice_id`) have **0
  readers** (grep for `human_quirks|maya1|hume` in orchestrator/ + mcp_server/ → 0
  files). Voice scaffold was decommissioned in `9f6371f`.
- **Impact**: Medium — 13 lines of dead config, plus a real key (`voice_enabled`)
  that no shipped config file sets; setting `voice.enabled: true` does nothing.

### F-G-6 — Pillow pin makes 5 known CVE fixes unreachable (pip-audit)
- **Where**: `requirements.txt` L23 `Pillow~=10.0` (installs 10.4.0).
- **What**: `pip-audit -r requirements.txt` → Pillow 10.4.0 carries PYSEC-2026-165,
  CVE-2026-25990, CVE-2026-40192, CVE-2026-42310, CVE-2026-42311; fixes land in
  12.1.1/12.2.0. The compatible-release pin `~=10.0` caps at <11, so `pip install`
  can never pick up the fixed versions. Kim feeds screenshots and user-supplied
  images through PIL. Also: pytest 8.4.2 → PYSEC-2026-1845 (fix 9.0.3; dev-only,
  low).
- **Impact**: High (dependency hygiene on an image-parsing library) — bump to
  `Pillow~=12.1` (or at least `>=12.1.1`) and re-run the suites.

### F-G-7 — `scripts/claw-via-browser` is broken: launches a module that no longer exists
- **Where**: `scripts/claw-via-browser` L102 (`"$PYTHON" -m orchestrator.run_claw_relay …`),
  L25 (docstring references `orchestrator.run_claw_bridge`), L61-64 (binary lookup
  into `pythonExperimentTool/claw-code/rust/target/…`).
- **What**: neither `orchestrator/run_claw_relay.py` nor `orchestrator/run_claw_bridge.py`
  exists (repo-wide find → 0; both were merged into `orchestrator/codex_bridge_service.py`).
  The script fails at launch on every invocation. Last touched 2026-05-11 (`006ef34`).
- **Impact**: Medium-Low — dead dev tooling; belongs in the pythonExperimentTool
  delete package (verdict item 1-3 above, extend to `git rm scripts/claw-via-browser`).

### F-G-8 — requirements.txt ships two never-imported dependencies
- **Where**: `requirements.txt` L15 `aiosqlite~=0.20`, L41 `pynput~=1.7`.
- **What**: repo-wide grep (excluding venv/ and pythonExperimentTool/) for
  `import aiosqlite|from aiosqlite` and `import pynput|from pynput` → **0 hits** in
  orchestrator/, mcp_server/, codex_engine/, cli/, scripts/, desktop/. pynput appears
  only as a stubbed module name in `tests/conftest.py` L57 (legacy stub) and
  commented out in `kim-orchestrator.spec` L82. Every other dep verified in use
  (mcp, anthropic, openai, dotenv, yaml, httpx, aiofiles, aiohttp, json5,
  json-repair, pyperclip, pyautogui, mss, Pillow, playwright, pytest/-asyncio).
- **Impact**: Low — install weight + audit surface; drop both lines and the
  conftest stub entries.

### F-G-9 — install.sh: no minimum-Python-version check (repo targets 3.11)
- **Where**: `install.sh` L29-45 accepts the first `python3`/`python` found;
  `pyrightconfig.json` L8 declares `"pythonVersion": "3.11"`.
- **What**: on a machine with Python 3.9/3.10 the installer proceeds and fails later
  with confusing pip/syntax errors instead of a clear "need >= 3.11" message.
  Otherwise install.sh audits clean: `set -e`, idempotent venv/.env/dir creation,
  correct macOS vs Linux hints, guarded `~/.kim_root` write. (Nit: no `set -u`/
  `pipefail`; header comment in requirements.txt recommends committing a lockfile
  but none exists — `requirements-lock.txt` absent.)
- **Impact**: Low — one version-guard block fixes it.

## CONFIG.YAML KEY AUDIT (every key → read by code?)

| Key | Readers (grep, code dirs) | Status |
|---|---|---|
| allowed_paths, use_real_browser, custom_sites, logging.level, max_iterations, max_tokens, memory_keep_screenshots, memory_max_messages, model.{claude,deepseek,gemini,openai}, openai_api_key_env, openai_base_url, preview_mode, project_root, provider, screenshot_scale, shell.timeout, mcp_servers, connectors.enabled | >=1 each | LIVE |
| browser_provider.* (headless, force_headless, cdp_url, max_history_messages, max_inject_chars, stateful_threads, compact_at_ratio, max_thread_turns) | >=1 each | LIVE |
| bridge_timeout_secs, screenshot_flash_duration_ms, ipc_protocol | >=2 each | LIVE |
| shell.blocked_commands | 0 | **DEAD — F-G-4** |
| relay.{pc_api_key, poll_interval, url} | 0 | **DEAD — F-G-3** |
| voice.* (entire section) | 0 (code reads flat `voice_enabled` instead) | **DEAD — F-G-5** |

Note: `config.yaml.example` documents `shell.sandbox_mode` (read by code, L165 of
mcp_server/config.py) but `config.yaml` itself does not set it — defaults to True.

## SCRIPTS/ INVENTORY
- `check_file_size_gate.py` — wired: `.github/workflows/ci.yml` L57. KEEP.
- `gen-events.js` — wired: `desktop/package.json` `gen:events`. KEEP.
- `probe_appserver.py` — dev tool for codex_engine protocol-drift checks; referenced
  by codex_engine docs. KEEP.
- `install-kim.sh` / `install-kim.ps1` — CLI release installers (download prebuilt
  binary from GitHub releases; distinct purpose from root install.sh). KEEP.
- `claw-via-browser` — BROKEN (F-G-7). DELETE with the claw package.

justfile audits clean: `check`/`test`/`fake`/`dev` recipes reference only live paths;
`cargo test -p kim-cli` works because `cli` is a root-workspace member (Cargo.toml L3).
