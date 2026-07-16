# kimcli feature matrix — ship-gate checklist

This is the per-provider ship-gate for kimcli (Kim's rebranded, pinned
codex-cli 0.144.3 build — see `docs/kimcli.md` for the fork/branding/routing
background). Every cell states the **expected mechanism** and the **test
ID(s)** that back it — `A-*` for something this repo actually runs offline
today, `M-*` for a manual QA step with a one-line instruction. A cell with
only `M-*` IDs means: this behavior cannot be verified offline in CI (it
needs a live LLM, a live terminal, or a live external daemon) — that is
stated honestly rather than papered over.

## Surfaces (read this before the table — it explains why so many kim tui
rows are M-only)

kimcli is driven through **three different surfaces** in Kim, and most rows
below only have automated coverage on one or two of them:

1. **`kim tui`** — the Rust launcher (`cli/src/commands/tui/`) spawns the
   real kimcli binary attached to a live terminal, in its own native
   interactive mode. Slash commands (`/model`, `/new`, `/compact`,
   `/approvals`, `/diff`, …), streaming render, the plan-tool sidebar, image
   display, and notifications are **entirely internal to the vendored
   codex-rs TUI** — kim-pro carries none of that source (it lives in the
   `AdamMagued/codex` fork) and cannot drive it headlessly. These are M-only
   almost everywhere below.
2. **`exec --json`** — kimcli/codex's non-interactive JSONL mode. This is
   the desktop app's legacy Code-tab transport
   (`orchestrator/codex_bridge_service.py`'s `_run_exec_task`) and is fully
   scriptable offline — `tests/test_kimcli_binary.py` (new in this change)
   drives it against the REAL binary.
3. **app-server (JSON-RPC over stdio)** — the desktop app's *default*
   Code-tab transport (`codex_appserver_transport.py`). Also fully
   scriptable offline — `tests/test_appserver_real_binary.py` drives it
   against the REAL binary (currently only exercised with `browser:gemini`,
   3 tests).

Mechanism vocabulary used in the table:

- **native** — kimcli/codex's own built-in behavior, unmodified by Kim; Kim
  is not in the data path (or is only an auth/routing pass-through that
  preserves item structure).
- **proxy-translated** — `codex_engine/engine.py`'s browser-contract mode
  rewrites codex's Responses-API items to/from Kim's own JSON contract so a
  browser-hosted LLM (no native function-calling) can participate.
- **parity-tool** — a Kim-authored MCP tool that stands in for a codex
  native capability the browser-contract path can't get natively (e.g.
  `web_search` as a Kim MCP tool, since a browser LLM has no built-in search
  tool the way an API model does).
- **n/a** — the feature has no route on this surface/column combination
  (stated explicitly, not left blank).

## Test ID legend

### Automated (`A-*`)

