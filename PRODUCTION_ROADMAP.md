# Kim — Production Roadmap & Improvement Master Plan

**Date:** 2026-06-10 · **Branch:** `kim-improvement` · **App version:** 0.9.6
**Supersedes:** `IMPROVEMENT_PLAN.md` (mostly executed) and extends `HARNESS_ROADMAP.md`

This is the single document to read for two questions:

1. **What should we add to make Kim dramatically better?** (Part II)
2. **What is left before Kim is production-grade?** (Part I)

Everything below was verified against the actual repo state on 2026-06-10 — not guessed.

---

## Status snapshot — where Kim is right now

After the 2026-06 quality pass (commits `d58f19e` + `c4ff733`):

| Area | Before | Now | Notes |
|---|---|---|---|
| Architecture | 6.5/10 | **7.5/10** | God files split, dead subsystems removed, run-termination contract enforced |
| Code quality | 6/10 | **7.5/10** | 13 bugs fixed, typed exits, eval suite started |
| Test health | unknown | **green** | 816 pytest · 31 vitest · 50 cargo, tsc clean, cargo zero warnings |
| Web automation | 4/10 | **6.5/10** | `web_fill_form` composite tool, synonym resolver, FORM_SCHEMA, state diffs |
| Production readiness | — | **~5/10** | The gap is almost entirely *distribution, observability, and resilience* — not features |

**The headline:** Kim's *agent core* is now solid. What separates it from a shippable product is everything **around** the agent: how it installs, how it updates, how it fails, and how you find out when it fails on someone else's machine.

---

# PART I — The gap to production grade

Ordered by severity. P0 = cannot ship without it. P1 = can ship a beta, will hurt fast. P2 = expected of a real product within months.

## P0-1. The install story: Python is not bundled ⚠️ (the single biggest blocker)

**Current reality:** The Tauri app spawns `python -m orchestrator.agent` via `find_python_interpreter()` (`desktop/src-tauri/src/subprocess.rs`), resolving through `~/.kim_root` / `~/.kim` written by `install.sh`. This means a user must have:

- A working Python 3.11+ install
- A venv with `requirements.txt` installed (playwright, aiohttp, mss, pynput, pyautogui, PIL, …)
- Playwright browser binaries downloaded
- `install.sh` run successfully (macOS/Linux only — **no Windows installer exists**)

A normal user downloading `Kim.dmg` gets an app that **renders a UI and cannot run a single task.** This is the #1 thing standing between "works on my machine" and "product."

**Options (pick one):**

| Option | Effort | Verdict |
|---|---|---|
| **A. PyInstaller/PyOxidizer-frozen orchestrator binary**, bundled as a Tauri sidecar | High (2–3 wk) | ✅ **Recommended.** One binary per platform, shipped inside the bundle, versioned with the app. Playwright browsers fetched on first run with a progress UI. |
| B. Bundled standalone CPython (python-build-standalone) + vendored site-packages | Medium-High | Works, larger bundle (~150 MB+), fragile native deps (pynput, mss) |
| C. First-run bootstrapper that creates the venv automatically | Medium | Still requires system Python; flaky on user machines; best as a stopgap |
| D. Rewrite orchestrator in Rust | Very high | Not worth it now; revisit at v2 |

**Concrete tasks for Option A:**
- [ ] PyInstaller spec for `orchestrator.agent` + `mcp_server.server` (single binary, `--onedir` for startup speed)
- [ ] Tauri `externalBin` sidecar config; `find_python_interpreter()` gains a "bundled binary first" branch
- [ ] First-run flow: detect missing Playwright browsers → download with progress events to the UI
- [ ] CI builds the frozen orchestrator per-platform in `release.yml`
- [ ] Keep dev mode untouched (venv path still wins when running from source)

## P0-2. Auto-updater — not configured

**Verified:** `tauri.conf.json` has no `plugins.updater` section, no `createUpdaterArtifacts`, bundle targets are only `["app", "dmg"]`. `release.yml` builds artifacts but users have **no way to receive updates** short of re-downloading.

