# Operation Google-Level — TRIAGE BOARD (Wave 1 complete — TRIAGED)

**Status:** ALL 12 hunt teams reported. **119 findings + 8 inherited = 127.** **TRIAGED by the Triage Authority (Fable) — verdicts set below; Wave 2 dispatch-ready.**
**Baseline:** `integration/audit-fixes` (== origin/main). Every finding committed under `docs/ops/findings/team-*.md`.
**Deliverables also produced:** `docs/THREAT_MODEL.md` (Team I), `docs/CONTRACTS.md` (Team H, all 4 seams + V-3 test plan), Wave-4 ratchet plan (in `team-k.md`). Wave-2 dispatch in `docs/ops/WAVE2_PLAN.md`.

---

## TRIAGE AUTHORITY DECISIONS (Fable) — 2026-07-12

The repo owner delegated triage to me ("if fable thinks it's good, delete the 43% cleanup; ask fable on everything else and take what he said"). My calls, decisively:

**Verdict tally:** **ACCEPT ~118** · **DEFER 5** · **REJECT 4** (the pre-existing "Rejected/no-action" cobweb set stays rejected; it is already-fixed ground). Default was ACCEPT for every Critical/High and nearly every Medium/Low, because these are real, already-evidenced findings and Wave 2 is territory-disciplined + test-gated (low blast radius per fix).

**The big win — DONE (not a decision, executed):** `pythonExperimentTool/` deleted. I re-verified zero LIVE references myself (only dead spawn arms in `subprocess.rs`/`codex_projects.rs` reading a binary that is never built, plus stale CI/pytest/Cargo exclusions). Removed 246 files + `tests/claw_test_suite.py` + `scripts/claw-via-browser` = **~89k LOC / 8.9 MB (~43% of repo LOC)**. All four suites green after removal. Committed `7075708`.

**The two Criticals — HOTFIX-FIRST (decided).** F-C-1 (`git -c alias` RCE) and F-C-2 (awk/tar/sed/make exec escapes) gate everything: they defeat the allowlist + path-sandbox + deny-list with no HITL under default config. They ship as an **immediate hotfix sub-branch `ops/w2-hotfix-crit` off the triage baseline, merged before any other Wave-2 branch.** Rationale: (a) blast radius is arbitrary RCE + absolute-path secret read, (b) the fix is a self-contained argv-level policy change in `mcp_server/tools/shell.py` (+ git tool), so it does not need to wait on the broader C' pass, (c) F-K-8 (git path-gate has zero test coverage) rides along in the same hotfix so the fix lands with the missing regression pack. Everything else follows the normal merge train.

