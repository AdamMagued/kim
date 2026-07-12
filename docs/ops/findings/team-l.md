# TEAM L — Docs, DX & Product Polish (Wave 1)

Status: COMPLETE
Baseline: `integration/audit-fixes`, 2026-07-12
Scope: docs truth-audit (README, HOW_TO, CLAUDE.md×5, FEATURE_FLAGS, ROADMAP*/PROPOSAL*), onboarding, error-message quality, logging/support, docs sprawl, justfile DX.
Method: every factual claim greped against the code it describes. Findings ordered most-severe-first. A consolidated doc truth-audit table is at the bottom.

---

## F-L-1: README privacy section misstates the screenshot-retention policy (wrong number, wrong mechanism)
- **File:** README.md:135 vs orchestrator/session_store.py:775-796, 145-148
- **Severity:** High
- **Class:** docs
- **Evidence:** README (Privacy section) claims: `Screenshots in kim_sessions/ (retained 7 days, strippable — see Settings → Data)`. The code says otherwise twice over: (1) `SessionStore.prune_old_sessions()` defaults are `max_age_days=30, screenshot_strip_age_days=2` — screenshot payloads are stripped after **2** days and whole sessions deleted after **30** days, not 7; (2) session_store.py:145-148 strips base64 image data at **write time** (`_strip_images_for_disk` — "Base64 image data is stripped to keep files manageable"), so full screenshots may never persist in the JSONL at all. A privacy claim is exactly the kind of doc line users rely on; it is wrong in number and in mechanism.
- **Fix sketch:** Rewrite the README privacy bullet to state the real policy: images stripped from session files on write; residual screenshot payloads stripped after 2 days; sessions deleted after 30 days; link the Data pane.
- **Cross-territory?** no (docs-only fix)

## F-L-2: Missing venv has NO friendly-fail — silent fallback to system python3 → raw ModuleNotFoundError; README has no Troubleshooting section
- **File:** desktop/src-tauri/src/subprocess.rs:327-387 (`find_python_interpreter`), README.md (no troubleshooting section exists)
- **Severity:** High
- **Class:** docs (DX / onboarding)
- **Evidence:** The long-known gotcha "missing venv → instant task errors" is neither documented nor auto-detected. `find_python_interpreter()` resolution: sidecar → `~/.kim_root|~/.kim` venvs → project venv → **bare system `python3`**. On any machine with a system python3 (i.e., every Mac), a missing/broken project venv silently resolves to system python, the orchestrator spawns without its dependencies, and dies instantly with a raw `ModuleNotFoundError: No module named 'anthropic'` (or similar) in the task stream. The only friendly message (subprocess.rs:382-386, "No Python interpreter found. Install Python 3, create a project venv…") fires **only** when there is no Python at all — the rarest case. `grep -rn "ModuleNotFoundError\|No module named" desktop/src-tauri/src/*.rs` → zero hits: nothing detects or translates the actual common failure. README has no Troubleshooting section covering this (or anything else).
- **Fix sketch:** (a) When falling through to system python, run a cheap preflight (`python -c "import mcp, anthropic"` or `-m orchestrator.agent --preflight`) and surface "Kim's Python dependencies are not installed — run ./install.sh or: python -m venv venv && pip install -r requirements.txt"; (b) add a README Troubleshooting section with this as entry #1.
- **Cross-territory?** yes — Team D owns the Rust preflight; Team L owns the README section.