For an agent product that will ship fixes weekly, this is non-negotiable.

- [ ] Add `tauri-plugin-updater` + signing keypair (store private key in GitHub Actions secrets)
- [ ] `createUpdaterArtifacts: true`; release workflow uploads `latest.json` manifest
- [ ] In-app "Update available" toast → download → relaunch (the Settings → About pane already has a `check_for_updates` command stub to wire into)
- [ ] Decide channel strategy: `latest` (stable) + optional `beta` channel reading from the `kim-improvement`-style prerelease tags

## P0-3. Code signing & notarization

**Current reality:** No signing identity in `tauri.conf.json`. Unsigned macOS builds trigger Gatekeeper "damaged/unidentified developer" — most users will never get past it. Unsigned Windows builds trigger SmartScreen.

- [ ] Apple Developer ID cert + notarization (`APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID` secrets, `tauri-action` handles `notarytool` natively)
- [ ] Windows: at minimum a self-signed cert documented; properly an OV/EV cert (or Azure Trusted Signing, which is cheap now)
- [ ] The updater (P0-2) **requires** signing anyway — do these together

## P0-4. README and LICENSE do not exist

**Verified:** `kim-pro/` has no `README.md` and no `LICENSE` file. The GitHub repo landing page is empty.

- [ ] `README.md` — what Kim is, screenshot/GIF, install instructions, dev setup (`venv` + `npm` + `tauri dev`), architecture pointer to `ARCHITECTURE.md`
- [ ] `LICENSE` — **decision required:** open source (MIT/Apache-2.0) vs. source-available vs. proprietary. This gates everything about distribution; pick before publicizing the repo.
- [ ] `CONTRIBUTING.md` if open source

## P0-5. CI doesn't run on the working branch

**Verified:** `ci.yml` triggers on `main, develop, feature/**, fix/**` — the branch `kim-improvement` **never triggers CI**. All the green test runs so far were local.

- [ ] Add `kim-improvement` (or switch to `branches: ['**']` for push) to the trigger list
- [ ] Add a `workflow_dispatch` trigger so any branch can be tested on demand
- [ ] Merge `kim-improvement` → `main` once CI is green remotely (the branch is 2 commits ahead and has never been validated by CI)

## P1-1. Crash reporting & error observability — none exists

**Verified:** zero telemetry/crash-reporting deps in `Cargo.toml` or `requirements.txt`. When Kim fails on a user's machine, you will never know.

