# Architecture

Kim is a local AI agent platform for cross-platform OS control. This document details the monorepo layers, data flows, communication protocols, and preserved design decisions of the active system.

---

## 1. Tauri 2 Desktop App Structure

The desktop application is built with a high-performance hybrid architecture using **Tauri v2** as the shell and **React 19** for the front-end user experience.

### Front-end Layer (React 19 + TypeScript)
- **State Orchestration**: Managed in `App.tsx` and custom React hooks (e.g. `useSessions`, `useAccount`, `useTheme`).
- **UI Views**: Main chat view is orchestrated by `ChatView.tsx` with revamp sidebar panels (`RevampSidebar.tsx` and `RevampSettings.tsx`).
- **Styles**: Unified style system using global CSS and core token mappings in `styles/design-tokens.css`.

### Backend Layer (Rust)
The Rust Tauri backend provides a secure, sandboxed bridge to the OS, handling configuration files, spawning worker subprocesses, and exposing commands to the React UI:
- `main.rs`: Application entry point and Tauri application bootstrap configuration.
- `lib.rs`: The central Tauri command router and subprocess manager (spawns the Python agent).
- `google_oauth.rs`: Secure PKCE OAuth 2.0 desktop loopback flow management.
- **Command Modules**: Specific command routines extracted into distinct modules (`account.rs`, `ollama.rs`, `session_commands.rs`, `feedback.rs`, etc.).
- `build.rs`: Native desktop build configuration scripting.

---

## 2. Python Orchestrator & MCP Server

The intelligence and action engine of the platform is written in Python and operates as a standalone agent runloop with Model Context Protocol (MCP) integrations.

```
┌─────────────────────────────────────────────────────────────┐
│                 Tauri React UI (ChatView)                   │
└──────────────┬──────────────────────────────▲───────────────┘
         stdin │ (Task Params)                │ Tauri Events
               ▼                              │ (stdout lines)
┌─────────────────────────────────────────────┴───────────────┐
│              Tauri Rust backend (lib.rs)                    │
└──────────────┬──────────────────────────────▲───────────────┘
         stdin │ Task Prompt                  │ stdout (Lines)
               ▼                              │
┌─────────────────────────────────────────────┴───────────────┐
│        Python Agent Orchestrator (agent.py Loop)            │
├──────────────────────────────┬──────────────────────────────┤
│                              │                              │
│  LLM Providers (base.py)     │  MultiMCPClient (stdio)      │
│  - claude.py                 │                              │
│  - openai_provider.py        │  Local MCP Server (server.py)│
│  - gemini.py / deepseek.py   │  - tool_registry.py          │
│  - browser/provider.py       │  - 50 OS Control Tools       │
└──────────────────────────────┴──────────────────────────────┘
```

- **KimAgent Loop (`agent.py`)**: An asynchronous iteration loop that takes task inputs, manages history compaction, prunes screenshots, requests choices from LLM Providers, and executes actions.
- **Local MCP Server (`server.py`)**: Communicates with the orchestrator over a secure stdio transport. Exposes **51 OS-control tools** grouped across files, shell command exec, mouse/keyboard inputs, window focal states, screen vision, browser interactions, search/grep, and git tools.

---

## 3. Communication Protocols & IPC

The system has four distinct IPC surfaces between layers.

### Surface A — Tauri events (Rust Backend → React UI)
Five events emitted by the Rust backend over the Tauri event bus:
1. `kim-agent-output` (string): Live stdout text stream from the agent.
2. `kim-agent-error` (string): Subprocess stderr output logs.
3. `kim-agent-done` (boolean): Signal confirming runloop completion.
4. `kim-agent-cancelled` (boolean): Signal that a running task was aborted.
5. `kim-agent-code-session` (SessionInfo): Session data emitted when a code workspace initializes.

Canonical event shapes are defined in `desktop/src/types/events.schema.json`; TypeScript types are generated from it via `scripts/gen-events.js` into `desktop/src/types/events.gen.ts` (codegen is live and checked by CI drift check). Events are also parsed from the raw stdout text protocol below.