## F-L-3: FEATURE_FLAGS.md documents a `RELAY_ENABLED` flag that does not exist anywhere in the frontend
- **File:** docs/FEATURE_FLAGS.md:3-9 vs desktop/src (grep)
- **Severity:** Medium
- **Class:** docs
- **Evidence:** FEATURE_FLAGS.md instructs: "`RELAY_ENABLED` in `desktop/src/components/kim-ui/RevampSettings.tsx` controls that surface and must default to `false`. For local relay UI work, change it to `true`…". `grep -rn "RELAY_ENABLED\|relayEnabled\|relay" desktop/src` → the identifier appears **nowhere** in desktop/src (only an unrelated string in chat/utils.ts:284). The relay settings surface was removed in the relay decommission (see ROADMAP_PROGRESS Phase 0 "relay/voice/dead-command decommission" and Team G F-G-3's dead `relay:` config section), but this doc still tells a developer to flip a flag that was deleted. A doc whose entire purpose is flag accuracy is 50% wrong (the `VOICE_ENABLED` half checks out: mcp_server/config.py:183).
- **Fix sketch:** Delete the Relay section of FEATURE_FLAGS.md (or rewrite it to say the surface was removed and point at the relay server code if it still exists); cross-link Team G's dead `relay:` config finding.
- **Cross-territory?** no (docs-only); root config cleanup is Team G F-G-3.

## F-L-4: Every hard-coded test/tool count in README and root CLAUDE.md is stale — and they contradict each other
- **File:** README.md:17,71-77,102 vs CLAUDE.md:41-46 vs actual code
- **Severity:** Medium
- **Class:** docs
- **Evidence:** Three-way divergence:

  | Claim | README | root CLAUDE.md | actual (counted 2026-07-12) |
  |---|---|---|---|
  | MCP tools | 31 (×3 places) | 50 | **50** `Tool(` entries in mcp_server/tool_registry.py |
  | Python tests | "816+" | "927+" | **~1885** `def test_` in tests/ |
  | Vitest tests | 31 | 73 | **~335** `it(`/`test(` in desktop/src |
  | Rust desktop tests | 50 | 54 | **~164** `#[test]`/`#[tokio::test]` |
  | Rust CLI tests | — | 90 | **~183** |

  README is stale by 2–10× on every number; even the fresher CLAUDE.md is ~2× off. Two authoritative docs disagreeing with each other and with the code erodes trust in both.
- **Fix sketch:** Remove hard counts from prose ("50+ tools", "four test suites") or generate them; if counts stay, add a doc-drift CI check comparing `Tool(` count to the README number.
- **Cross-territory?** no

## F-L-5: HOW_TO.md golden-path recipes point at symbols/files that don't exist (5 distinct rots)
- **File:** HOW_TO.md:9-13, 51-60 vs mcp_server/tool_registry.py, mcp_server/tools/, desktop/src/types/
- **Severity:** Medium
- **Class:** docs
- **Evidence:** HOW_TO.md's whole premise is "exact minimal file set — read only those files", so wrong pointers actively misroute:
  1. "Add an MCP tool" step 2: "add the JSON schema under **`TOOL_SCHEMAS`** and a dispatch entry in **`TOOL_DISPATCH`**" — neither name exists; the registry exports `TOOLS`, `DISPATCH`, `TIER_DISPATCH` (tool_registry.py:5, server.py:34).
  2. "Add an MCP tool" step 3: "**`mcp_server/tool_tiers.py`** — add a risk tier entry" — tier entries live in `TIER_DISPATCH` in **tool_registry.py:1076**; tool_tiers.py only filters by `KIM_ENABLED_TOOL_TIERS`.
  3. "Fix a web automation issue" step 1: "**`mcp_server/tools/web.py`**" — web is now a package (`mcp_server/tools/web/`); there is no web.py.
  4. Same recipe step 3: "`site_configs.py` — per-site overrides and **`FORM_SCHEMA`**" — `FORM_SCHEMA` lives in `mcp_server/tools/web/observation.py`, not site_configs.py (grep -l FORM_SCHEMA).
  5. "Add an agent event" step 1: "**`events.schema.json`** (repo root)" — actual location is `desktop/src/types/events.schema.json` (find confirms one non-worktree copy). The recipe also says "(3 files)" then lists 4 steps.
- **Fix sketch:** Correct the five pointers; add a tiny CI/pre-commit doc-link check that greps HOW_TO for named files/symbols and fails when they vanish.
- **Cross-territory?** no

## F-L-6: mcp_server/CLAUDE.md file table misdescribes `sites/` and `tools/` contents
- **File:** mcp_server/CLAUDE.md:19-20 vs mcp_server/sites/, mcp_server/tools/
- **Severity:** Medium
- **Class:** docs
- **Evidence:** The table claims `sites/` holds "Per-site web automation configs (`site_configs.py`, `FORM_SCHEMA`)" — `ls mcp_server/sites/` shows `base.py, guc_cms.py, guc_mail.py`; site_configs.py is in `orchestrator/providers/browser/` and FORM_SCHEMA in `tools/web/observation.py`. The `tools/` line lists groups "files, shell, screen, mouse, keyboard, windows, browser, web, git, code, search" — there is no `browser` group file, and `github.py`, `memory.py`, `ui_observe.py`, `screen_annotator.py`, `web_element_scoring.py` are unlisted. Per-directory CLAUDE.md files are loaded by agents as ground truth (context_loader.py), so wrong tables misroute every future agent.
- **Fix sketch:** Regenerate both lines from `ls`; note site_configs' true home.
- **Cross-territory?** no

## F-L-7: 59 `KIM_*` environment variables in code; ~8 documented anywhere; no env-var reference exists
- **File:** orchestrator/, mcp_server/, desktop/src-tauri/src/, cli/src (grep) vs README.md, ARCHITECTURE.md, docs/CONTRACTS.md
- **Severity:** Medium
- **Class:** docs (DX)
- **Evidence:** `grep -rhoE "KIM_[A-Z_]+" …| sort -u` → **59 distinct vars** (KIM_FAKE, KIM_ENABLED_TOOL_TIERS, KIM_HITL_RISK_THRESHOLD, KIM_APPROVAL_SOCK, KIM_BROWSER_EXECUTABLE, KIM_CONTEXT_BUDGET_TOKENS, KIM_DISCORD_WEBHOOK, …). Docs (CONTRACTS.md + ARCHITECTURE.md combined) mention **8**; README mentions **zero** KIM_* vars (only the three provider API keys). Even KIM_FAKE — the documented offline test mode that Operation Google-Level's own exit criterion G10 depends on — is absent from README/HOW_TO (it only appears in the justfile `fake` recipe and internal docs). Behavior knobs that exist only in source are undiscoverable and unsupportable.
- **Fix sketch:** Add docs/ENVIRONMENT.md (or a README appendix) generated or hand-curated: name, layer, default, effect — start with the user-facing dozen (KIM_FAKE, KIM_LOG_LEVEL, KIM_ENABLED_TOOL_TIERS, KIM_HITL_*, KIM_BROWSER_*).
- **Cross-territory?** no

## F-L-8: `just setup` missing, and the existing install.sh/install.bat are never mentioned by README — three disconnected onboarding paths
- **File:** justfile (repo root), install.sh, install.bat, README.md:24-63
- **Severity:** Medium
- **Class:** docs (DX)
- **Evidence:** Three setup paths exist and none references the others: (1) README walks manual steps (venv, pip, playwright, cp config, npm install); (2) `install.sh` automates exactly that plus writes `~/.kim_root` — but README never mentions it, so nobody runs it; (3) justfile has `check/test/test-web/test-py/fake/dev` but **no `setup`** — and since almost every recipe starts with `source venv/bin/activate`, a newcomer's very first `just check` on a fresh clone dies with a raw `venv/bin/activate: No such file or directory` (compounding F-L-2). `just test` is an acceptable `test-all` equivalent; `setup` is genuinely absent. Also `just check`'s "<30 seconds" claim runs ~1885 pytest tests in the fast lane — plausibly stale.
- **Fix sketch:** Add `setup:` recipe that calls install.sh (single source of truth); README Install section: "Quick: `./install.sh` (or `just setup`) — Manual: steps below"; make venv-dependent recipes emit "run `just setup` first" when venv/ is absent.
- **Cross-territory?** no

## F-L-9: desktop/src-tauri/CLAUDE.md documents a Python-resolution order that is wrong, including a dead arm install.sh can never satisfy
- **File:** desktop/src-tauri/CLAUDE.md:36 vs desktop/src-tauri/src/subprocess.rs:327-368, paths.rs:38, install.sh:107
- **Severity:** Medium
- **Class:** docs (+ latent dead code)
- **Evidence:** CLAUDE.md invariant: "resolution order is bundled-sidecar-first → `~/.kim_root` → `~/.kim` → system. Do not short-circuit this." Two problems: (1) it omits the **project-local venv** step (subprocess.rs:354-363) — the path virtually every developer actually hits — so an agent obeying the doc would preserve the wrong order; (2) subprocess.rs:336-345 probes `~/.kim_root/venv/bin/python` as a *directory*, but install.sh:107 creates `~/.kim_root` as a *file* (`echo "$PWD" > "$HOME/.kim_root"`), and paths.rs:38 reads it as a file — so the "install-script venv in ~/.kim_root" arm can never match anything the install script produces. The doc canonizes a dead arm. (Adjacent to Team D F-D-2's root-precedence finding — this is the interpreter-precedence sibling.)
- **Fix sketch:** Doc: state the real order incl. project venv. Code (Team D): drop or fix the `~/.kim_root`-as-directory candidates.
- **Cross-territory?** yes — code arm is Team D; doc line is Team L.

## F-L-10: Bare `return f"ERROR: {e}"` in ~20+ tool handlers produces contentless dead-end errors (the `ERROR: 'path'` class)
- **File:** mcp_server/tools/git.py:106,120,145,166,183,207,235; mouse.py:29,44,59,79,103; keyboard.py:59,84,100; code.py:264,361; screen.py:62,108; shell.py:579; (82 `f"ERROR` sites total in mcp_server/tools/)
- **Severity:** Medium
- **Class:** docs (error-message quality) / bug-adjacent
- **Evidence:** Generalizing Team H F-H-4 (`ERROR: 'path'` from a KeyError): any handler that catches broadly and returns `f"ERROR: {e}"` collapses `KeyError('path')` → `ERROR: 'path'`, `IndexError(0)` → `ERROR: 0`, `TypeError` → messages with no type context. The consumer is the LLM (which retries blind) and the user's activity feed (which shows a riddle). No message in this class says what to do next.
- **Fix sketch:** Repo-wide pattern: `return f"ERROR: {type(e).__name__}: {e}"` minimum; better, a shared `tool_error(e, hint=...)` helper; lint/grep-test banning bare `f"ERROR: {e}"`.
- **Cross-territory?** yes — Team C owns mcp_server fixes; the pattern rule is a triage-level decision.

## F-L-11: README License section says "License TBD" while an MIT LICENSE ships at repo root
- **File:** README.md:139-151 vs LICENSE:1-3
- **Severity:** Medium
- **Class:** docs
- **Evidence:** README: "License TBD — see docs/archive/PRODUCTION_ROADMAP.md § P0-4" plus `TODO(human): Choose a license before making this repo public… A LICENSE file must be added once the decision is made.` Repo root LICENSE: "MIT License / Copyright (c) 2026 adam magued". Contradictory licensing signals are a legal-clarity problem for any consumer; Contributing section is likewise stale ("TBD pending license decision").
- **Fix sketch:** README License → "MIT — see [LICENSE](LICENSE)"; delete the TODO block; unblock the Contributing stub. (If MIT was NOT a deliberate decision, that's a user decision to surface at triage — the LICENSE file, not the README, is what the world sees.)
- **Cross-territory?** no — but flag to user at triage in case LICENSE was committed unintentionally.

## F-L-12: desktop/src/CLAUDE.md tells contributors ESLint enforces no-new-`any` — ESLint does not exist (doc side of F-F-1)
- **File:** desktop/src/CLAUDE.md:23 vs desktop/package.json, repo root (no eslint config)
- **Severity:** Low (doc side; the missing linter itself is Team F F-F-1, High)
- **Class:** docs
- **Evidence:** "**No new `any`**: ESLint warns on new `any` types in this directory." Team F confirmed: no eslint dependency, no config, no CI step, anywhere. The doc invents an enforcement mechanism; contributors will assume CI catches what nothing catches.
- **Fix sketch:** Until F-F-1 lands, reword to "No new `any` (convention — not yet lint-enforced)"; when ESLint lands, restore the claim.
- **Cross-territory?** yes — root fix Team F (F-F-1); this is the doc correction.

## F-L-13: No support bundle: "Reveal logs" is the entire support story; scheduled_runs logs unbounded (cross-ref F-J-1)
- **File:** desktop/src/components/kim-ui/settings-panes/PaneInfo.tsx:235 ("Reveal logs"), logs/scheduled_runs/ (1,176 files on this dev machine), mcp_server/logger.py:256 vs logs/scheduled_runs (no retention)
- **Severity:** Low
- **Class:** docs (supportability)
- **Evidence:** Good news verified: structured JSONL logs rotate daily with 7-day retention on both GUI and CLI paths (agent.py `__main__` routes through cli.py:94-99), and error suggestions correctly point at the real "Settings → Feedback → Reveal logs" control (agent_states.py:103). Gaps: (1) no one-click support bundle (logs + config-with-secrets-redacted + versions + last session trace) — a user filing a bug must hand-assemble from logs/, kim_sessions/, config.yaml; (2) `logs/scheduled_runs/` has **no retention** — 1,176 files accumulated locally (root bug is Team J F-J-1; noted here because it is precisely what a support bundle would sweep up); (3) trivial: orchestrator/cli.py:92 comment says `logs/kim-YYYY-MM-DD.jsonl` (dash) — actual pattern is `kim_{date}.jsonl` (underscore, logger.py:132).
- **Fix sketch:** Add "Copy support bundle" next to Reveal logs (zip of last N days logs + redacted config + app/py/rust versions); fix the cli.py comment; F-J-1 retention closes the leak.
- **Cross-territory?** yes — bundle button is Team F/D; F-J-1 is Team A/J territory.

## F-L-14: Docs sprawl — verdict: largely already consolidated; three residual items
- **File:** ROADMAP.md, docs/ROADMAP_PROGRESS.md, docs/archive/ (20 files), docs/PROPOSAL_*.md (6 files)
- **Severity:** Low
- **Class:** docs
- **Evidence:** The feared sprawl is mostly handled: ROADMAP.md is a clean router to the single living plan (docs/ROADMAP_TO_10.md) + progress log (ROADMAP_PROGRESS.md); 4 superseded backlogs are archived with provenance notes; docs/archive/ is clearly labeled. Residuals: (1) proposal status headers are inconsistent in format and placement (`Status: accepted` / `**Status:** proposal (evidence-verified, not started)` / PROPOSAL_code_tab_backend.md has **no status line at all**, just a 2026-06-11 date — is it live or superseded by the parity proposal?); (2) ROADMAP_TO_10.md §backlog still lists A6 "Flip ipc_protocol default to typed" as pending while ROADMAP_PROGRESS.md:364 records it "code-complete; live step outstanding" and config.rs:15 already defaults `typed` — the plan doc and progress doc disagree on a shipped item; (3) docs/archive/repomap.md is hand-maintained and dated 2026-06-29 — either refresh or mark stale-by-design at top.
- **Fix sketch:** One-line status header convention for proposals (Status: draft|accepted|done|superseded + date); annotate A6 in ROADMAP_TO_10 as shipped-pending-verification; stamp repomap.md.
- **Cross-territory?** no

---

## Doc truth-audit table (claim → reality → verdict)

| # | Doc & line | Claim | Reality | Verdict |
|---|---|---|---|---|
| 1 | README:135 | Screenshots retained 7 days | Stripped at write + 2-day strip / 30-day delete (session_store.py:775-796) | **FALSE** (F-L-1) |
| 2 | README:17,102 | 31 MCP tools | 50 tools (tool_registry.py) | **FALSE** (F-L-4) |
| 3 | README:71-77 | 816+ py / 31 vitest / 50 rust tests | ~1885 / ~335 / ~164 | **FALSE** (F-L-4) |
| 4 | README:139 | License TBD, no LICENSE file yet | MIT LICENSE at root | **FALSE** (F-L-11) |
| 5 | root CLAUDE.md:41-46 | 927+/73/54/90 tests | ~1885/~335/~164/~183 | **STALE** (F-L-4) |
| 6 | root CLAUDE.md:28 | mcp_server has 50 tools | 50 | TRUE |
| 7 | FEATURE_FLAGS.md:5 | RELAY_ENABLED in RevampSettings.tsx | identifier absent from desktop/src | **FALSE** (F-L-3) |
| 8 | FEATURE_FLAGS.md:13 | VOICE_ENABLED in mcp_server/config.py, default False | config.py:183, default False | TRUE |
| 9 | HOW_TO:11 | TOOL_SCHEMAS / TOOL_DISPATCH in tool_registry.py | TOOLS / DISPATCH / TIER_DISPATCH | **FALSE** (F-L-5) |
| 10 | HOW_TO:12 | tier entry goes in tool_tiers.py | TIER_DISPATCH in tool_registry.py:1076 | **FALSE** (F-L-5) |
| 11 | HOW_TO:53 | mcp_server/tools/web.py | tools/web/ package | **STALE** (F-L-5) |
| 12 | HOW_TO:55 | FORM_SCHEMA in browser/site_configs.py | tools/web/observation.py | **FALSE** (F-L-5) |
| 13 | HOW_TO:37 | events.schema.json (repo root) | desktop/src/types/events.schema.json | **FALSE** (F-L-5) |
| 14 | mcp_server/CLAUDE.md:20 | sites/ = site_configs.py + FORM_SCHEMA | sites/ = base, guc_cms, guc_mail | **FALSE** (F-L-6) |
| 15 | mcp_server/CLAUDE.md:19 | tools/ groups incl. "browser" | no browser.py; github/memory/ui_observe unlisted | **STALE** (F-L-6) |
| 16 | src-tauri/CLAUDE.md:36 | interp order sidecar → ~/.kim_root → ~/.kim → system | omits project venv; ~/.kim_root-as-dir arm dead vs install.sh file | **FALSE** (F-L-9) |
| 17 | desktop/src/CLAUDE.md:23 | ESLint warns on new `any` | no ESLint anywhere (F-F-1) | **FALSE** (F-L-12) |
| 18 | ROADMAP_TO_10 A6 | ipc_protocol typed-flip pending | already default `typed` (config.rs:15); PROGRESS says code-complete | **STALE** (F-L-14) |
| 19 | orchestrator/cli.py:92 comment | logs/kim-YYYY-MM-DD.jsonl | kim_YYYY-MM-DD.jsonl (underscore) | **STALE** (F-L-13) |
| 20 | agent_states.py:103 | "Settings → Feedback → Reveal logs" exists | pane id `feedback` + Reveal logs button confirmed | TRUE |
| 21 | ROADMAP.md router claims | ROADMAP_TO_10 active; 4 backlogs archived | all files exist where stated | TRUE |
| 22 | orchestrator/CLAUDE.md file table | listed files exist | all exist (fake.py + browser/ pkg unlisted — minor) | TRUE-ish |

## Cross-territory handoffs
- **Team D:** F-L-2 (python preflight / friendly ModuleNotFoundError), F-L-9 (dead ~/.kim_root-as-dir interpreter arm).
- **Team C:** F-L-10 (tool_error helper across mcp_server/tools; generalizes Team H F-H-4).
- **Team F:** F-L-12 doc line rides on F-F-1 (add ESLint); F-L-13 support-bundle button.
- **Team A/J:** F-L-13 cross-refs F-J-1 (scheduled_runs retention).
- **User decision at triage:** F-L-11 — confirm MIT LICENSE at root was intentional before README is updated to advertise it.