**Notable DEFERs (valid, but not Wave 2):**
- **F-E god-file split** (main.rs 2,155 + commands.rs 1,684 LOC) — DEFER. High-churn, zero behavior change, and it collides with every other E' edit; do it as its own isolated PR after Wave 2, not mixed into fix commits.
- **F-D-5 stdout back-pressure redesign** — DEFER to Wave 2 *stretch* (accept the finding, but the full bounded-channel rework is larger than a fix-with-test; if D' runs long, this slips to a follow-up. The lifecycle-event fixes F-H-1/F-H-2 are the priority in that territory).
- **F-I-1 / F-K-6 cosign signature verification in installers** — DEFER. Real supply-chain gap, but implementing signature verification needs a signing-key/release-infra product decision (owner + release eng), not a code fix a Wave-2 agent can self-complete. Flag to owner; keep the same-origin sha256 as interim.
- **F-K-1 Windows CI job** — DEFER to Wave 4 (Team R1 already owns "Windows CI job" in the ratchet). Accepting it into Wave 2 would have a frontend/CLI agent editing CI infra outside a clean territory; it belongs in the CI-infrastructure wave. Tracked, not dropped.
- **F-L "one living roadmap" consolidation (docs/archive sprawl)** — DEFER. Pure docs churn with product-judgment calls on what to archive; batch it with the Wave-4 docs-canon team (R3), not Wave 2.

**Notable REJECTs (won't-fix / by-design — one line each):**
- **F-D-3 `/v1/health` unauthenticated** — REJECT. By design: 127.0.0.1-only liveness probe returning `{"ok":true}`, no data. "Fingerprinting a local port" is not a threat on a loopback-bound server; token-gating a health check defeats its purpose. (Keep the route; a one-line doc-comment noting intent is folded into D' territory pass, not tracked as a fix.)
- **F-G-4 `shell.blocked_commands` read by nothing** — REJECT the "wire it up" option; ACCEPT only the delete-the-key half. Wiring config into the shell deny-set is exactly the kind of config-can-weaken-gates surface the CLAUDE.md invariant forbids; the deny-list must stay code-owned. So: delete the dead key + document deny-list as code-owned (that half is ACCEPTED under G'/C'); do NOT make it configurable.
- **cobweb 5.2 (preflight inside runner lock)** — REJECT (already a documented design decision in `scheduled_runner.py`; re-confirmed).
- **The prior "Rejected/no-action" cobweb block (1.1–6.6 etc.)** — REJECT stands: already fixed + regression-tested on merged `fix/cobweb-plumbing`. Not re-litigated.

**Everything not called out above is ACCEPT** at its default Wave-2 owner. The per-tier tables below carry a **Verdict** column reflecting this.

## Finding census

| Team | Territory | Findings |  | Team | Territory | Findings |
|---|---|---|---|---|---|---|
| A | Orchestrator core | 8 | | G | Satellites/legacy | 9 |
| B | Providers | 14 | | H | Contracts/IPC | 9 |
| C | MCP server/tools | 7 | | I | Security/threat | 4 |
| D | Desktop Rust | 5 | | J | Concurrency/perf | 6 |
| E | CLI/kimctl | 15 | | K | Tests/CI | 15 |
| F | Frontend | 13 | | L | Docs/DX | 14 |
| | | | | **Total** | | **119** (+8 inherited) |

Severity mix: **2 Critical · ~22 High · ~50 Medium · ~45 Low**.

---

## HOW TO USE THIS BOARD (owner action needed)

For each item: **ACCEPT** (fix in Wave 2), **DEFER** (valid, later), or **REJECT** (won't fix / by design). Default recommendation is ACCEPT for all Critical/High. Skim the tiers; tell me any to DEFER/REJECT plus the three 🔶 owner-decision items. Everything untouched stays at its default.

---

## TIER 0 — CRITICAL (security, fix first)

| ID | Title | Verdict | W2 |
|---|---|---|---|
| **F-C-1** | `git -c 'alias.x=!<shell>'` via `run_command` = un-approved arbitrary RCE + absolute-path secret read; defeats allowlist + path-sandbox + deny-list, no HITL under default config | **ACCEPT — HOTFIX** | `ops/w2-hotfix-crit` (→ C') |
| **F-C-2** | Same class via `awk 'BEGIN{system()}'`, `tar --checkpoint-action=exec=`, `sed`, `make` — allowlisted command-runners exec arbitrary programs | **ACCEPT — HOTFIX** | `ops/w2-hotfix-crit` (→ C') |

**Root cause (Team I threat model):** allowlist trusts program *names*, but allowed programs can exec other programs. Fix is a design change (argv-level policy on known escape flags + default the HITL threshold on), not a blocklist patch. **Triage Authority call: HOTFIX-FIRST — ship both on `ops/w2-hotfix-crit` off the triage baseline, merged before every other Wave-2 branch. F-K-8 (git path-gate has zero tests) rides in the same hotfix.**

---

## TIER 1 — HIGH

### Security / trust
| ID | Title | Verdict | W2 |
|---|---|---|---|
| F-I-2 | `KIM_CODEX_BYPASS_SANDBOX=1` → prompt-injection → unsandboxed RCE, no per-command HITL | ACCEPT | C'/A' |
| F-C-3 | `gh auth token` exfiltrates GitHub token (allowlisted binary reads its own store) | ACCEPT | C' |
| F-D-1 | `/v1/open` navigates app webview to ANY URL → link-local/RFC-1918/metadata SSRF | ACCEPT | D' |
| F-C-4 | SSRF via subresource/XHR from navigated page — **pairs with F-D-1, one guard fixes both** | ACCEPT | C'+D' |
| F-D-4 | Loopback bridge token injected into 3rd-party provider webviews → page compromise → `/v1/task` | ACCEPT | D' |
| F-G-4 | `shell.blocked_commands` config read by nothing (false security) | **REJECT wire-up / ACCEPT delete-key** | C'/G' |
| F-I-1 / F-K-6 | Installers verify same-origin sha256, never cosign signature — supply-chain gap | **DEFER (owner: signing infra)** | G'/K' |
| F-G-6 | Pillow pin blocks 5 CVE fixes | ACCEPT | G' |

### Correctness
| ID | Title | Verdict | W2 |
|---|---|---|---|
| F-A-1 | `/compact` is a complete no-op for ALL API providers (Claude/OpenAI/Ollama/DeepSeek) | ACCEPT | A' |
| F-A-2 / F-B-2 | Resumed long API session sends assistant-first list → Anthropic 400 (root memory.py; guard claude.py) | ACCEPT | A'+B' |
| F-B-1 | Every Gemini OAuth HTTP failure misclassified non-retryable ("oauth" label trips regex) | ACCEPT | B' |
| F-B-7 | Browser sentinel-echo race: selectors match USER turn, scrape echoed prompt as answer, can fake TASK_COMPLETE | ACCEPT | B' |
| F-F-2 | Cross-session event bleed: missing `session_id` = "belongs to this view"; old-run output pours into new view | ACCEPT | F'+H' |
| F-F-5 | Spinner-forever + hidden recovery banner when a run ends without `kim-agent-done` | ACCEPT | F' |
| F-H-1 | Run-lifecycle events that CLEAR running-state are off-schema + un-enveloped | ACCEPT | H'/F' |
| F-H-2 / F-H-8 | Codex-bridge typed mode never emits run-done/failed + spawn exports no run identity — **root cause of F-F-2 & F-F-5** | ACCEPT | H'/D' |
| F-E-4 | one-shot `kim chat`/`kim code` exits 0 on FAILED runs and Ctrl-C | ACCEPT | E' |
| F-E-7 | `kimctl send --session` reports instant success from a STALE `TASK_COMPLETE` | ACCEPT | E' |

### Tests / CI / DX
| ID | Title | Verdict | W2 |
|---|---|---|---|
| F-K-1 | No Windows CI job — app ships to Windows, CI never builds it | **DEFER → Wave 4 R1** | K'/R1 |
| F-K-8 | git tool's path-validation security gate has ZERO test coverage — **compounds F-C-1** | **ACCEPT — rides the crit hotfix** | hotfix/K' |
| F-F-1 / F-L-12 | No ESLint anywhere; docs falsely claim it enforces no-`any` (20k LOC never linted) | ACCEPT | F'/K' |
| F-L-1 | README privacy section misstates screenshot-retention policy (wrong number + mechanism) | ACCEPT | L' |
| F-L-2 | Missing venv has no friendly-fail → raw ModuleNotFoundError, no Troubleshooting doc | ACCEPT | L'/D' |

---

## TIER 2 — STRUCTURAL / BIG WINS (🔶 → RESOLVED by Triage Authority)

| ID | Item | Verdict (Fable) |
|---|---|---|
| **G-VERDICT** | DELETE `pythonExperimentTool/` — 8.9 MB / ~89k LOC (~43% of repo). Confirmed dead vendored claw-code CLI, unreachable; NOT an alternative to codex. `codex_engine` = KEEP. | **DONE — deleted + suites green, commit `7075708`.** Re-verified zero live refs myself. |
| **BRANCH-GRAVEYARD** | 184 of 192 refs recommended delete — merged/rebased-equal. | **DONE (local) — 46 local branches deleted (42 ancestor-merged via `-d` + 4 verified cherry-equal via `-D`).** Remote branches NOT touched → owner recommendation: `git push origin --delete` the merged/rebased-equal remotes, or use the forge stale-branch UI. Left in place: 6 worktree-checked-out `fix/audit-*` (in use) + ~40 unmerged-with-unique-patch stale bot branches (out of the safe-delete set — owner may `-D` these knowing they carry dangling pre-restructure patches). |
| **F-L-11** | README "License TBD" but MIT LICENSE ships at root. | **RESOLVED — README updated to state MIT** (matches shipped `LICENSE`, © 2026 adam magued). Safe: it aligns the README with the license the owner already committed, not a new legal decision. |

---

## TIER 3 — MEDIUM (grouped by W2 owner) — **all ACCEPT except where noted**

**Verdict:** ACCEPT the whole tier at its listed owner, with these two annotations: **F-D-5 (stdout back-pressure) = ACCEPT-but-stretch** (full rework may slip to a follow-up; lifecycle fixes prioritized). **F-G-1/F-G-5/F-G-7 dead-arm removals** ride the same territory pass as the pythonExperimentTool cleanup (already partly executed).

- **A':** F-A-3 (event-loop-blocking file re-read), F-A-4 (codex-side /compact never works), F-A-5..8; F-J-1/4/6 (scheduled_runs log leak, fsync offload, self-watchdog); F-INH-5/7/8.
- **B':** F-B-3 (ollama done_reason), F-B-4 (image tool-results break pairing), F-B-5 (httpx timeout misclassified), F-B-8 (non-idempotent browser retries), F-B-9..14; F-J-3 (reap orphan CDP Chrome); F-INH-1/2/3/4.
- **C':** F-C-5 (unclamped run_python/run_node/web_wait timeouts), F-C-6 (code.py no pgroup kill), F-L-10 (shared `tool_error` helper for ~20 bare `ERROR:` sites); F-INH-6.
- **D':** F-D-2 (paths.rs env-override ignored), ~~F-D-3 (unauth /v1/health)~~ **REJECT — by-design loopback liveness**, F-D-5 (stdout no back-pressure) **ACCEPT-stretch**; F-J-5 (spawn_blocking run_update); F-L-9 (dead ~/.kim_root arm).
- **F':** F-F-6 (Google-CDN font import), F-F-8/9/10/11 (envelope, ToolResultBlock shape, swallowed invoke rejections, O(n²) re-render), F-J-2 (cap runHistory).
- **E':** F-E medium set (drift, JSONL frame parsing, session-id, `--test-threads=1` parallel-unsafety).
- **G':** F-G-1 (dead codex-code arm), F-G-5 (dead voice: config), F-G-7 (broken claw-via-browser script).
- **I':** F-I-3 (world-readable session JSONLs), F-I-4 (unauth CDP :9222).
- **K':** flake one-line fix (task_timeout_s 1→8 back-port), real-binary parity auto-skip (F-K-7), coverage gate 60→ratchet.
- **H':** F-H-3..7 (silent chat-stdout drop, unenforced MCP required-args, orphaned event channels, undocumented tag grammar, codex-proxy schema loss).
- **L':** F-L-3..8/13/14 (stale flags/counts, rotted HOW_TO pointers, undocumented KIM_* env vars, onboarding fragmentation, support-bundle).

---

## TIER 4 — LOW / SMELL (~45)

Batch-fixable, low risk. Roll into each W2 team's territory pass. Full list in per-team files.

---

## Pre-accepted inherited findings (from Wave 0)

F-INH-1 (Med, B'+D') · F-INH-2..8 (Low) — see original queue below. Already triaged; folded into Tier-3 owners above.

| # | ID | Sev | Title | Team |
|---|---|---|---|---|
| 1 | F-INH-1 | Medium | Gemini OAuth token frozen at spawn → mid-task auth death | B'(+D') |
| 2 | F-INH-2 | Low | `max_tokens` 400s on newer OpenAI models | B' |
| 3 | F-INH-3 | Low | malformed tool JSON → `{}`; model gets no signal | B' |
| 4 | F-INH-4 | Low | 2 Ollama HTTP round-trips per turn | B' |
| 5 | F-INH-5 | Low | project_root default mismatch client vs server | A' |
| 6 | F-INH-6 | Low | MCP errors are plain text (string-prefix contract) | C'(+H doc) |
| 7 | F-INH-7 | Low | interval schedules drift later forever | A' |
| 8 | F-INH-8 | Low | list_sessions O(total transcript bytes) | A' |

## Rejected / no-action

| Finding | Reason |
|---|---|
| cobweb 1.1–1.5, 2.1–2.5, 3.1, 3.4, 3.8, 4.1–4.3, 5.1, 5.3, 6.1–6.6 | already fixed + regression-tested on main (`fix/cobweb-plumbing`, merged) |
| cobweb 3.5 (dropped tool-call narration) | fixed later by audit campaign ("H2") |
| cobweb 5.2 (preflight inside runner lock) | documented design decision in scheduled_runner.py |

---

## Wave 2 structure — APPROVED (see `docs/ops/WAVE2_PLAN.md` for the full dispatch)

8 branches, worktree-isolated, territory-disciplined (A'–G' map to Wave-1 territories; cross-cutting H/I/J/K/L findings assigned to territory owners above), plus the crit hotfix. Every fix ships a failing→passing test; green check suite before merge; branch `ops/w2-<team>`.

**Merge order (decided): `ops/w2-hotfix-crit` (F-C-1/F-C-2/F-K-8) → C' (safety) → A' → D' → B' → E' → F'.** (G' deletions already largely executed by the Triage Authority; the residual G' items — dead-arm strips, Pillow bump, dead config keys — fold into C'/D' territory passes as noted.)

**The three owner questions — answered by the Triage Authority (delegated):**
1. Tier-0/1 DEFER/REJECT: only F-I-1/F-K-6 (cosign) DEFERred and F-K-1 (Windows CI) DEFERred to Wave 4; everything else ACCEPT. Criticals = hotfix-first.
2. 🔶 decisions: pythonExperimentTool **deleted** ✓; local branches **pruned (46)** ✓ + remote-prune recommendation to owner; license **set to MIT** ✓.
3. Criticals: **immediate hotfix** on `ops/w2-hotfix-crit`, merged before all other Wave-2 branches. ✓