| ID | Test | What it proves |
|---|---|---|
| A-1 | `tests/test_kimcli_binary.py::VersionBrandingTests::test_version_matches_rebrand_pattern` | Real kimcli binary prints `kimcli X.Y.Z (rebranded codex-cli X.Y.Z)`. Skipped when the resolved binary is the codex stand-in. |
| A-2 | `tests/test_kimcli_binary.py::UpdateRefusalTests::test_update_refuses_with_nonzero_exit` | `kimcli update` exits non-zero (Kim owns the version pin). Skipped for the codex stand-in (which legitimately self-updates). |
| A-3 | `tests/test_kimcli_binary.py::ResponsesPassthroughExecTests::test_full_turn_completes_with_canned_reply` | Real binary, real `standalone_proxy --provider fake` subprocess, `exec --json -c model_providers.kim-proxy.*` (the desktop app's exec-transport shape) — full request/response loop round-trips and the canned reply reaches `item.completed`/`agent_message`. |
| A-4 | `tests/test_kimcli_binary.py::McpAttachSmokeTests::test_mcp_server_listed_as_kim_and_enabled` | `kimcli mcp list -c mcp_servers.kim.*` (real binary, isolated `CODEX_HOME`) reports a `kim` row, `enabled`, with the exact command/args/cwd/env `kim tui`'s launcher builds (`argv.rs`'s `mcp_kim_overrides`, replicated in the test). |
| A-5 | `tests/test_kimcli_binary.py::McpAttachSmokeTests::test_full_turn_completes_with_mcp_attached` | Same as A-3, plus `-c mcp_servers.kim.*` attached — proves MCP attach doesn't break a real turn. (The canned FakeProvider can't itself call a tool, so tool *invocation* by a real model is M-6, not this.) |
| A-6 | `tests/test_appserver_real_binary.py::RealBinaryAppServerSmoke::test_turn_executes_command_and_persists_thread_then_resumes` | Real binary, app-server transport, `browser:gemini`: a scripted tool call really executes inside the sandboxed cwd, `codex_thread_id` persists to the sidecar, and the NEXT message resumes the same codex thread (session continuity). |
| A-7 | `tests/test_appserver_real_binary.py::RealBinaryAppServerSmoke::test_native_approval_declined_blocks_escalated_command` / `test_native_approval_accept_runs_escalated_command` | Real binary's native `require_escalated` → `item/commandExecution/requestApproval` protocol round-trip (decline blocks, accept runs) — proves the approval *protocol*, not the interactive TUI's own approval dialog rendering. |
| A-8 | `tests/test_responses_passthrough.py` (golden translation suite) | `responses_request_to_canonical` / `canonical_reply_to_responses_parts` — item-structure-preserving translation codex 0.144.3 requires on `/v1/responses` for every API provider (no chat-completions fallback exists in this codex version). |
| A-9 | `tests/test_codex_proxy_modes.py` | Mode defaults/relay-cap/delta-cursor behavior of `_CodexProxy` shared by every routed provider. |
| A-10 | `tests/test_browser_parity_tools.py::WebSearchAndAskUserTests::test_web_search_returns_provider_native_request` | Kim's `web_search` MCP parity tool (browser path) returns the "ask the browser LLM to search natively" contract Kim actually ships. |
| A-11 | `tests/test_view_image_tool.py` | Kim's `view_image` MCP tool — data-URI shape, size/type guards, secret-path sandbox denial. |
| A-12 | `tests/test_gemini_oauth_provider.py`, `tests/test_gemini_user_project_mode.py` | Gemini's two auth modes (API key vs Kim OAuth bearer) at the provider-adapter layer feeding `responses-passthrough`. |
| A-13 | `cli/src/commands/tui/argv.rs` unit tests (`cargo test -p kim-cli`) — `proxy_route_argv_matches_the_trusted_design_exactly`, `ollama_direct_argv_matches_the_trusted_design_exactly` | The exact `-c` override argv `kim tui` builds per route (proxy vs. ollama-direct) — argv shape only, does not itself spawn kimcli. |
| A-14 | `cli/src/commands/tui/routing.rs` unit test `routing_table_ollama_is_direct_everything_else_is_proxy` | `ollama` (exact, case-insensitive) is the only provider name that takes the direct (no-proxy-hop) route. |
| A-15 | `tests/test_e2e_smoke.py` | Full bridge→proxy→codex relay loop, SSE framing, bearer auth — codex side is a **scripted fake binary** (not the real kimcli/codex), so this is wire-shape proof, not real-binary proof. |

### Manual (`M-*`)

| ID | One-line instruction |
|---|---|
| M-1 | Launch `kim tui --provider <X>`, send a multi-turn chat, confirm tokens stream live (not buffered) and render correctly. |
| M-2 | In the live TUI, run `/model`, confirm the picker lists this provider's models and switching actually changes the next reply's model. |
| M-3 | Trigger a shell command requiring escalation; confirm the TUI's own approval dialog renders and both Approve/Deny act correctly; separately confirm `-s workspace-write` / `-s read-only` / `-s danger-full-access` behave as documented. |
| M-4 | Run `/new`, confirm the conversation visibly clears and the next message starts a fresh codex thread (new `thread_id`). |
| M-5 | Run `/compact` in a long TUI session, confirm a summary is produced and context shrinks without losing task continuity. |
| M-6 | With `-c mcp_servers.kim.*` attached (or via `kim tui`), ask the model to actually use a Kim tool (e.g. "take a screenshot"); confirm the tool call round-trips and its result reaches the model. |
| M-7 | Paste/attach an image in the TUI; confirm the model can describe it (image input) and that a model-issued `view_image` call on a local file renders inline. |
| M-8 | Ask a question that should trigger web search; confirm the provider's native search UI/citations appear (API providers) or the browser LLM's own search affordance is used (browser providers) — no Kim-side search result should be silently substituted. |
| M-9 | Ask for a multi-step task; confirm the plan tool's step list renders and updates live in the TUI sidebar. |
| M-10 | Ask the model to edit a file; confirm `apply_patch` produces a real diff, applies cleanly, and `/diff` reflects it. |
| M-11 | Ask the model to run `git status`/`git diff`/commit inside a real repo; confirm native git integration (not a Kim MCP git tool) is what runs. |
| M-12 | Trigger a long-running/background task; confirm any desktop notification fires on completion. |
| M-13 | Run `/resume` (or `--resume` / the session picker) in a live TUI; confirm the prior session's messages reload and continuation works. |
| M-14 | `api:ollama(direct)` only: with a local Ollama ≥ 0.13.4 running, launch `kim tui --provider ollama`, confirm chat works with zero Kim-proxy hop (kill the proxy process and confirm the session is unaffected, since this route never spawns one). |
| M-15 | `browser:*` only: confirm the exact site (claude.ai / chatgpt.com / gemini.google.com) opens for `preferred_site` and that `browser-contract` mode's nudge/salvage narration appears in logs, not stdout. |