### Surface B — Stdout Text Protocol (Python → Rust Backend)
The agent loop formats outputs printed to stdout, which `subprocess.rs` captures line-by-line and forwards via the Tauri event bus:
- `[STATUS] <message>`: Live status updates (e.g. "Taking a screenshot...", "Running command...").
- `[PLAN]{json_array}`: Emits a structured JSON array representing planned workflow steps.
- `[STEP N]:{json_object}`: Emits details of the current step $N$ (status, tool parameters, outcome).
- `[DONE N]`: Signals completion of the step $N$.
- `[CONTEXT]{json_object}`: Cumulative context usage (input token meter status and compaction metrics).
- `[UI] SCREENSHOT_FLASH`: Triggers a full-window screen-capture camera flash transition in the UI.
- `[UI] SHOW`: Restores/shows the main Tauri window.

### Surface C — HTTP Bridge (`http_bridge.rs`, `/v1/*` endpoints)
`desktop/src-tauri/src/http_bridge.rs` runs a `tiny_http` server on a fixed local port (authenticated by `X-Kim-Token`). Key endpoints:
- `POST /v1/task` — spawns a new agent subprocess (used by the Code tab / `codex_bridge_service`).
- `POST /v1/send` and `GET /v1/result/{reqId}` — split send/receive for browser-provider LLM calls (bridge.js → Python → bridge.js round-trip).
- `POST /v1/complete` — legacy single-call variant of send/receive.
- `POST /v1/callback` — signals to the waiting condvar that the WebView bridge has initialized.
- `GET /v1/status`, `/v1/health`, `/v1/ping` — status/health probes.
- `/v1/browser/*` — current URL, metadata, commit-URL, and restore for in-app WebView state.
- `POST /v1/cancel`, `/v1/hide`, `/v1/show`, `/v1/open` — task lifecycle and window controls.

### Surface D — WebView Automation Bridge (`browser_bridge.rs` + `bridge.js`)
`desktop/src-tauri/src/browser_bridge.rs` manages a persistent in-app WebView window (`kim-browser-automate`) and injects `bridge.js` (extracted from `lib.rs` and loaded via `include_str!("bridge.js")`). The injected script intercepts outgoing fetch/XHR calls inside the target site (Claude, ChatGPT, Gemini) and re-routes them through the `/v1/send` HTTP bridge so the Python `BrowserProvider` can intercept LLM responses without a Playwright subprocess.

### Surface E — Phone Relay (removed)
The phone-relay subsystem (relay_server/, `relay.rs`, the hidden settings pane) was decommissioned in Phase 0 of `docs/ROADMAP_TO_10.md` (A5/S6): it was never enabled and shipped a deployable server that was pure attack surface. Git history preserves the code if it is ever redesigned.

---

## 4. Codex Bridge Flow

The Code workspace uses a highly coordinated multi-layered proxy pipeline to execute sandboxed browser prompts:

```
Tauri React UI (Code Tab)
  → Tauri Backend (subprocess.rs / http_bridge.rs /v1/task)
    → subprocess (python -m orchestrator.codex_bridge_service)
      → codex_bridge_service.py (setup proxy + launch CLI, with atexit/SIGTERM cleanup)
        → _CodexProxy (aiohttp proxy server on ephemeral port)
          → Codex CLI (codex exec --json)
            → BrowserProvider.complete() (scrapes browser using CDP)
```

The 5 distinct execution layers:
1. **React Code Tab**: Submits tasks using Tauri RPC; Tauri may use `/v1/task` (HTTP bridge) to spawn the subprocess.
2. **Tauri Subprocess Launch**: Spawns `orchestrator.codex_bridge_service` (the consolidated bridge module; replaced the legacy `run_codex_bridge.py` entrypoint). It imports the bridge engine from the top-level `codex_engine/engine.py` package — a normal sibling import with no `sys.path` manipulation (resolution comes from `PYTHONPATH=kim_root`).
3. **`codex_bridge_service.py`**: Starts an in-process `_CodexProxy`; registers `atexit`/`SIGTERM` handlers for cleanup; uses a `tempfile.TemporaryDirectory` context for scratch files.
4. **_CodexProxy (aiohttp)**: Intercepts OpenAI-format API endpoints and maps them to local execution structures.
5. **Codex CLI & BrowserProvider**: Connects to the browser provider using Chromium Developer Tools Protocol (CDP) to drive prompts in the live browser.

### 4.1 App-server transport (`codex_bridge.transport: app-server`)

Behind a config flag (default is still `exec`), layer 5 is replaced by
`codex app-server` — newline-delimited JSON-RPC over stdio (client:
`codex_engine/app_server.py`; transport: `orchestrator/codex_appserver_transport.py`):

