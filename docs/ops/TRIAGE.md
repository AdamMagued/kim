# Operation Google-Level — Triage board

Single work queue for Wave 2. Populated at the post-Wave-1 triage session
(dedupe → accept/reject → assign to a Wave-2 team → rank by severity).
Finding format and severity scale: see `docs/OPERATION_GOOGLE_LEVEL.md` §2/§3.

## Status: AWAITING WAVE 1 (only inherited findings pre-triaged so far)

## Queue

| # | Finding ID | Severity | Title | Source file | Verdict | Assigned team | Fix commit |
|---|---|---|---|---|---|---|---|
| 1 | F-INH-1 | Medium | Gemini OAuth token frozen at spawn → mid-task auth death | findings/inherited.md | accepted (pre-triage) | B' (+D') | — |
| 2 | F-INH-2 | Low | `max_tokens` 400s on newer OpenAI models | findings/inherited.md | accepted (pre-triage) | B' | — |
| 3 | F-INH-3 | Low | malformed tool JSON → `{}`; model gets no signal | findings/inherited.md | accepted (pre-triage) | B' | — |
| 4 | F-INH-4 | Low | 2 Ollama HTTP round-trips per turn | findings/inherited.md | accepted (pre-triage) | B' | — |
| 5 | F-INH-5 | Low | project_root default mismatch client vs server | findings/inherited.md | accepted (pre-triage) | A' | — |
| 6 | F-INH-6 | Low | MCP errors are plain text (string-prefix contract) | findings/inherited.md | accepted (pre-triage) | C' (+H doc) | — |
| 7 | F-INH-7 | Low | interval schedules drift later forever | findings/inherited.md | accepted (pre-triage) | A' | — |
| 8 | F-INH-8 | Low | list_sessions O(total transcript bytes) | findings/inherited.md | accepted (pre-triage) | A' | — |

## Rejected / no-action

| Finding | Reason |
|---|---|
| cobweb 1.1–1.5, 2.1–2.5, 3.1, 3.4, 3.8, 4.1–4.3, 5.1, 5.3, 6.1–6.6 | already fixed + regression-tested on main (`fix/cobweb-plumbing`, merged) |
| cobweb 3.5 (dropped tool-call narration) | fixed later by audit campaign ("H2") |
| cobweb 5.2 (preflight inside runner lock) | documented design decision in scheduled_runner.py |
