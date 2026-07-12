# TEAM L — Docs, DX & Product Polish (Wave 1)

Status: IN PROGRESS (preliminary pass committed early for resilience; findings appended as confirmed)
Baseline: `integration/audit-fixes`, 2026-07-12
Scope: docs truth-audit, onboarding, error-message quality, logging/support, docs sprawl, justfile DX.

---

## F-L-1: README privacy section misstates the screenshot-retention policy (wrong number, wrong mechanism)
- **File:** README.md:135 vs orchestrator/session_store.py:775-796, 145-148
- **Severity:** High
- **Class:** docs
- **Evidence:** README (Privacy section) claims: `Screenshots in kim_sessions/ (retained 7 days, strippable — see Settings → Data)`. The code says otherwise twice over: (1) `SessionStore.prune_old_sessions()` defaults are `max_age_days=30, screenshot_strip_age_days=2` — screenshots are stripped after **2** days and whole sessions deleted after **30** days, not 7; (2) session_store.py:145-148 strips base64 image data at **write time** (`_strip_images_for_disk`) — "Base64 image data is stripped to keep files manageable", so full screenshots may never persist in the JSONL at all. A privacy claim is exactly the kind of doc line users rely on; it is wrong in number and in mechanism.
- **Fix sketch:** Rewrite the README privacy bullet to state the real policy: images stripped from session files on write; residual screenshot payloads stripped after 2 days; sessions deleted after 30 days; link the Data pane.
- **Cross-territory?** no (docs-only fix)

## F-L-2: README claims "31 MCP tools" in three places; registry has 50
- **File:** README.md:17, README.md:73, README.md:102 vs mcp_server/tool_registry.py
- **Severity:** Medium
- **Class:** docs
- **Evidence:** README states "**MCP server:** 31 OS-control tools", the architecture diagram says "MCP server (31 OS tools)", and the test section says "(31 Vitest tests)". `mcp_server/tool_registry.py` defines **50** `Tool(` entries. Hard-coded counts rot; every one in README is already stale.
- **Fix sketch:** Replace hard counts with approximations ("50+ tools") or drop the numbers; better, add a doc-count CI check or generate the number.
- **Cross-territory?** no

## F-L-3: README says "License TBD" while an MIT LICENSE file ships at repo root
- **File:** README.md:139-151 vs LICENSE:1-3
- **Severity:** Medium
- **Class:** docs
- **Evidence:** README License section: "License TBD — see docs/archive/PRODUCTION_ROADMAP.md § P0-4" plus a `TODO(human): Choose a license before making this repo public`. The repo root contains `LICENSE` with "MIT License / Copyright (c) 2026 adam magued". Contradictory licensing signals are a legal-clarity problem for any consumer of the repo. Contributing section is likewise stale ("TBD pending license decision").
- **Fix sketch:** Update README License section to "MIT — see LICENSE"; remove the TODO comment; unblock/write the Contributing stub.
- **Cross-territory?** no

## F-L-4: justfile lacks the stranger-verbs `setup` and `test-all`; onboarding is copy-paste from README only
- **File:** justfile (repo root)
- **Severity:** Medium
- **Class:** docs (DX)
- **Evidence:** Recipes present: `default, check, typecheck, test, test-web, test-py, fake, dev`. There is **no `just setup`** — a newcomer must hand-run venv creation, `pip install -r requirements.txt`, `playwright install chromium`, `cp config.yaml.example`, `npm install` from README prose. Since nearly every recipe starts with `source venv/bin/activate`, a missing venv makes `just check`/`just test`/`just dev` fail with a raw shell error (see F-L-5). `just test` exists as the test-all equivalent (acceptable), but `setup` is genuinely absent.
- **Fix sketch:** Add a `setup` recipe: create venv, pip install, playwright install, npm install, cp config.yaml.example if absent; make other recipes print "run `just setup` first" when venv is missing.
- **Cross-territory?** no

---
(Continuing sweep: per-directory CLAUDE.md truth audit, HOW_TO.md, venv friendly-fail verification, error-message quality, logging/support-bundle, docs sprawl consolidation.)