```
codex_bridge_service.py ── transport branch
  → AppServerClient (codex app-server, JSON-RPC 2.0)
      thread/resume(codex_thread_id from the sidecar) or thread/start
      → one turn/start per user message
      → notifications → typed Kim events (plan/output/diff/tokens/deltas)
      → item/commandExecution/requestApproval → kim:command-approval-request
         → BLOCKS on the stdin decision line {type: approval_decision, id, decision}
  → _CodexProxy unchanged (model calls still go to the browser LLM)
```

What changes vs `exec`: native per-command approvals (accept /
acceptForSession / decline) inside a `workspace-write` sandbox
(`KIM_CODEX_BYPASS_SANDBOX` is ignored on this path); true session resume via
the persisted `codex_thread_id`; the browser is **model-only** — tool
execution happens natively in codex. Protocol contract is pinned by
`codex_engine/appserver_schema/` (regenerate + re-probe with
`scripts/probe_appserver.py` on codex upgrades).

---

## 5. Browser Provider Mechanics

For API-key-free execution, the `BrowserProvider` drives prompts directly inside live browsers (Claude, ChatGPT, Gemini) via Playwright using CDP on port `9222`:
- **Prompt Injection**: Injects user prompts by automating clipboard pasting and typing.
- **Scraper DOM Parsing**: Scrapes answers directly from target page DOM elements based on selectors listed in `site_configs.py`.
- **Response Sentinel**: The provider terminates all visual scraping cycles with a specific `[END_OF_RESPONSE_{hash}]` signature. This allows the parser to cleanly identify where a streaming response terminates.

---

## 6. Configuration Sources

The system resolves parameters at runtime from four key layers:
1. `config.yaml`: Canonical local runtime config (e.g. models selection, MCP ports, execution preferences).
2. `.env`: OS environmental variables storing access secrets (GitHub PAT, Google client keys).
3. `tauri.conf.json`: Tauri application details, package versions, and window boundaries settings.
4. `lib.rs` Constants: Low-level fallback parameters (such as WebView labels and IPC limits).

---

## Preserved decisions (from agent handoff archives)

1. **Browser LLM sign-in detection UX**:
   - The in-app WebView window for Google/Gemini/Claude authentication is labeled `"kim-browser-signin"`. Heuristic detection categorizes URL pages using provider domain matching while checking for signin/login substrings to determine if a user is "likely signed in".
   - The React frontend uses the `get_browser_current_url` Tauri command to fetch the WebView URL and dynamically update user-facing prompts/toasts without automating user credentials.

2. **Context Budget and compaction**:
   - A cumulative input token budget is configured via `config.context_budget_tokens`, `KIM_CONTEXT_BUDGET_TOKENS` environment variable, or default `200_000`.
   - Budget states are segmented into `ok`, `warn` (≥80%), and `critical` (≥95%). Estimated token usage is calculated for Browser providers, but exact `[STATS]` output is withheld to prevent UI pill conflicts.
   - Compaction prompts (`__KIM_COMPACT_CONTEXT__`, `compact`, or `/compact`) execute inline LLM summarization, clear active memory, and prompt BrowserProvider with `clear_chat=True` on the subsequent completion request.

3. **Gemini API user-owned OAuth**:
   - A direct API-based `provider: gemini` runs alongside `browser:gemini`. It implements a Google Cloud OAuth PKCE loopback mechanism to store user OAuth credentials (in secure OS storage) rather than relying on developer-provided global credentials. 
   - Authenticators pass these tokens down to the agent environment under the `KIM_GOOGLE_ACCESS_TOKEN` variable.

4. **Multi-login mapping via `authuser`**:
   - Google account multi-login profiles linked within `KimAccount` store `{ email, authuser_index }` pairings.
   - The active identity's `authuser_index` propagates down to the browser bridge (`authuser` key) via the `/v1/send` endpoint, driving WebView loads to URLs matching `?authuser=N`.

5. **HTTP bridge security**:
   - The `/v1/*` HTTP bridge (`http_bridge.rs`) is authenticated via a per-session `X-Kim-Token` header checked using constant-time comparison to prevent timing attacks. `/v1/health` is the only unauthenticated endpoint.
   - Hardcoded OAuth credentials have been removed from source. Google OAuth relies exclusively on env vars (`GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`) via `google_oauth.rs`, falling back to a 503 error when missing.