## The matrix

| Row | browser:claude | browser:chatgpt | browser:gemini | api:claude | api:gemini(key) | api:gemini(oauth) | api:deepseek | api:ollama(direct) |
|---|---|---|---|---|---|---|---|---|
| **Interactive TUI chat + streaming** | proxy-translated — M-1 | proxy-translated — M-1 | proxy-translated — M-1 | native — M-1 | native — M-1 | native — M-1 | native — M-1 | native — M-1 |
| **`/model`** | native (TUI-internal) — M-2 | native — M-2 | native — M-2 | native — M-2 | native — M-2 | native — M-2 | native — M-2 | native — M-2 |
| **`/approvals` + sandbox modes** | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 | native protocol (A-7), TUI render M-3 |
| **`/new`** | native (TUI-internal) — M-4 | native — M-4 | native — M-4 | native — M-4 | native — M-4 | native — M-4 | native — M-4 | native — M-4 |
| **`/compact`** | native (TUI-internal) — M-5 | native — M-5 | native — M-5 | native — M-5 | native — M-5 | native — M-5 | native — M-5 | native — M-5 |
| **`exec --json`** | proxy-translated — A-15 (fake binary only); M-1-class real-binary gap noted below | proxy-translated — A-15 (fake binary only) | proxy-translated — A-15 (fake binary only) | native, item-structure preserved — **A-3**, A-8, A-9 | native — **A-3**, A-8, A-9, A-12 | native — **A-3**, A-8, A-9, A-12 | native — **A-3**, A-8, A-9 | n/a — exec transport has no ollama-direct branch (only `kim tui`'s launcher does; via exec, "ollama" is just another responses-passthrough provider) |
| **resume / sessions** | proxy-translated — A-6 (app-server surface); TUI `/resume` M-13 | proxy-translated — A-6; M-13 | proxy-translated — **A-6**; M-13 | native — A-6-class coverage is browser-only today; M-13 | native — M-13 | native — M-13 | native — M-13 | native — M-13 |
| **Kim MCP tools listed + callable** | native kimcli MCP client, provider-agnostic — **A-4**, A-5; invocation M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 | native — A-4, A-5; M-6 |
| **Image input / `view_image`** | proxy-translated (image parts flattened into contract text) — A-11 (`view_image` tool only); M-7 | proxy-translated — A-11; M-7 | proxy-translated — A-11; M-7 | native — A-11; M-7 | native — A-11; M-7 | native — A-11; M-7 | native — A-11; M-7 | native — A-11; M-7 |
| **Web search** | parity-tool (provider-native browser search, not a Kim result) — **A-10**; M-8 | parity-tool — A-10; M-8 | parity-tool — A-10; M-8 | native (codex's own web_search tool) — M-8 | native — M-8 | native — M-8 | native — M-8 | n/a — no local model web-search tool; M-8 confirms the honest absence |
| **Plan tool** | proxy-translated (update_plan relayed through the JSON contract) — M-9 | proxy-translated — M-9 | proxy-translated — M-9 | native — M-9 | native — M-9 | native — M-9 | native — M-9 | native — M-9 |
| **`apply_patch` / edits** | proxy-translated — M-10 | proxy-translated — M-10 | proxy-translated — M-10 | native — M-10 | native — M-10 | native — M-10 | native — M-10 | native — M-10 |
| **Git integration** | proxy-translated (shell tool runs `git`) — M-11 | proxy-translated — M-11 | proxy-translated — M-11 | native — M-11 | native — M-11 | native — M-11 | native — M-11 | native — M-11 |
| **Notifications** | native (TUI-internal) — M-12 | native — M-12 | native — M-12 | native — M-12 | native — M-12 | native — M-12 | native — M-12 | native — M-12 |
| **`update` refusal** | n/a per-provider (binary-level check, before any routing) — **A-1**, **A-2** | A-1, A-2 | A-1, A-2 | A-1, A-2 | A-1, A-2 | A-1, A-2 | A-1, A-2 | A-1, A-2 |

Column-specific notes not otherwise obvious from the table:

- **`browser:*` `exec --json`**: A-15 proves the wire shape (fake binary,
  not real kimcli/codex) — there is currently no automated test that runs
  the REAL binary through `exec --json` in `browser-contract` mode (only
  `responses-passthrough`, via A-3/A-5, and app-server, via A-6/A-7, are
  covered against the real binary). This is a genuine gap, tracked as a
  follow-up rather than papered over; `A-6`/`A-7` (app-server, real binary,
  `browser:gemini`) is the closest real-binary proof that the browser-
  contract loop works end-to-end against this codex build.
- **`api:ollama(direct)`**: this route only exists in `kim tui`'s Rust
  launcher (`argv.rs`/`routing.rs`, A-13/A-14) — the Python `exec --json`
  legacy transport (`codex_bridge_service.py`) has no ollama-direct branch
  at all, hence "n/a" rather than a mechanism, in that row. An Ollama
  behind the Kim proxy (a *different*, non-direct configuration) would
  instead be just another `responses-passthrough` provider.
- **`resume/sessions`**: A-6's continuity proof was written against
  `browser:gemini` only; the underlying sidecar (`codex_engine/
  thread_state.py`) and app-server transport are provider-agnostic, so the
  same mechanism applies to every column, but only one column has an actual
  automated run backing it today.

## Known caveats (from `docs/kimcli.md`)

- **Web search on browser providers is provider-native** — Kim's
  `web_search` MCP parity tool (A-10) asks the browser LLM to use its own
  built-in search; Kim does not run or inject search results itself.
- **Approvals default-deny for Kim's own MCP tools under kimcli, pending
  #64** — this is distinct from the row above: codex's *native* command-
  execution approval protocol (A-7) works today; Kim's own MCP
  tool-approval risk tiers (e.g. a medium-risk Kim tool call) have no
  broker wired into the app-server transport yet, so an approval request
  for one of Kim's own tools currently default-denies rather than prompting.
- **Parallel tool calls are sequential on proxied API providers** —
  `responses-passthrough` handles one `function_call` per canonical turn
  (a `"batch"` reply is expanded into several serial entries, not executed
  concurrently); this is a translation-layer property, not a per-provider
  bug.
- **`kimcli app-server daemon`** retains upstream's managed-install path
  (can download a separate upstream `codex` binary under its own
  management dir); Kim does not invoke this subcommand today.
- **Unsigned binaries** — macOS needs the installer's quarantine-strip
  step; Windows may SmartScreen-prompt on first run.
- **`FREE_GO_TOOLTIP`** still shows upstream's "included in your plan"
  wording in some TUI flows (a branding-grep miss, not yet fixed).

## How to run the manual pass

1. Install the real binary one of two ways:
   - `./scripts/install_kimcli.sh` (pulls the pinned `kimcli-v0.144.3`
     release into `~/.kim/bin/kimcli`), or
   - point Kim at a local build: `export CODEX_BIN=/path/to/local/kimcli`
     (or `KIMCLI_BIN` for the test suite specifically).
2. For each provider column, launch: `kim tui --provider <name>` — e.g.
   `kim tui --provider browser:claude`, `kim tui --provider claude`,
   `kim tui --provider gemini` (API-key mode is config-driven; OAuth mode
   requires a signed-in Kim desktop session so Tauri can inject
   `KIM_GOOGLE_ACCESS_TOKEN`), `kim tui --provider deepseek`,
   `kim tui --provider ollama` (requires a local Ollama ≥ 0.13.4 daemon —
   see docs/kimcli.md's Ollama caveat).
3. Work through the `M-*` instructions above for every row that applies to
   that column; note the mechanism actually observed (native / proxy-
   translated / parity-tool) against what this matrix expects, and file a
   deviation if it doesn't match.
4. Re-run `python -m pytest tests/test_kimcli_binary.py tests/
   test_appserver_real_binary.py -v` against the same binary first — a red
   automated cell means don't bother starting the manual pass until it's
   fixed.