- [ ] **Sentry** (or self-hosted GlitchTip) in three layers: Rust (`sentry` crate, panic hook), Python (`sentry-sdk`, wrap the agent loop's top-level exception handler), React (ErrorBoundary already exists conceptually — add the SDK)
- [ ] Strictly **opt-in** with a first-run prompt; scrub task text and screenshots (agent payloads are extremely sensitive — only send stack traces + version + OS)
- [ ] Local structured logs: the orchestrator already emits `[STATUS]`/JSON events — add a rotating file handler (`logs/kim-YYYY-MM-DD.log`, keep 7 days) and a "Reveal logs" button in Settings → Feedback so users can attach them to bug reports

## P1-2. Graceful failure UX

The agent can fail dozens of ways (provider 429s, CDP browser not running, MCP server crash, Playwright timeout). Today most surface as raw text in the chat or a silent stall.

- [ ] Typed error taxonomy end-to-end: the `AgentTermination` enum now exists in Python — propagate it as a structured `kim:run_failed {reason, recoverable, suggestion}` event and render a **distinct error card** in ChatView with a one-click suggested action ("Start Chrome with debugging", "Re-authenticate Gemini", "Retry")
- [ ] Watchdog in Rust: if the Python subprocess produces no output for N minutes and isn't waiting on approval, surface "Kim seems stuck — view logs / restart task" instead of an infinite spinner
- [ ] Pre-flight checks before a run starts: provider auth valid? CDP reachable (browser provider)? MCP server bootable? Fail in 2 seconds with a clear message instead of 30 seconds into the loop
- [ ] Provider 429/529 backoff already exists in `_call_with_retry` — add UI surfacing ("Rate-limited, retrying in 20s…") instead of dead air

## P1-3. Permission & safety model for tool execution

Kim executes shell commands, clicks the user's real browser, and types keystrokes. Production users (and reviewers) will ask: *what stops it from `rm -rf`?*

Current state: `tool_risk.py` classifies risk tiers, `InteractionPolicy` gates some web actions, `shell.blocked_commands` is a config blocklist. Good bones, no UX.

- [ ] **Approval gates in the UI**: high-risk tool calls (`run_command` with destructive patterns, `delete_file`, payments-looking web forms) pause the run and render an Approve/Deny card. The plumbing for pausing already exists in the policy layer — it needs the frontend round-trip.
- [ ] Per-session permission modes like Claude Code: *Ask every time / Ask for risky only / Full auto* — a visible toggle in the chat header
- [ ] An immutable per-run **action log** (every tool + args + result digest) viewable after the run — this already exists as JSONL trace records; it needs a "View run log" UI
- [ ] Expand `blocked_commands` defaults (fork bombs, `mkfs`, `dd of=/dev/`, credential exfil patterns) — the translated-command recheck fix from the bug pass makes this enforceable now

## P1-4. Windows & Linux are untested in practice

`release.yml` builds all four targets, but nothing verifies the app *runs*. Known risk areas: `pynput`/`pyautogui` permissions differ wildly per OS; `install.sh` is Unix-only; window-management tools (`pygetwindow`) are Windows-biased; macOS needs Screen Recording + Accessibility permission prompts.

- [ ] One real smoke pass per OS: install → onboard → run a trivial task → run a web task
- [ ] macOS: first-run permission wizard (Screen Recording, Accessibility) with deep links to System Settings — without this, screenshots silently return black and the agent loops
- [ ] Windows installer story (NSIS target in Tauri bundle + the P0-1 sidecar)
- [ ] Document the support matrix honestly in the README ("macOS first-class, Windows/Linux beta")

## P1-5. Session data hygiene & privacy

Sessions store screenshots (base64) and full task text in plaintext JSONL under `kim_sessions/`. Screenshots of a user's desktop are about as sensitive as data gets.

- [ ] Retention policy: auto-prune session files older than N days (configurable), and strip screenshot payloads from sessions older than 48h (keep the text trace)
- [ ] "Delete all my data" button in Settings → Data (the pane exists)
- [ ] At minimum document what is stored where; ideally encrypt at rest using the OS keychain for the key
- [ ] Privacy note in README/onboarding: everything is local, nothing leaves the machine except LLM API calls (this is actually a *selling point* — say it loudly)

## P2-1. Test coverage where it's thin

816 Python tests are heavily unit-level. The riskiest seams have no coverage:

- [ ] **Tauri ↔ Python IPC integration test**: spawn the real orchestrator with a fake provider, assert the Rust side parses events correctly (this seam broke twice during the bug pass — it's the highest-value test in the repo)
- [ ] **Provider contract tests**: one shared test suite run against every provider's message-formatting layer (the Ollama tool-call-id bug would have been caught)
- [ ] **E2E smoke** (Playwright against the Vite dev build): app boots, settings open, a mocked task renders a full chat exchange
- [ ] Grow the **web eval suite** (`tests/evals/`) — it caught 3 real resolver bugs on day one; every web-automation change should add a fixture. Target: 50+ form/page fixtures harvested from real sites (GitHub, Google Forms, Amazon checkout-like, airline forms)
- [ ] Rust: `lib.rs` is down to 2078 lines but session-JSONL parsing and `find_code_backend()` resolution have no tests

## P2-2. Performance & footprint

Nothing here blocks shipping, but measure before users do:

- [ ] Cold-start time budget: Tauri window → first usable chat (target < 2s) and task-start → first agent token (target < 4s)
- [ ] Screenshot pipeline: every iteration ships a full PNG through base64 → JSON → stdout. Move to a shared temp-file handoff or at least JPEG at quality 80 — likely a 5–10× size cut on the hottest path
- [ ] Token spend meter per run (input/output counts already tracked in `_total_tokens`) — surface cost in the UI; users of API-key providers will demand it
- [ ] `index.css` split made styles maintainable; now audit for unused rules (the 2448-line chat.css almost certainly has dead selectors from removed components)

## P2-3. Docs

- [ ] User-facing: a 5-minute quickstart with GIFs (install, connect provider, first task, first web task)
- [ ] `ARCHITECTURE.md` exists and is good — keep it current as the IPC refactor lands
- [ ] Provider setup guides (Claude API key, Gemini OAuth, Ollama local/cloud, browser-CDP mode with the Chrome flag incantation)
- [ ] `CHANGELOG.md` exists — adopt keep-a-changelog format and cut it per release tag

---

# PART II — What would make Kim 10/10

These are ranked by **product impact per unit of effort**, not by coolness.

## A. Web automation v2 — from "can fill forms" to "feels psychic" 🥇

`web_fill_form` collapsed 7+ LLM round-trips into one. The next leaps, in order:

1. **Site playbooks (learned recipes).** When Kim completes a multi-step web task (create GitHub repo, book a slot, send a tweet), persist a compact recipe: URL pattern → ordered steps → field semantic names. Next time, replay the recipe with `web_observe` verification per step and **zero LLM planning calls** until something diverges. Store as YAML in `~/.kim/playbooks/`, user-editable. This is the single biggest "wow" feature available: second-time tasks become near-instant.
2. **Semantic element resolution.** The current resolver is lexical (token overlap + synonyms). Add a small local embedding model (e.g. `bge-small` via `fastembed`, ~30 MB) and score `intent ↔ element` by cosine similarity as a third signal. Kills the synonym-table whack-a-mole permanently.
3. **`web_extract` structured scraping tool.** "Get me the prices from this page" currently means screenshots + LLM eyeballing. A tool that takes a JSON schema and returns matching structured data from the DOM (readability-style extraction + table parsing) makes research tasks 10× cheaper and more accurate.
4. **Multi-page flow awareness.** `web_fill_form` handles one page. Wizards (checkout, signups) span pages. Add a `web_flow` composite: fill → submit → wait for navigation → re-observe → continue with remaining fields — one tool call per *flow*, not per page.
5. **Vision fallback.** When the DOM resolver returns nothing with text evidence (canvas apps, Flutter web, cross-origin iframes), fall back to a screenshot + grounding model click (the screen tools already exist). Resolver miss → vision attempt → only then ask the LLM.
6. **Stale-element auto-healing.** `InteractionPolicy` already tracks observation generations; on element-id mismatch, auto-re-observe and re-resolve by the original intent instead of surfacing an error to the model.

## B. First-class tool surface expansion 🥈

The TOOL ROUTING prompt change proved the principle: **a dedicated tool beats web automation every time it exists** (`github_create_repo` via `gh` is instant and deterministic; the web path takes 30s and can misclick). Grow the dedicated surface:

- [ ] **Google suite**: Gmail send/search, Calendar create/list, Drive upload — the Google OAuth plumbing (`google_oauth.rs`, Keychain refresh) *already exists*; it's wired to almost nothing
- [ ] **File conversions**: pdf↔text, image resize/convert, audio transcribe (whisper.cpp local) — agents get asked for this constantly
- [ ] **Clipboard tool** (read/write) — trivially easy, used in half of real desktop workflows
- [ ] **App-launch + deep-link tool** (`open -a` / `start` with URL schemes) — cheaper and more reliable than clicking the Dock
- [ ] A **tool-suggestion telemetry hook** (local only): log when the agent uses web automation for something, review the log monthly, promote the top patterns to dedicated tools

## C. Persistent agent memory 🥉

Kim has session storage but no *cross-session* memory. Every conversation starts cold.

- [ ] `~/.kim/memory/` — markdown facts file(s) the agent reads at startup and can append to via a `remember` tool: user's name, preferred editor, project paths, "always use dark mode on site X", GitHub username
- [ ] Auto-capture: after each successful run, one cheap LLM call extracts durable facts ("user's repos live in ~/Desktop/projects")
- [ ] Surfaced in Settings → Data with full user edit/delete control (privacy + trust)
- [ ] This compounds with playbooks (A1): memory = facts, playbooks = procedures

## D. Streaming + cost visibility

- [ ] **Token-streaming for API providers** (Claude/OpenAI/Gemini/Ollama all support SSE). Today the UI waits for complete responses; streaming makes the agent *feel* 3× faster with zero behavior change. The typed-event channel (`kim:*`) is ready to carry deltas.
- [ ] **Prompt caching** for Anthropic (cache the system prompt + tool schemas — they're identical every iteration). For long agent runs this is a 50–80% input-cost cut and significant latency cut.
- [ ] **Live cost meter** next to the ContextRing: tokens in/out × per-model price table = "$0.14 this run"

## E. Kill the dual IPC protocol (the deferred refactor)

Still the most important *architectural* item, deliberately deferred from the quality pass. The legacy text protocol (`[STATUS]`/`[TOOL]`/`[CONTEXT]` regex parsing in ChatView/parsers.ts) and the typed `kim:*` JSON events are dual-emitted everywhere. Every new feature pays a 2× emit tax and a regex-fragility tax.

- [ ] Inventory which UI elements still depend on text parsing (mostly ChatView activity feed + Codex JSONL passthrough)
- [ ] Move each to its typed equivalent; delete the regex parsers; single emit path
- [ ] Do this **before** building streaming (D) — streaming on top of dual-emit doubles the pain
- [ ] Estimate: 3–5 focused days; the typed events already exist, this is mostly frontend deletion

## F. Background & scheduled tasks, surfaced

A scheduling subsystem and `task_queue.py` exist in the codebase but have **no UI**. This is a differentiator sitting on the shelf:

- [ ] "Run this every morning at 9" → natural-language schedule capture → visible in a Scheduled pane with next-run time, last result, enable/disable
- [ ] Background runs that don't occupy the chat: a run-in-background toggle, with results landing as a notification + history entry
- [ ] OS-level notifications on completion/failure (Tauri notification plugin — trivial)

## G. The Code tab decision

Today: Codex CLI (external) or bundled `claw-code` fallback, driven through the browser-provider proxy. It works but it's a Rube Goldberg machine (temp CODEX_HOME, aiohttp proxy, JSONL scraping), and standing constraint applies (**never OpenAI auth / gpt-5.5 — only ollama cloud or browser provider**).

**Recommendation:** pick **one** backend (claw, since it's bundled and patchable) and delete the other path; or replace both with the Kim agent itself running with a code-tools-only toolset — the MCP server already has `code.py`, `git.py`, `files.py`, `search.py`. The latter unifies the product: one agent, one protocol, one UI renderer. Estimate 1–2 weeks, removes ~2k lines of bridge code.

## H. Voice: kill or restore (stop carrying the corpse)

`tray/` is gone; `agent.py` still try-imports `tray.voice`; Settings still shows a Voice pane with kokoro/maya1/http/hume engine options that do nothing.

- **Kill** (recommended for 1.0): delete the try-import, the settings pane, and the config keys. 1 hour. Restore later as a feature with TTS via the typed event channel.
- **Restore**: re-home `voice.py` under `orchestrator/voice.py`, wire `_voice_speak` to it, gate behind the existing config. 1–2 days including per-OS audio testing.

Either is fine; shipping a settings pane that silently does nothing is not.

## I. Onboarding & first-run experience

Currently: install → blank chat. A user's first 5 minutes decide everything.

- [ ] First-run wizard: pick provider → paste key / OAuth / detect Ollama → grant OS permissions (macOS screen recording etc.) → **run a guaranteed-success demo task** ("take a screenshot and describe my desktop")
- [ ] Empty-state chat suggestions (3–4 clickable example tasks tuned to what Kim is reliably good at)
- [ ] Provider health indicators in the header (auth OK / CDP reachable) so failures are visible *before* a task is typed

## J. Relay / mobile — decide the boundary

`relay_server/` (FastAPI), `relay.rs`, the pairing UI, and `railway.toml` deployment all exist and were deliberately kept. But it's a half-product: no documented mobile client story.

- **If keeping:** define the v1 scope (phone sends task → desktop runs → phone sees result), add auth hardening + E2E pairing test, document deployment
- **If dropping for 1.0:** hide the pane behind a feature flag (don't delete — it's real code) and revisit post-launch
- Either way, stop shipping it in limbo.

---

# PART III — Open product decisions (need an answer from you)

| # | Decision | Options | My recommendation |
|---|---|---|---|
| 1 | License / open-source? | MIT · Apache-2.0 · source-available · closed | Apache-2.0 if you want community; closed if you might sell it. **Gates P0-4.** |
| 2 | Python packaging | PyInstaller sidecar · bundled CPython · bootstrap venv | PyInstaller sidecar (P0-1 Option A) |
| 3 | Code tab backend | Codex CLI · claw only · Kim-native code agent | Kim-native (G) — biggest simplification available |
| 4 | Voice | Kill now, restore later · restore now | Kill now (H) |
| 5 | Relay in 1.0 | Feature-flag off · finish it | Flag off, finish post-1.0 (J) |
| 6 | Dual IPC | Kill legacy text protocol | Yes — before streaming work (E) |
| 7 | Support matrix at launch | macOS-only 1.0 · all three OSes | macOS-first 1.0, Windows/Linux labeled beta (P1-4) |

---

# PART IV — Suggested sequencing

### Phase 1 — "Installable" (~2–3 weeks)
P0-1 Python sidecar → P0-3 signing → P0-2 auto-updater → P0-4 README/LICENSE → P0-5 CI branch fix + merge to main.
**Exit test:** a stranger downloads the DMG, opens it, runs a task. Nothing else matters until this passes.

### Phase 2 — "Trustworthy" (~2 weeks)
P1-1 crash reporting + log files → P1-2 failure UX + pre-flight checks → P1-3 approval gates → P1-5 data retention → I onboarding wizard.
**Exit test:** unplug the network mid-task, kill Chrome mid-web-task, revoke a key — every failure produces a clear, actionable card.

### Phase 3 — "Fast & coherent" (~2 weeks)
E kill dual IPC → D streaming + prompt caching + cost meter → H voice decision executed → G code-tab consolidation.
**Exit test:** one emit path, visibly streaming responses, cost shown per run.

### Phase 4 — "Magic" (ongoing)
A playbooks + semantic resolver + web_extract → C cross-session memory → B Google-suite tools → F scheduling UI.
**Exit test:** "make a private repo called X" the *second* time completes in under 10 seconds.

---

# Appendix — Health snapshot (2026-06-10)

- **Tests:** 816 pytest · 31 vitest · 50 cargo — all green locally; **never validated in CI** (P0-5)
- **Builds:** tsc clean · Vite production build OK · cargo zero warnings
- **CI:** frontend + rust + python jobs exist in `ci.yml`; release matrix covers macOS arm64/x64, Linux x64, Windows x64 + `kim` CLI binaries
- **Known-kept tech debt:** dual IPC emit · relay half-product · claw/codex dual backend · `tray.voice` dead import · `web_resolve` lexical-only scoring
- **Recently fixed (don't re-fix):** all 13 audit bugs (B1–B13) · god-file splits (lib.rs, RevampSettings.tsx, index.css) · run-termination contract · loop guard + perceptual stuck detection · `web_fill_form` + resolver text-evidence gate
- **Standing constraint:** Code tab must never use OpenAI auth or gpt-5.5 — ollama cloud or browser provider only
