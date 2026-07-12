# Operation Google-Level — TRIAGE BOARD (Wave 1 complete)

**Status:** ALL 12 hunt teams reported. **119 findings + 8 inherited = 127.** Awaiting owner accept/reject before Wave 2.
**Baseline:** `integration/audit-fixes` (== origin/main). Every finding committed under `docs/ops/findings/team-*.md`.
**Deliverables also produced:** `docs/THREAT_MODEL.md` (Team I), `docs/CONTRACTS.md` (Team H, all 4 seams + V-3 test plan), Wave-4 ratchet plan (in `team-k.md`).

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

| ID | Title | Rec | W2 |
|---|---|---|---|
| **F-C-1** | `git -c 'alias.x=!<shell>'` via `run_command` = un-approved arbitrary RCE + absolute-path secret read; defeats allowlist + path-sandbox + deny-list, no HITL under default config | ACCEPT | C' |
| **F-C-2** | Same class via `awk 'BEGIN{system()}'`, `tar --checkpoint-action=exec=`, `sed`, `make` — allowlisted command-runners exec arbitrary programs | ACCEPT | C' |

**Root cause (Team I threat model):** allowlist trusts program *names*, but allowed programs can exec other programs. Fix is a design change (argv-level policy on known escape flags + default the HITL threshold on), not a blocklist patch. **These gate everything — recommend fixing before anything else ships.**

---

## TIER 1 — HIGH

### Security / trust
| ID | Title | W2 |
|---|---|---|
| F-I-2 | `KIM_CODEX_BYPASS_SANDBOX=1` → prompt-injection → unsandboxed RCE, no per-command HITL | C'/A' |
| F-C-3 | `gh auth token` exfiltrates GitHub token (allowlisted binary reads its own store) | C' |
| F-D-1 | `/v1/open` navigates app webview to ANY URL → link-local/RFC-1918/metadata SSRF | D' |
| F-C-4 | SSRF via subresource/XHR from navigated page — **pairs with F-D-1, one guard fixes both** | C'+D' |
| F-D-4 | Loopback bridge token injected into 3rd-party provider webviews → page compromise → `/v1/task` | D' |
| F-G-4 | `shell.blocked_commands` config read by nothing (false security) | C' |
| F-I-1 / F-K-6 | Installers verify same-origin sha256, never cosign signature — supply-chain gap | G'/K' |
| F-G-6 | Pillow pin blocks 5 CVE fixes | G' |

### Correctness
| ID | Title | W2 |
|---|---|---|
| F-A-1 | `/compact` is a complete no-op for ALL API providers (Claude/OpenAI/Ollama/DeepSeek) | A' |
| F-A-2 / F-B-2 | Resumed long API session sends assistant-first list → Anthropic 400 (root memory.py; guard claude.py) | A'+B' |
| F-B-1 | Every Gemini OAuth HTTP failure misclassified non-retryable ("oauth" label trips regex) | B' |
| F-B-7 | Browser sentinel-echo race: selectors match USER turn, scrape echoed prompt as answer, can fake TASK_COMPLETE | B' |
| F-F-2 | Cross-session event bleed: missing `session_id` = "belongs to this view"; old-run output pours into new view | F'+H' |
| F-F-5 | Spinner-forever + hidden recovery banner when a run ends without `kim-agent-done` | F' |
| F-H-1 | Run-lifecycle events that CLEAR running-state are off-schema + un-enveloped | H'/F' |
| F-H-2 / F-H-8 | Codex-bridge typed mode never emits run-done/failed + spawn exports no run identity — **root cause of F-F-2 & F-F-5** | H'/D' |
| F-E-4 | one-shot `kim chat`/`kim code` exits 0 on FAILED runs and Ctrl-C | E' |
| F-E-7 | `kimctl send --session` reports instant success from a STALE `TASK_COMPLETE` | E' |

### Tests / CI / DX
| ID | Title | W2 |
|---|---|---|
| F-K-1 | No Windows CI job — app ships to Windows, CI never builds it | K' |
| F-K-8 | git tool's path-validation security gate has ZERO test coverage — **compounds F-C-1** | K' |
| F-F-1 / F-L-12 | No ESLint anywhere; docs falsely claim it enforces no-`any` (20k LOC never linted) | F'/K' |
| F-L-1 | README privacy section misstates screenshot-retention policy (wrong number + mechanism) | L' |
| F-L-2 | Missing venv has no friendly-fail → raw ModuleNotFoundError, no Troubleshooting doc | L'/D' |

---

## TIER 2 — STRUCTURAL / BIG WINS (🔶 owner decisions)

| ID | Item | Recommend |
|---|---|---|
| 🔶 **G-VERDICT** | **DELETE `pythonExperimentTool/` — 8.9 MB / ~89k LOC (~43% of repo).** Confirmed dead vendored claw-code CLI (legacy Code-tab fallback), unreachable in any standard install; NOT an alternative to codex. `codex_engine` = KEEP. | **Owner yes/no.** Rec: YES |
| 🔶 **BRANCH-GRAVEYARD** | 184 of 192 branches recommended delete (`BRANCH_GRAVEYARD.md`) — merged/rebased-equal. | **Owner yes/no.** Rec: delete the 85 ancestor-merged + 16 rebased-equal first |
| 🔶 **F-L-11** | README says "License TBD" but MIT LICENSE ships at root. | **Owner: confirm license intent** |

---

## TIER 3 — MEDIUM (grouped by W2 owner)

- **A':** F-A-3 (event-loop-blocking file re-read), F-A-4 (codex-side /compact never works), F-A-5..8; F-J-1/4/6 (scheduled_runs log leak, fsync offload, self-watchdog); F-INH-5/7/8.
- **B':** F-B-3 (ollama done_reason), F-B-4 (image tool-results break pairing), F-B-5 (httpx timeout misclassified), F-B-8 (non-idempotent browser retries), F-B-9..14; F-J-3 (reap orphan CDP Chrome); F-INH-1/2/3/4.
- **C':** F-C-5 (unclamped run_python/run_node/web_wait timeouts), F-C-6 (code.py no pgroup kill), F-L-10 (shared `tool_error` helper for ~20 bare `ERROR:` sites); F-INH-6.
- **D':** F-D-2 (paths.rs env-override ignored), F-D-3 (unauth /v1/health), F-D-5 (stdout no back-pressure); F-J-5 (spawn_blocking run_update); F-L-9 (dead ~/.kim_root arm).
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

## Proposed Wave 2 structure (on your ACCEPT)

7 fix teams, worktree-isolated, territory-disciplined (A'–G' map to Wave-1 territories; cross-cutting H/I/J/K/L findings assigned to territory owners above). Every fix ships with a failing→passing test. **Merge order:** G' (the big delete) → C' (security) → A' → D' → B' → E' → F'. Tier-0/1 security can run as a hotfix sub-branch first to close the Criticals immediately.

**Owner, tell me:**
1. Any Tier-0/1 items to DEFER or REJECT? (default: ACCEPT all)
2. The three 🔶 decisions: delete pythonExperimentTool? prune the 184 branches? license intent?
3. Close the 2 Criticals as an immediate hotfix, or fold into normal Wave-2 flow?
