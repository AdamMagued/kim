# Kim Agent Platform — Repository Map

> **Purpose.** A single, complete navigation map of the Kim codebase. A future
> debugging agent should be able to read this file end-to-end and locate any
> bug, feature, or design decision without re-exploring the tree from scratch.
>
> **Scope.** Every important file under `desktop/`, `orchestrator/`,
> `relay_server/`, `mcp_server/`, `tray/`, `extension/`, `kimctl/`, and
> `tests/`. Generated 2026-05-15 by a multi-agent sweep.
>
> **What this does NOT replace.** `BUGS_PENDING.md` (two live bugs from
> 2026-05-15), `CHANGELOG.md` (commit-level history), `CLAUDE.md` (the
> project's build instructions), `kim_PRD.md` (the original product spec).
> Read those when you need *why* or *when*; read this when you need *where*.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Subsystem File-by-File](#2-subsystem-file-by-file)
   - 2.1 [`desktop/`](#21-desktop--tauri--react)
   - 2.2 [`orchestrator/`](#22-orchestrator--python-agent-engine)
   - 2.3 [`mcp_server/`](#23-mcp_server--os-tool-server)
   - 2.4 [`relay_server/`](#24-relay_server--fastapi-bridge)
   - 2.5 [`tray/`](#25-tray--legacy-system-tray-ui)
   - 2.6 [`extension/`](#26-extension--chrome-mv3-extension)
   - 2.7 [`kimctl/`](#27-kimctl--cli-controller)
   - 2.8 [`CLI`](#28-cli--terminal-kim)
   - 2.9 [`tests/`](#29-tests)
3. [Method / Function Map](#3-method--function-map)
4. [Reference / Dependency Map](#4-reference--dependency-map)
5. [Feature Map](#5-feature-map)
6. [Debugging Guide](#6-debugging-guide)
7. [Risks and Unclear Areas](#7-risks-and-unclear-areas)
8. [Bug Inventory](#8-bug-inventory)

---

# 1. High-Level Architecture

## 1.1 What Kim is

Kim is a cross-platform desktop AI agent. The user types a natural-language
task and Kim takes screenshots, sees the screen, controls the mouse and
keyboard, executes shell commands, edits files, drives a browser, and runs
git/code tools — all driven by an LLM brain that the user picks (Claude,
GPT-4o, Gemini, DeepSeek, Ollama-local, or a "free" mode that scrapes
ChatGPT/Claude/Gemini DOMs).

A second use case (phone-to-PC relay) lets a phone POST tasks to a cloud
relay server; the PC polls and executes; results stream back.

## 1.2 Six independently deployable components

| Layer | Process | Stack | Purpose |
|---|---|---|---|
| **Desktop shell** | `Kim.app` | Tauri 2 (Rust) + React 19 + TypeScript | UI, settings, sessions, in-app browser bridge |
| **Orchestrator** | `python -m orchestrator.agent` | Python 3.12 async | Agent loop, LLM providers, memory, retries |
| **MCP server** | `python -m mcp_server.server` | Python 3.12 + MCP SDK | OS tools (files, shell, screen, mouse, browser…) over stdio |
| **Relay server** | `uvicorn relay_server.main:app` | FastAPI + SQLite (aiosqlite) | Phone↔PC task bus, deployed to Railway/Render |
| **Tray** *(legacy)* | `python -m tray.app` | pystray + Tkinter | Pre-Tauri tray UI; still works |
| **Extension** *(legacy)* | Chrome MV3 | JS, Manifest V3 | DOM scraping for Gemini/Claude/ChatGPT/DeepSeek; depends on a standalone bridge that **no longer exists in this repo** (see [§7](#7-risks-and-unclear-areas)) |

Plus two convenience tools:

| Tool | Purpose |
|---|---|
| `python -m kimctl …` | Talks to the desktop app's local HTTP bridge from the terminal |
| `kim` / Codex bridge | Code-mode execution. Browser-backed Code mode goes through `orchestrator.run_codex_bridge` and `mcp_server/tools/codex_bridge.py`; the historical `claw-code` tree is retained under `pythonExperimentTool/` for CLI/runtime work. |

## 1.3 The two flow paths

### A. Local "agent loop" path (the main one)

```
ChatView (React) ──invoke('send_task')──▶ lib.rs (Rust)
                                              │
                                              │ spawns subprocess:
                                              ▼
                                  python -m orchestrator.agent
                                              │
                                              │ spawns child via stdio:
                                              ▼
                                   python -m mcp_server.server
                                              │
KimAgent.run() loop:                          │
   1. take_screenshot via MCP ◄───────────────┤
   2. call LLM provider (Claude/OpenAI/…) ────┤
   3. parse tool_call from response           │
   4. call_tool via MCP ◄─────────────────────┤
   5. add result to memory; loop              │
   6. TASK_COMPLETE: → save summary; exit ────┤
                                              │
   stdout streamed back to lib.rs ────────────┘
   lib.rs emits Tauri events to ChatView
```

### B. Browser-provider path (no API key)

When `provider = browser:gemini` (etc.), the orchestrator does NOT call an LLM
API. Instead, two transports exist:

1. **In-app webview bridge** (default for the Tauri app). The
   `BrowserProvider` POSTs to `lib.rs`'s in-app HTTP bridge on
   `127.0.0.1:<random>`; `lib.rs` injects ~800 lines of `PERSISTENT_BRIDGE_JS`
   into a hidden Tauri WebviewWindow which pastes the prompt into the AI site,
   clicks Send, scrapes the response, and returns it.
2. **CDP** (legacy/tray). The provider connects to a real Chrome via
   Playwright on `localhost:9222` and drives the chat UI directly.

### C. Phone-to-PC relay path

```
phone ──POST /prompt──▶ relay_server (Railway)
                              │
                              │  (SQLite queue)
                              ▼
PC orchestrator.relay_worker ─ GET /prompt/next ─ enqueues into KimAgent
PC ─ POST /result ─▶ relay_server ─ WS push ─▶ phone
```

## 1.4 Build phases (from `CLAUDE.md`, all ✅ done)

| Phase | Deliverable | Status |
|---|---|---|
| 1 | MCP server with stdio transport | ✅ |
| 2 | Multi-LLM orchestrator (Claude/OpenAI/Gemini/DeepSeek + BrowserProvider) | ✅ |
| 3 | Browser extension v2 (Claude.ai / ChatGPT / Gemini / DeepSeek) | ✅ (but disconnected — see [§7](#7-risks-and-unclear-areas)) |
| 4 | FastAPI relay server | ✅ |
| 5 | Tray app | ✅ |
| 5.5 | Cross-platform OS layer | ✅ |
| 6 | Claude Code compatibility + git/code/search tools | ✅ |
| 7 | Production hardening + retries + structured logs | ✅ |
| 8 | Voice UI (Kokoro / Maya-1 / Hume / HTTP) | ✅ |

The Tauri desktop frontend was added *after* the PRD as the production UI;
the tray app is preserved as an alternative entry point.

---

# 2. Subsystem File-by-File

## 2.1 `desktop/` — Tauri + React

### Top-level

| File | Purpose |
|---|---|
| `desktop/package.json` | npm config: React 19, qrcode.react (for pairing QR), Tauri 2 plugins (`dialog`, `opener`). Tailwind 4. Vite 7. App version `0.9.6`. |
| `desktop/vite.config.ts` | Dev server on port 1420 (matches `tauri.conf.json devUrl`) |
| `desktop/src-tauri/Cargo.toml` | Rust deps: `tauri 2`, `tiny_http 0.12` (the in-app bridge), `keyring 2` (OAuth refresh-token storage), `reqwest`, `tokio`, `zip`, `base64`, `rand`, `sha2`, `url` |
| `desktop/src-tauri/tauri.conf.json` | Identifier `com.kim.desktop`. Window: 1280×840, transparent, `titleBarStyle: Overlay`, `macOSPrivateApi: true`. CSP allows GitHub API + ipc.localhost. |
| `desktop/src-tauri/capabilities/default.json` | Main window perms: `core:default`, `core:window:allow-start-dragging`, `opener:default`, `dialog:default` |
| `desktop/src-tauri/capabilities/browser-bridge.json` | Hidden `kim-browser-signin` window gets `core:event:allow-emit` so injected JS can `postMessage` results back via native IPC instead of polling `document.title`. Remote URL allowlist covers claude.ai, chatgpt.com, gemini.google.com, accounts.google.com, grok.com, chat.deepseek.com |

### `desktop/src/`

#### `App.tsx` (494 lines)
Root component. State:
- `settings: Settings` — persisted to `localStorage('kim-settings')`
- `account: KimAccount | null` — loaded via `invoke('load_account')`
- `activeSession`, `newChatMode`, `chatSerial` — chat view state
- `activeTab: 'chat' | 'code'` — switches between Kim Chat and Code mode
- `showSettings`, `settingsInitialPane` — settings modal control
- `appVersion`, `updateInfo`, `showUpdate` — silent GitHub release check on startup

Side effects on mount:
- Suppresses native WebView context menu and devtools shortcuts (F12, Cmd+Opt+I/J/C, Cmd+Shift+I/J/C).
- `invoke('get_app_version')` then `silentUpdateCheck()` against `api.github.com/repos/AdamMagued/kim/releases/latest`.
- Cmd+N / Cmd+, / Cmd+B / Escape global shortcuts.

Renders: `RevampSidebar` + `kim-main` (topbar + `ChatView`) + `RevampSettings` modal + `UpdateModal` + `OnboardingFlow` (if no account) + `ToastProvider`.

If `!account`, shows `OnboardingFlow` instead of the main UI.

#### `components/ChatView.tsx` (~2,750 lines)
The main chat surface. Owns:
- **Activity feed** — `ActivityItem[]` derived from `[STATUS]/[TOOL]/[DIFF]/[USAGE]/[CONTEXT]/[PLAN]/[STEP]/[DONE]` lines streamed from the agent's stdout via Tauri events.
- **Plan parser** — `parsePlanFromActivity()` reads structured `[PLAN]{json}` / `[STEP]{json}` / `[DONE]{json}` envelopes (preferred) or falls back to heuristic phrase detection. Drives the `CollapsiblePlan` widget.
- **Touched-files extractor** — `extractTouchedFiles()` walks tool calls to find `write_file/edit_file/create_file` and surfaces filenames in the UI.
- **Run-history persistence** — `invoke('save_run_history')` per task (used to show "Worked for 12s" badges on past Code runs).
- **Message rendering** — `collapseMessages()` merges adjacent tool-use + tool-result; `groupCodexMessages()` clusters Codex subtasks per user turn. `isIntermediateToolCall()` identifies pure-tool-call assistant messages (suppressed as bubbles; surfaced via WorkedForPill instead). `synthesizeExchangeActivity()` reconstructs `WorkedForTraceItem[]` from saved Ollama sessions where no `runHistory` entry exists.
- **Compact summary filter** — `compact_summary` role messages are stripped at `setMessages()` time and never shown as chat bubbles; the sentinel is injected into the system prompt by the orchestrator instead.
- **Composer** — input bar with attachments, screenshot drop, provider switcher, context ring.

Key Tauri commands invoked from this file (full list):

| Command | Purpose |
|---|---|
| `set_task_active_mode` | Tells Rust the task started/ended (controls window state and cancel widget) |
| `show_screenshot_flash` | Triggers the screen-capture aura animation |
| `load_run_history` / `save_run_history` | Per-session activity persistence |
| `load_session_messages` | Reads JSONL for an existing session |
| `send_task` | Spawns the orchestrator subprocess |
| `cancel_task` | Kills the subprocess (process group on Unix) |
| `set_browser_keep_visible` | Toggles the hidden webview's visibility behavior |
| `session_browser_url_commit` | Saves the current provider URL into `.browser.json` sidecar |
| `session_browser_meta_write` | Writes `browser_threads` / `browser_last_site` |
| `restore_browser_for_session` | On session load, restores the provider URL |
| `navigate_browser_window_if_open` | Mid-task safe navigation (with allowlist guard) |
| `open_browser_signin_window` | Opens the hidden webview for AI provider login |

Listens for Tauri events: `agent-output`, `kim-agent-done`, `kim-update-progress` (in `UpdateModal`), `kim-auth-changed` (in `useAuthStatus`).

The first 1,200 lines are helpers; the React component proper starts at the
`function ChatView(…)` definition. Most of the complexity lives in:
- The mutable `ActivityFeed` reducer
- The "live plan" extraction
- Provider switching with race-guarded `restoreSeq`
- Codex-run grouping (`groupCodexMessages`)
- The composer's attachment / drag-and-drop handling

Refactor note: pure chat helpers now live in `components/chat/utils.ts`
(message grouping, plan parsing, formatting, error normalization) and shared
interfaces live in `components/chat/types.ts`. Add new pure helpers there
instead of growing `ChatView.tsx`.

#### `components/MessageBubble.tsx` (545 lines)
Renders one chat message. Handles text, tool-use card, tool-result card, image
placeholders. Includes `Copy` and `Edit` action chips. Calls `friendlyError()`
from `ChatView` to massage raw errors.

#### `components/ToolCallCard.tsx` (343 lines)
Three components: `ToolUseCard` (the request), `ToolResultCard` (the response,
collapsible), `SignalCard` (special-case for high-signal tools like
`take_screenshot`). All render a small wrench/check chevron pattern.

#### `components/Sidebar.tsx` (867 lines, legacy)
Original sidebar implementation. **Superseded by `kim-ui/RevampSidebar.tsx`**
but kept around — `App.tsx` only imports `RevampSidebar`. Treat this file as
dead unless it's referenced elsewhere (verify with `rg "from './Sidebar'"`).

#### `components/SettingsPanel.tsx` (~1,490 lines, legacy)
Original settings UI. **Superseded by `kim-ui/RevampSettings.tsx`** but still
present. `App.tsx` imports `RevampSettings`, not this one. Likely dead, but
some helpers may be re-imported.
Provider/model constants now live in `components/settings/constants.ts`; inline
icon components now live in `components/settings/icons.tsx`.

#### `components/PairingModal.tsx` (195 lines, NEW)
Phone-relay pairing flow. Three phases: `loading → ready → claimed | expired |
error`.
- `invoke('relay_pair_init')` returns `{pair_code, expires_at, url}`.
- Renders a QR (`QRCodeSVG`) with `{url, code}` JSON payload plus the bare
  6-char code.
- Polls `invoke('relay_pair_status', {pairCode})` every 2s.
- Auto-closes 1.5s after `claimed`.

#### `components/OnboardingFlow.tsx` (272 lines)
First-run wizard. Welcome → choose provider → sign in → ready. Calls
`open_browser_signin_window` to open the in-app browser for provider auth.
Uses `useChromaShader` for the animated metallic background.

#### `components/ProviderPicker.tsx` (~560 lines)
Provider dropdown. Knows about every provider (cloud + browser:* + ollama).
Reads `OllamaStatus` from Rust (`ollama_get_status`), shows model list, lets
the user pick local vs cloud, install models with `ollama_pull_model`. Calls
`useAuthStatus` to show a sign-in chip per browser provider.

Cloud Ollama model selection has two modes (toggled by pill buttons): dropdown (pick from a curated list) or free-text (type any Ollama cloud model name and press Enter). Free-text mode is only available when the mode is set to `cloud`; switching to `local` collapses it automatically.

#### `components/kim-ui/WorkedForPill.tsx` (NEW)
Standalone disclosure pill shown before a final assistant answer in saved Kim/Ollama sessions. Displays a collapsed "Worked for Ns" badge that expands into a list of `WorkedForTraceItem` rows (tool name, target, duration). Used by `ChatView` when `runHistory` has no entry for an exchange (i.e., the session was saved before run-history tracking or is a non-Claw session).

#### `components/BrowserProviderPicker.tsx` (212 lines)
The grid of provider tiles (Claude / ChatGPT / Gemini / Grok / DeepSeek /
Custom) shown during onboarding. Each tile has a real brand SVG.

#### `components/AuthIndicator.tsx` (173 lines)
Chip rendered below the composer. Three visual states:
- Gray dot — "Not signed in — click to sign in"
- Green dot — "Signed in as <email>"
- Pulsing — "Checking…" during probe

Reads from `useAuthStatus(provider)`. Click → `invoke('provider_signin')`.

#### `components/UpdateModal.tsx` (167 lines)
Shown when `silentUpdateCheck` finds a newer GitHub release. Calls
`run_update` and streams progress via `kim-update-progress` event.

#### `components/Toast.tsx` (110 lines)
Toast notification system. Module-level `_setToasts` reference set by
`ToastProvider`. Top-level `toast(text, kind, duration)` function dispatches.

#### `components/KimLogo.tsx` (135 lines)
Brand logo SVG with stacked/inline layouts.

#### `components/Bloop.tsx` (178 lines)
Animated mascot character. CSS-only animations with 6 states: `idle`,
`thinking`, `processing`, `success`, `error`, `waiting`.

#### `components/CancelWidget.tsx` (52 lines)
The "Stop" pill shown in a small floating window during a task. Invokes
`cancel_task` on click. Window is `cancel-widget` (separate Tauri window
declared in capabilities).

#### `components/ThemeToggle.tsx` (58 lines)
Simple light/system/dark cycler used in older sidebar.

#### `components/kim-ui/` (revamped UI shell)

| File | Purpose |
|---|---|
| `index.ts` | Barrel exports |
| `RevampSidebar.tsx` | New sidebar — resizable width (drag handle, persisted in `localStorage`), session list (grouped Today/Yesterday/Week/Earlier), account chip, project list (Code tab), theme cycler. |
| `RevampSettings.tsx` | New settings modal — nav: appearance/ai/voice/paths/data/account/mcp/feedback/about. Tabs map to `PaneId`. |
| `ContextRing.tsx` | The circular context-budget indicator shown next to the composer; opens a popover to compact context. |
| `CollapsiblePlan.tsx` | Renders the agent's plan as a checklist with `done/active/pending/todo` statuses. |
| `ThinkingWithPlan.tsx` | Wraps `CollapsiblePlan` with a "Kim is thinking…" header during runs. |
| `Mascot.tsx` | Newer mascot component (replaces `Bloop` in some contexts). |
| `AppLaunchView.tsx` | Empty-state shown when no chat is active. |
| `NewSessionEmpty.tsx` | Empty-state shown when starting a new Code (Claw) session. |
| `ConnectorsPanel.tsx` | Right-side panel listing built-in connectors (currently `guc_cms`, `guc_mail` stubs). Has Connect/Manage buttons. |

#### `design-mocks/`
Static design references (not imported by the running app — these are HTML/TSX
mockups that informed `kim-ui/`). Safe to ignore for runtime debugging.

#### `hooks/`

| Hook | Purpose |
|---|---|
| `useTheme.ts` (49 lines) | `light/system/dark` with `prefers-color-scheme` listener; writes `kim-theme` to `localStorage`. |
| `useSessions.ts` (42 lines) | Loads `list_sessions` via Tauri, splits into `kimSessions` / `clawSessions`. |
| `useAccount.ts` (34 lines) | `load_account` / `save_account`. |
| `useAuthStatus.ts` (~100 lines) | Per-provider sign-in probe. Re-probes on `kim-auth-changed` event after a 400ms cookie-settle delay. |
| `useChromaShader.ts` (~120 lines) | WebGL "liquid chrome" shader for Onboarding and Settings backgrounds. |

#### `types/index.ts` (242 lines)
All shared TypeScript types: `SessionInfo`, `KimMessage`, `ContentBlock`,
`KimAccount`, `GoogleApiAccount`, `ClawProject/Branch/Session`, `Settings`,
`OllamaSettings`, `VoiceSettings`, `Theme`, `Provider`, `AccentTheme`,
`VoiceEngine`, `TypingAnimation`.

`DEFAULT_SETTINGS` shows the shipped defaults: `provider: 'ollama'`,
`accent: 'indigo'`, `voice.enabled: true`, `voice.engine: 'kokoro'`,
`ollama.cloud_model: 'gpt-oss:120b-cloud'`, `context_budget_tokens: 200_000`.

`VOICES_BY_ENGINE` maps each TTS engine to its catalog of voices.

#### `index.css`
Complete design system — CSS custom properties, dark/light mode via `.dark`
class on `<html>`, glassmorphism, screenshot flash overlay animation,
`kr-pulse-dot` keyframes, accent gradients per `AccentTheme`.

### `desktop/src-tauri/src/`

#### `main.rs` (4 lines)
Calls `desktop_lib::run()`.

#### `lib.rs` (~7,420 lines — still the central Tauri bridge)

The Rust shell. Owns four very distinct responsibilities, mashed into one
file:

Refactor note: account, Codex project, data import/export, feedback, Ollama,
relay, run-history, session-command, and voice-config commands now live in
their own `desktop/src-tauri/src/*.rs` modules and are registered from
`lib.rs`.

**(a) Tauri command handlers** — 54 commands registered in `generate_handler!`
(line 9711-9771):

```text
list_sessions, delete_sessions, load_session_messages,
summarize_session, save_run_history, load_run_history,
get_app_version, get_platform_info, run_update,
add_custom_provider_capability,
open_browser_signin_window, navigate_browser_window_if_open,
get_browser_current_url, session_browser_meta_read,
session_browser_meta_write, session_browser_url_commit,
restore_browser_for_session,
show_browser_window, hide_browser_window, set_browser_keep_visible,
provider_check_auth, provider_signin, provider_signout,
hide_main_window, show_main_window, set_task_active_mode,
send_task, cancel_task,
read_voice_config, write_voice_config,
read_relay_config, write_relay_url,
relay_pair_init, relay_pair_status,
google_oauth::google_oauth_status,
google_oauth::google_oauth_start,
google_oauth::google_oauth_disconnect,
google_oauth::google_oauth_test,
google_oauth::google_oauth_setup_free_tier_project,
load_account, save_account, clear_account, reset_onboarding,
delete_all_sessions,
ollama_get_status, ollama_test_model, ollama_signin, ollama_pull_model,
verify_github_pat,
export_data, import_data, backup_to_gist, restore_from_gist,
list_codex_projects, add_code_project, remove_code_project,
open_in_finder, send_feedback, show_screenshot_flash,
```

Source-line anchors for key commands:

| Command | Line | What it does |
|---|---|---|
| `list_sessions` | session_commands.rs | Walks Kim and Code session stores |
| `delete_sessions` | 5620 | Bulk delete by `[{session_id, session_type}]` list |
| `summarize_session` | 5685 | Generates `.summary.txt` via the orchestrator's compact prompt |
| `load_session_messages` | session_commands.rs | Parses JSONL into `KimMessage[]`; handles Code/Codex nested-content shapes |
| `get_app_version` | 5927 | Returns `env!("CARGO_PKG_VERSION")` |
| `open_browser_signin_window` | 6127 | Creates the hidden `kim-browser-signin` WebviewWindow with `PERSISTENT_BRIDGE_JS` injected |
| `show_browser_window` / `hide_browser_window` | 6192 / 6278 | Toggles visibility; offscreen-positioning when hidden |
| `set_browser_keep_visible` | 6296 | Persists user preference; honored during browser-provider runs |
| `navigate_browser_window_if_open` | 6309 | **Only navigates if URL passes provider allowlist** — refuses arbitrary URLs |
| `session_browser_meta_read` / `_write` | 6352 / 6367 | `.browser.json` sidecar I/O; **temp-file + rename** for atomicity (Windows fallback is remove+rename) |
| `session_browser_url_commit` | 6396 | Records current URL into sidecar, rejecting login/auth/home URLs (`browser_url_is_bad_for_commit`) |
| `restore_browser_for_session` | 6446 | Validates stored URL with `browser_url_allowed_for_restore`, navigates the webview, sets `KIM_BROWSER_RESTORE_STATUS` env for the next task |
| `provider_check_auth` | 6681 | Injects `build_auth_probe_js` (per-site script), reads result back, parses with `parse_auth_response` |
| `provider_signin` | 6814 | Opens the sign-in URL for the given site |
| `provider_signout` | 6836 | Clears cookies/storage for the provider host |
| `send_task` | 7121 | The big one. Picks Browser-bridge mode or direct mode; spawns Python subprocess; streams stdout via `agent-output` event; honors `KIM_WEBVIEW_BRIDGE_URL/TOKEN` and Google OAuth env injection |
| `cancel_task` | 7490 | SIGTERM then SIGKILL the process group; clears `BRIDGE_TASK_PID` |
| `read_voice_config` / `write_voice_config` | 7675 / 7757 | Surgical YAML edits to `config.yaml` (no full re-parse, preserves comments) |
| `read_relay_config` / `write_relay_url` | 7903 / 7918 | Same surgical YAML approach for relay section |
| `relay_pair_init` | 7955 | `POST /pair/init` to the configured relay using `RELAY_PC_API_KEY` from `.env` |
| `relay_pair_status` | 8004 | `GET /pair/status/<code>` polling |
| `load_account` / `save_account` | 8159 / 8170 | Read/write `~/.kim/account.json` (or platform-specific equivalent) |
| `delete_all_sessions` | account.rs | Deletes Kim session artifacts under the configured sessions dir |
| `ollama_get_status` | 8487 | Probes `/api/version`, `/api/tags`; merges known cloud models |
| `ollama_pull_model` | 8733 | `POST /api/pull` with streaming progress emitted via Tauri events |
| `verify_github_pat` | 8822 | `GET https://api.github.com/user` with Bearer token; returns `GitHubUser` |
| `export_data` / `import_data` | 8846 / 9034 | Sessions zip/json/markdown export and zip/json import |
| `backup_to_gist` / `restore_from_gist` | 9175 / 9244 | GitHub Gist round-trip using `account.github_token` |
| `list_codex_projects` | codex_projects.rs | Scans configured Code project paths and recent Codex sessions |
| `add_code_project` / `remove_code_project` | 9381 / 9406 | Mutates `account.code_projects` |
| `open_in_finder` | 9425 | macOS `open`, Windows `explorer`, Linux `xdg-open` |
| `send_feedback` | 9611 | POSTs to a feedback endpoint (`FeedbackPayload`) |

**(b) In-app HTTP bridge** — `start_webview_bridge_server` (line 5520) runs a
`tiny_http` server on `127.0.0.1:<random>` with an `X-Kim-Token` auth header.
Routes (line 3866 `handle_webview_bridge_request`):

| Method+Path | Purpose | Auth |
|---|---|---|
| `GET /v1/health` | Liveness | none |
| `POST /v1/hide` / `/v1/show` | Toggle webview visibility | token |
| `POST /v1/open` | Navigate to a URL inside the webview | token |
| `POST /v1/callback` | JS bridge result drop-off (`__kimBridge.deliver`) | token |
| `GET /v1/ping*` | Debug | token |
| `POST /v1/complete` | **Legacy monolithic** prompt-send-and-wait (kept for old orchestrators) | token |
| `POST /v1/send` | New split API; returns `{req_id}` instantly | token |
| `GET /v1/result/<req_id>` | Long-poll for the response | token |
| `GET /v1/status` | Returns running-task flag, session ID, browser visibility | token |
| `GET /v1/browser/current-url` | Reads the live webview URL | token |
| `GET /v1/browser/meta?session_id=…` | Reads `.browser.json` sidecar | token |
| `POST /v1/browser/meta` | Writes sidecar | token |
| `POST /v1/browser/commit-url` | Validate + persist current URL into sidecar | token |
| `POST /v1/browser/restore` | Restore a stored URL for a session | token |
| `POST /v1/task` | External task submission (used by `kimctl send`) | token |
| `POST /v1/cancel` | External cancel | token |
| `POST /v1/browser/show` / `/hide` / `/click` / `/new-chat` | Browser ops | token |
| `POST /v1/provider` | Switch active browser provider | token |

**(c) Persistent JS bridge script** — `build_bridge_complete_script` (line
2543) builds the ~800-line `PERSISTENT_BRIDGE_JS` injected into the
WebviewWindow at `initialization_script` time. The script:

- Detects which AI site is loaded (host match).
- Finds the input editor / send button / stop button / response containers
  per site (selector map embedded in the JS, in sync with
  `orchestrator/providers/browser/site_configs.py:SITE_CONFIGS`).
- Listens for `__kimBridge.send(prompt, reqId, site, attachments)`:
  - Clears editor
  - Pastes prompt (with attachments via clipboard for Gemini, file upload
    elsewhere)
  - Clicks Send (NOT triple-Enter — that was a known bug, fixed)
  - Polls for response completion using a "stop button hidden + text stable
    for ≥6s" heuristic plus a dynamic completion hash marker injected into
    the prompt
  - Scrapes the final response text
  - POSTs to `/v1/callback` (with `__kimCallbackToken` placeholder
    substituted)
- A separate URL-change observer clears stale `_lastHash` markers when the
  user navigates between conversations.

**(d) Helper functions** — see [§3](#3-method--function-map) for a full map.
Notable ones:
- `default_project_root()` (line 231) — compile-time embed → `~/.kim_root` file → `KIM_ROOT` env → exe ancestor → `~/.kim`
- `find_python_interpreter()` (line 307) — `<project>/venv/bin/python` → `python3` → `python`
- `validate_session_id()` (line 341) — only allows `[A-Za-z0-9._-]`
- `is_bridge_task_running()` (line 1051) — checks `BRIDGE_TASK_PID` AND verifies process still alive (auto-clears stale PIDs)
- `prepare_gemini_webview()` (line 1110) — navigates browser to the correct `authuser=<n>` URL
- `gemini_url_has_conversation_path()` (line 1090) — distinguishes `/app/<conv>` from `/app` start page (used by URL commit validation)
- `write_first_png_to_clipboard()` (line 1225) — macOS uses NSPasteboard via swift-bridge (or osascript fallback); Windows uses clipboard crate; Linux is a no-op
- `clear_provider_webview_chat()` (line 1024) — `localStorage.clear()` + reload, used when the orchestrator signals it needs a fresh chat
- `handle_bridge_ipc_event()` (line 2458) — receives `BridgeIpcEvent` via Tauri's window-message channel from the JS bridge

#### `google_oauth.rs` (~475 lines)
Google OAuth for Gemini API. PKCE flow (no client secret), loopback callback.

| Function | Purpose |
|---|---|
| `google_oauth_start` | Tauri command; runs the PKCE flow, listens on a random TCP port, exchanges code → tokens, stores refresh token in OS keyring (`KEYRING_SERVICE = "kim.google.oauth"`) |
| `google_oauth_status` | Reads keyring; refreshes access token; returns `GoogleOAuthStatus` |
| `google_oauth_disconnect` | Deletes keyring entry |
| `google_oauth_test` | Calls `GEMINI_MODELS_URL` to verify the token works |
| `google_oauth_setup_free_tier_project` | NEW: generates `kim-gemini-<random>`, calls Google Cloud Resource Manager + Service Usage APIs to create a project and enable `generativelanguage.googleapis.com`. Stores `project_id` alongside the refresh token. |
| `create_gcp_project` | Internal: `POST https://cloudresourcemanager.googleapis.com/v1/projects` |
| `enable_gemini_api` | Internal: `POST .../services/generativelanguage.googleapis.com:enable` |
| `AgentGoogleOAuthEnv::as_env_pairs()` | Builds env-var pairs for the Python subprocess. Auto-detects mode: `oauth_user_project` if `project_id` is set, else `oauth`. Always passes `KIM_GEMINI_AUTH_MODE`, `KIM_GOOGLE_ACCESS_TOKEN`, `KIM_GOOGLE_ACCESS_TOKEN_EXPIRES_AT`; conditionally passes `KIM_GOOGLE_USER_PROJECT_ID`. |

Scopes requested:
- `openid email profile` (base)
- `https://www.googleapis.com/auth/generative-language.retriever` (Gemini API)
- `https://www.googleapis.com/auth/cloud-platform` (for project creation only)

---

## 2.2 `orchestrator/` — Python agent engine

### Files

| File | Lines | Role |
|---|---|---|
| `__init__.py` | 0 | Package marker |
| `agent.py` | ~1,540 | Main loop and `KimAgent`; delegates CLI, MCP client, UI bridge, and state helpers |
| `agent_states.py` | 40 | Explicit `AgentTermination` enum + `make_run_result()` |
| `cli.py` | 72 | CLI parser and `_cli_main()` extracted from `agent.py` |
| `mcp_client.py` | 137 | `MultiMCPClient` and `mcp_session_context()` |
| `ui_bridge.py` | 135 | `UIBridge` and `UIBridgeLogHandler` |
| `tool_utils.py` | 76 | Tool-name normalization and text JSON tool-call extraction |
| `memory.py` | ~190 | Sliding-window conversation memory with screenshot pruning; compact-summary sentinel support |
| `compaction.py` | ~220 | NEW: Claw-style local compaction (no LLM call); `compact_messages()`, `should_compact()`, `_fix_tool_boundary()`, `_summarize_messages()`, `_merge_summaries()` |
| `task_queue.py` | 138 | Local+relay async task queue (dormant by default) |
| `relay_worker.py` | 123 | NEW: wires `TaskQueue` to `KimAgent` for end-to-end execution |
| `context_meter.py` | 261 | Token-budget tracker; emits `[CONTEXT]` log lines |
| `context_loader.py` | 151 | Discovers `KIM.md` / `KIM.local.md` / `.kim/KIM.md` walking upward |
| `session_store.py` | 332 | JSONL session persistence + summaries + compact artifacts |
| `run_codex_bridge.py` | 156 | CLI entry that spawns Codex with a local proxy relaying through `BrowserProvider` |
| `run_codex_relay.py` | 95 | Relay helper for Codex browser bridge traffic |
| `providers/__init__.py` | 0 | — |
| `providers/base.py` | 93 | `BaseProvider` ABC + `create_provider` factory |
| `providers/claude.py` | 138 | Anthropic Claude (native tool-use, `batch` for multi-tool) |
| `providers/openai_provider.py` | 174 | OpenAI + OpenAI-compatible (Cerebras, Groq, etc.) |
| `providers/deepseek.py` | 36 | Thin `OpenAIProvider` subclass with `_BASE_URL = api.deepseek.com/v1`. **Does not call `super().__init__`** |
| `providers/gemini.py` | 518 | Three auth modes: `api_key`, `oauth`, `oauth_user_project` |
| `providers/ollama.py` | 538 | Local/cloud Ollama with native tool-calling, streaming, ctx-limit detection |
| `providers/browser_provider.py` | 25 | Backward-compatible shim that lazily re-exports `BrowserProvider` and `SITE_CONFIGS` |
| `providers/browser/provider.py` | 1,090 | BrowserProvider implementation: CDP/in-app bridge, injection, wait/scrape |
| `providers/browser/bridge_client.py` | 289 | In-app webview bridge HTTP client |
| `providers/browser/prompt_builder.py` | 366 | Browser prompt formatting, history recap, data URI extraction |
| `providers/browser/response_parser.py` | 148 | DOM text → canonical provider response parsing |
| `providers/browser/site_configs.py` | 159 | Per-site selector maps and browser-provider constants |

### Key class detail

#### `KimAgent` (`agent.py:392-`)

`__init__(config, provider, mcp_client_or_session, ...)`:
- Reads `max_iterations`, `screenshot_scale`, `memory_max_messages`,
  `memory_keep_screenshots`, `max_retries`, `retry_base_delay`,
  `retry_max_delay`, `context_budget_tokens`.
- Creates `ConversationMemory`, `SessionStore`, `ContextMeter`.
- Restores context-meter state from `<session>.context.json` if it exists;
  carries forward `needs_fresh_chat` into `_clear_chat_on_next_call`.

`run(task)`:
1. Check task against `_COMPACT_CONTROL_TASKS` → if match, delegate to `_compact_and_reset_context()`. Browser providers use LLM-based compaction (sets `_clear_chat_on_next_call`); API providers (Ollama, Claude, etc.) use `_compact_api_provider()` — local, no LLM call, no browser flags.
2. `provider.reset_session()` if provider supports it (BrowserProvider does).
3. Load session memory (resume or fresh).
4. `_refresh_tools()` — `list_tools` via MCP, canonicalize.
5. `_build_system_prompt(task)` — assembles the system prompt with per-task nonce, tool list, OS guidance, plan protocol, KIM.md, recent summaries. Uses `_build_lean_system_prompt` for `provider.lean_system_prompt = True` (Ollama). If `memory.compact_summary` is set, prepends it to the system prompt so the compaction context is visible to the LLM without embedding a system-role message in the messages array (which Anthropic's API forbids).
6. Append `"Task: <task>"` user message.
7. **Loop up to `max_iterations`:**
   - Cancellation check via `UIBridge`.
   - `estimate_request_tokens()` for fallback usage.
   - `_call_with_retry(messages, tools, system, clear_chat=…)` — exponential backoff + jitter on retryable errors (regex match on 429/500/502/503/529/overloaded/network/timeout).
   - `_track_context_usage()` updates `ContextMeter`; emits `[STATS]`, `[USAGE]`, `[CONTEXT]` log lines.
   - **If `tool_call`:**
     - Normalize tool name (`_TOOL_NAME_ALIASES`, lowercase, strip non-word).
     - **Batch dispatch** — if `tool == "batch"`, validate each call against `_BATCH_SAFE` whitelist; execute sequentially; abort on first error.
     - Preview mode — if enabled and UI bridge attached, `await bridge.confirm_action(name, args)` with 60s timeout.
     - Append assistant JSON to memory and session.
     - `_execute_tool(name, args)` — emits `[UI] SCREENSHOT_FLASH` for screenshot tools, hides UI for 0.45s, calls MCP.
     - For `take_screenshot`: strip `data:image/png;base64,` prefix, run stuck detection (`_is_stuck` MD5 over last 3), add as multimodal user content with `has_screenshot=True`.
     - For `take_annotated_screenshot`: parse JSON result, extract image + grid, build text context.
     - For `web_open` returning `AUTH_FAILED:`: short-circuit `NEED_HELP`.
   - **If `text`:**
     - Append, emit any `[PLAN]/[STEP]/[DONE]` markers as `[STATUS]` lines.
     - `TASK_COMPLETE:` → `_generate_and_save_summary()` → return success.
     - `NEED_HELP:` → return failure.
     - Else increment `consecutive_continues`; at 3 consecutive text-only turns return `NEED_HELP`.
8. Exit loop → return failure with `"Reached maximum iterations"`.

#### `UIBridge` (`agent.py:135-`)
Thread-safe channel. `log_queue`, `_confirm_queue`, `_visibility_queue`,
`cancelled` event. `confirm_action` blocks for up to 60s waiting on the UI
thread; `cancel()` drains the queue and rejects pending confirms.

#### `MultiMCPClient` (`agent.py:278-`)
Multiplexes multiple MCP sessions. **Caveat**: on duplicate tool names, the
last server's version silently wins.

#### `mcp_session_context` / `mcp_agent_context` (`agent.py:314-`, `1585-`)
Async context managers. Spawn one stdio MCP server per config entry, plus the
internal `mcp_server.server`. Merge env (`PYTHONPATH`, `PROJECT_ROOT`,
`VIRTUAL_ENV`, `KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`) into
child env. Timeout init at 30s.

### Provider contract

```
BaseProvider.complete(messages, tools, system) -> dict
    "type": "tool_call", "tool": str, "args": dict, "usage"?: dict
  | "type": "text",      "content": str,             "usage"?: dict
```

Compliance matrix:

| Provider | Native tool-calling | Multi-tool | Usage | Compliance |
|---|---|---|---|---|
| Claude (`AnthropicProvider`) | API-native `tool_use` blocks | Wraps as `batch` correctly | `{input, output}` | ✅ Fully compliant |
| OpenAI (`OpenAIProvider`) | API-native `function` tools | Returns `type=text` with error string (NOT a tool_call) — agent then sees this as a stuck loop | `{input, output}` | ⚠ Multi-tool surfaces wrong type |
| DeepSeek | Inherits from OpenAI | Same | Same | ⚠ Same multi-tool issue; also `__init__` skips `super()` |
| Gemini (`GeminiProvider`) | `FunctionDeclaration` SDK or REST | Returns only the first `functionCall` from parts; others dropped | `{input, output}` | ⚠ Silent multi-tool drop |
| Ollama | Native tool_calls | Same as OpenAI multi-tool issue | Rich dict | ⚠ Multi-tool issue + class flags `native_tool_calling=True`, `lean_system_prompt=True` |
| Browser | Prompt-injected JSON | Cannot batch (one JSON per response) | `{input, output, estimated:True}` | Diverges from base type hints (extra `clear_chat` kwarg detected via `inspect.signature` introspection) |

---

## 2.3 `mcp_server/` — OS tool server

### Files

| File | Lines | Role |
|---|---|---|
| `__init__.py` | 0 | — |
| `server.py` | 134 | `Server("kim")` over stdio; imports tool definitions/dispatch from `tool_registry.py` and merges site connectors |
| `tool_registry.py` | 922 | MCP tool schemas plus dispatch map |
| `config.py` | 146 | Loads `config.yaml` + `.env`; `validate_path()`; constants for all options |
| `logger.py` | 174 | `JSONLineHandler` + `setup_structured_logging()` writing `logs/kim_<date>.jsonl`. **Not auto-wired** — see [§7](#7-risks-and-unclear-areas) |
| `os_utils.py` | 265 | OS detection; `translate_command()`; app mapping dicts (`_APP_MAP_MAC`, `_APP_MAP_LINUX`, `_BUILTIN_MAP_UNIX`) |
| `tools/__init__.py` | 0 | — |
| `tools/files.py` | 95 | `read_file`, `write_file`, `list_dir`, `delete_file` |
| `tools/shell.py` | 245 | `run_command`, `run_powershell`; metachar filter + deny-set + recursive shell-wrapper check |
| `tools/screen.py` | 158 | `take_screenshot`, `get_screen_info`, `take_annotated_screenshot` |
| `tools/screen_annotator.py` | 210 | Pillow-based grid overlay (`A1..J10`) |
| `tools/mouse.py` | 82 | pyautogui click/double/right/drag/scroll |
| `tools/keyboard.py` | 49 | `type_text` (clipboard paste, **ignores `interval` arg**), `hotkey`, `key_press` |
| `tools/windows.py` | 393 | Cross-platform window mgmt + legacy `handle_open_url` (currently unused — see bugs) |
| `tools/git.py` | 186 | git_status, git_diff, git_add, git_commit, git_log, git_checkout |
| `tools/code.py` | 269 | run_python, run_node, lint_file; inline-code blocklist |
| `tools/search.py` | 250 | search_in_files (rg→grep→findstr), find_files |
| `tools/web.py` | ~1,060 | Playwright-driven web_* tools; module-level browser singleton |
| `tools/web_element_scoring.py` | 264 | Pure element-scoring helpers for `web_resolve` |
| `tools/web_observe_js.py` | 166 | JavaScript blob used by `web_observe` |
| `tools/ui_observe.py` | 338 | macOS AX accessibility tree via AppleScript; `observe_ui`, `click_ui` |
| `tools/codex_bridge.py` | ~1,160 | `run_codex_subtask()` library function and local OpenAI-compatible proxy for Codex browser mode |
| `tools/test_extract.py` | 29 | **Dev scratch file** that runs at import time |
| `sites/__init__.py` | — | Exports `SiteConnector`, `register_site`, `enabled_connectors`, `load_builtin_connectors` |
| `sites/base.py` | — | Dataclass + global registry |
| `sites/guc_cms.py` | — | Stub with placeholder `guc_cms_ping` |
| `sites/guc_mail.py` | — | Stub with placeholder `guc_mail_ping` |

### Tool catalog

Files (4):
- `read_file(path)`, `write_file(path, content)` *(detects `data:…;base64,` prefix → binary)*, `list_dir(path?, recursive?)` *(prunes node_modules/.git/venv/__pycache__/.next/.nuxt; truncates at 500)*, `delete_file(path)` *(refuses dirs)*.

Shell (2):
- `run_command(cmd, cwd?, timeout?, allow_chaining?)` — validates `_DENY_PATTERNS` regex, then metachar (`;|&` and backtick and `$(`) unless `allow_chaining`, then `_DENY_COMMANDS` first-token set, then recursive shell-wrapper check, then `translate_command()` cross-platform translation.
- `run_powershell(script, timeout?)` — uses `pwsh` on Unix if installed; passes `allow_chaining=True` (bypasses metachar filter).

Screen (3): `take_screenshot(scale?, monitor?)`, `get_screen_info()`, `take_annotated_screenshot(scale?, monitor?, grid_cols?, grid_rows?)`.

Mouse (5): `click`, `double_click`, `right_click`, `drag`, `scroll`.

Keyboard (3): `type_text(text, interval?)` *(interval ignored)*, `hotkey(keys)`, `key_press(key, presses?, interval?)`.

Window mgmt (4): `get_windows`, `focus_window`, `resize_window`, `open_url` *(dispatched to `handle_web_open` not `handle_open_url` — see bugs)*.

UI accessibility, macOS only (2): `observe_ui(limit?, depth?)`, `click_ui(element_id, button?, clicks?)`.

Web/Playwright (10): `web_open` *(with optional Basic auth, detects AUTH_REQUIRED/AUTH_FAILED/`chrome-error://`)*, `web_observe`, `web_click`, `web_fill`, `web_press`, `web_text`, `web_screenshot`, `web_wait_for`, `web_back`, `web_close` *(no-op so the dedicated Kim browser persists)*.

Git (6): `git_status`, `git_diff`, `git_add`, `git_commit`, `git_log`, `git_checkout`. **All use `create_subprocess_exec` (no shell injection)** but `git_diff/git_add/git_checkout` do NOT pass user paths through `validate_path` — see bugs.

Code (3): `run_python(file?, code?, cwd?, timeout?)`, `run_node(file?, code?, cwd?, timeout?)`, `lint_file(path, fix?)` *(ruff preferred, flake8 fallback)*.

Search (2): `search_in_files(pattern, path?, include?, case_sensitive?, regex?, context_lines?)` — `rg → grep → findstr`. `find_files(pattern, path?, type?)` — pathlib glob with hidden/node_modules pruning, 200-result cap.

### Connection flow for `tools/web.py`'s browser singleton

First call to any `web_*` tool tries:
1. Connect to `127.0.0.1:9222` (real Chrome via CDP).
2. Launch real Chrome with `--remote-debugging-port=9222` and retry.
3. Connect to `KIM_DEDICATED_BROWSER_CDP_PORT` (default 9333).
4. Launch a dedicated Kim browser (detached process with its own profile).
5. `playwright.launch_persistent_context(USER_DATA_DIR)` fallback.

`USER_DATA_DIR` is resolved at **import time** to `sessions/kim_browser/` and
the directory is created as a side effect of the import — this can crash MCP
server startup if PROJECT_ROOT is misconfigured.

---

## 2.4 `relay_server/` — FastAPI bridge

### Files

| File | Role |
|---|---|
| `__init__.py` | Empty package marker |
| `auth.py` (109 lines) | `require_phone_key`, `require_pc_key`, `require_any_key` — `secrets.compare_digest` |
| `queue.py` (423 lines) | `TaskDB` (aiosqlite). Three tables + indexes. Stale expiry. Device pairing. |
| `models.py` (115 lines) | Pydantic v2 schemas |
| `main.py` (378 lines) | FastAPI app, lifespan, CORS, WebSocket manager, PC heartbeat |
| Top-level `Dockerfile` | python:3.12-slim, non-root, hardcoded `--port 3001` (BUG — does not read `$PORT`) |
| Top-level `railway.toml` | Builder + `$PORT`-aware startCommand + healthcheck `GET /status` (BUG — `/status` requires auth) |
| Top-level `requirements-relay.txt` | fastapi, uvicorn[standard], pydantic, aiosqlite, python-dotenv |

### HTTP API

| Method | Path | Auth | Body / Params | Returns | Caller |
|---|---|---|---|---|---|
| POST | `/prompt` | phone key OR device token | `PromptRequest` | `PromptResponse` 202 | Phone |
| GET | `/prompt/next` | PC key | — | task or 204 | PC orchestrator (polled) |
| POST | `/result` | PC key | `ResultRequest` | `ResultResponse` + WS broadcast | PC orchestrator |
| GET | `/result/{task_id}` | phone key OR device token | path | `TaskStatusResponse` 200 / 404 | Phone (polling) |
| GET | `/status` | any key (BUG) | — | `StatusResponse` | Both / healthcheck |
| POST | `/pair/init` | PC key | — | `PairInitResponse` | PC desktop |
| POST | `/pair/complete` | **none** | `PairCompleteRequest` | `PairCompleteResponse` 200 / 400 | Phone (no prior credential) |
| GET | `/pair/status/{pair_code}` | PC key | path | `PairStatusResponse` | PC desktop polling |
| WS | `/ws` | phone key OR device token (inline check, not dep) | — | broadcasts result events; ping/pong | Phone |

### SQLite schema

```sql
CREATE TABLE tasks (
    id           TEXT PRIMARY KEY,        -- uuid4().hex
    task         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    picked_up_at TIMESTAMP,
    completed_at TIMESTAMP,
    summary      TEXT,
    screenshot   TEXT,
    success      BOOLEAN
);
CREATE INDEX idx_tasks_status_priority ON tasks (status, priority DESC, created_at ASC);

CREATE TABLE devices (
    id           TEXT PRIMARY KEY,        -- uuid4().hex
    device_token TEXT UNIQUE NOT NULL,    -- secrets.token_urlsafe(32)
    device_name  TEXT NOT NULL,
    paired_at    TIMESTAMP NOT NULL,
    last_seen    TIMESTAMP
);
CREATE INDEX idx_devices_token ON devices(device_token);

CREATE TABLE pending_pairings (
    pair_code  TEXT PRIMARY KEY,           -- 6-char UPPERCASE
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,         -- created_at + 300s default
    claimed_at TIMESTAMP,
    device_id  TEXT
);
```

### Pairing flow

1. PC: `POST /pair/init` → `{pair_code, expires_at}` (5 min TTL).
2. PC renders QR with `{url, code}` JSON payload.
3. Phone scans, calls `POST /pair/complete` **(no auth)** with `{pair_code, device_name}`.
4. Relay validates code unclaimed + unexpired, INSERTs into `devices`, marks pairing claimed.
5. Phone receives `device_token` (43 URL-safe base64 chars), stores it permanently.
6. PC polls `GET /pair/status/{code}` until `claimed=true`.

### Env vars

| Var | Default | Purpose |
|---|---|---|
| `RELAY_PHONE_API_KEY` | empty | Phone master key (optional — device tokens still work) |
| `RELAY_PC_API_KEY` | empty | PC key (REQUIRED — empty = all PC endpoints reject) |
| `RELAY_DB_PATH` | `<pkg>/relay.db` | SQLite file path. Docker overrides to `/app/data/relay.db` |
| `PC_TIMEOUT_S` | 15 | Seconds without PC poll before `pc_connected=False` |
| `STALE_PENDING_S` | 300 | Auto-fail pending tasks after this |
| `STALE_RUNNING_S` | 600 | Auto-fail running tasks after this |
| `PAIR_CODE_TTL_S` | 300 | QR validity |
| `ALLOWED_ORIGINS` | empty | CORS list; empty = block all cross-origin (BUG — silent) |

---

## 2.5 `tray/` — Legacy system tray UI

### Files

| File | Role |
|---|---|
| `__main__.py` (3 lines) | Imports `main` from `tray.app`; entry point for `python -m tray` |
| `app.py` (585 lines) | `KimApp`, `_AsyncRunner`, hotkey listener, tray icon |
| `ui.py` (730 lines) | `ControlPanel(tk.Toplevel)` — dark-mode chat UI; `_Toggle` widget; voice engine hot-swap |
| `settings.py` (329 lines) | `SettingsWindow` — Config tab + API Keys tab; atomic YAML/`.env` write |
| `voice.py` (1,069 lines) | `VoiceEngine` + 4 providers: `KokoroVoiceProvider`, `MayaVoiceProvider`, `HttpVoiceProvider`, `HumeVoiceProvider` |

### Threading model
1. Tk main thread — event loop + `_poll` (every 50ms).
2. Daemon thread `kim-tray` — pystray icon (`run_detached`).
3. Daemon thread `kim-asyncio` — asyncio event loop (agent + relay worker).
4. `ThreadPoolExecutor` — TTS generation/playback (1 worker).
5. Optional `engine-swap` thread for hot-swapping voice engines.

### Hotkey
Ctrl+Alt+J via `pynput.keyboard.Listener` (`pynput.keyboard.GlobalHotKeys`
crashes on macOS due to an `injected` arg, so a manual key-set listener is
used).

### Voice provider chain
`_build_fallback_chain(primary, voice_cfg)` always adds Kokoro + HTTP as
fallbacks unless they're the primary. **Hume is never added as a fallback.**

---

## 2.6 `extension/` — Chrome MV3 Extension

### Files

| File | Role |
|---|---|
| `manifest.json` | MV3. Permissions: `storage`, `activeTab`, `scripting`, `tabs`. Host perms: localhost, claude.ai, chatgpt.com, gemini.google.com, chat.deepseek.com. |
| `background.js` (~240 lines) | Service worker. `parseBlocks` (preserved Bridge V3 format). `postSync` to `localhost:3000/sync`. Relay status poller. |
| `content_claude.js` (~138 lines) | Claude.ai DOM driver |
| `content_chatgpt.js` (~138 lines) | ChatGPT DOM driver |
| `content_gemini.js` (~128 lines) | Gemini DOM driver |
| `content_deepseek.js` (~143 lines) | DeepSeek DOM driver — uses `last div[role='button']` (fragile) |
| `overlay.js` (~119 lines) | Toast + drag-drop file uploader. **Sends full `data:…;base64,…` data URL** as content — bridge must strip the prefix |
| `popup.html` + `popup.js` | Popup UI: site badge, loop toggle, retries spinbox, settings panel |

### Selector map

| Site | Response | Input | Send | Stop |
|---|---|---|---|---|
| claude.ai | `[data-testid^='conversation-turn']` last | `div[contenteditable='true'].ProseMirror` | `button[aria-label*='Send']` | `button[aria-label*='Stop']` |
| chatgpt.com | `div.markdown` last | `div#prompt-textarea` | `button[data-testid='send-button']` | `button[data-testid='stop-button']` |
| gemini.google.com | `model-response` last | `rich-textarea > div[contenteditable]` | `button[aria-label*='Send message'], button[aria-label*='Send']` | `button[aria-label*='Stop']` |
| chat.deepseek.com | `div.ds-markdown` last | `textarea#chat-input, textarea` | last `div[role='button']` | `div[role='button'][class*='stop'], div[aria-label*='Stop']` |

⚠ **The extension is currently disconnected.** It POSTs to `http://localhost:3000/sync` and `/write_file` but no such bridge exists in this repo. The Tauri desktop app's bridge is at `127.0.0.1:<random>:18991ish` with a totally different API (`/v1/task`, …). The extension and desktop are architecturally decoupled.

---

## 2.7 `kimctl/` — CLI controller

### Files

| File | Role |
|---|---|
| `__init__.py` | `# kimctl — terminal control surface for Kim` |
| `__main__.py` (~580 lines) | Full CLI |

### Commands

| Command | Calls | Purpose |
|---|---|---|
| `status` | `GET /v1/status` | Running task flag, session ID, browser visibility |
| `chats` | reads JSONL directly | Lists sessions with preview |
| `show <id>` | reads JSONL directly | Renders session conversation (ANSI colors) |
| `send "<task>"` | `POST /v1/task` | Submit task; polls session JSONL for `TASK_COMPLETE:` / `NEED_HELP:` |
| `cancel` | `POST /v1/cancel` | Stop running task |
| `browser show/hide/click/new-chat` | `POST /v1/browser/...` | Browser ops |
| `browser current-url` | `GET /v1/browser/current-url` | Read live URL |
| `browser meta <id>` | `GET /v1/browser/meta?session_id=…` | Read sidecar |
| `browser commit-url <id>` | `POST /v1/browser/commit-url` | Persist current URL |
| `browser restore <id>` | `POST /v1/browser/restore` | Restore stored URL |

### Bridge resolution order
1. `KIM_WEBVIEW_BRIDGE_URL` / `KIM_WEBVIEW_BRIDGE_TOKEN`
2. `KIM_API_KEY` or `mcp_server.config.get_config()["api_key"]`
3. `kim_sessions/.bridge_url`, `kim_sessions/.bridge_token`
4. `config.yaml: browser_provider.bridge_url / bridge_token`
5. Default `http://127.0.0.1:18991`

---

## 2.8 `CLI` — terminal Kim

Kim has two terminal surfaces now:

| Tool | Location | Purpose |
|---|---|---|
| `python -m kimctl` | `kimctl/` | Thin remote-control CLI for the desktop app's local HTTP bridge. |
| `kim` | `pythonExperimentTool/claw-code/rust/crates/kim-cli/` | Lightweight standalone terminal UI that feels like Kim in a terminal. |

### `kim-cli` crate

| File | Purpose |
|---|---|
| `Cargo.toml` | Binary crate named `kim`; deps are `ratatui`, `crossterm` with bracketed paste, `tokio`, `reqwest`, `serde`, `dirs`, `rpassword`, plus shared `runtime` compaction. |
| `src/main.rs` | App loop, terminal lifecycle, keyboard handling, menu-first Chat/Code mode switching (`Tab`), `Esc` back-to-chat-list behavior, bracketed-paste with file-path-first detection (chat view also accepts plain text paste), model picker state, Ctrl-C clear/exit flow, session resume IDs, single-thread Tokio runtime, `--help` / `--version` / `--resume`. Panic hook (`install_panic_hook`) restores raw mode / alternate screen / bracketed paste before printing the panic message so the terminal is never left broken. Key events filtered to `Press | Repeat` (eliminates Windows key-release duplicate characters). |
| `src/provider.rs` | Provider adapters for Ollama/OpenAI-compatible APIs, Claude Messages API, optional Kim desktop bridge, and image attachment payload conversion for pasted PNG/JPEG/WebP/GIF paths. |
| `src/ui.rs` | Ratatui layout: header, full-screen chat/session picker at launch, Chat/Code mode tabs, full-screen chat view with persistent Esc-to-list hint, slash-command picker, model picker, input bar, status/footer. |
| `src/theme.rs` | Merged TUI palettes from the ZIP mocks: default `dark-neovim` and soft muted novel `quiet-light`. |
| `src/config.rs` | Persists provider/model/theme/API keys at `~/.kim/cli-config.json`. |
| `src/commands.rs` | Kim-ready slash command subset and focused unit tests. |
| `src/sessions.rs` | Discovers repo/home Kim, Claw, desktop, and project-only session roots; humanizes JSONL titles/previews and filters raw tool/usage blobs before loading sessions into the TUI. |
| `../runtime/src/compact.rs` | Shared Claw-style local compaction used by `/compact`; no LLM/webview call is needed for API/Ollama terminal sessions. |

### Supported slash commands

Core: `/login`, `/logout`, `/provider`, `/model`, `/status`, `/help`, `/clear`, `/exit`.
Sessions: `/sessions`, `/resume`, `/usage`, `/compact` (shared runtime local compaction with recent messages preserved).
UI: `/theme`, `/mode`, `/chat`, `/code`.
Coding helpers: `/diff`, `/run`, `/git`, `/search`, `/files`, `/init`.

Claw commands that are placeholders or not backed by current Kim behavior are intentionally hidden from `kim`.

### Provider and login behavior

- Default provider is `ollama` with `http://127.0.0.1:11434/v1` compatibility.
- `/login` defaults to Ollama. On **Windows**, `ollama signin` is piped (no interactive TTY), so the CLI immediately opens `https://ollama.com/signin` in the default browser and then sets provider to `ollama`. If `ollama` is not on PATH it prints a clear install link. On macOS/Linux, `ollama signin` is tried first (15 s timeout) with browser fallback. API key login requires an explicit `/login claude|openai|gemini|deepseek`.
- `/login claude|openai|gemini|deepseek` prompts for an API key and saves it in `~/.kim/cli-config.json`.
- `/provider desktop` posts to Kim desktop's bridge at `http://127.0.0.1:18991/v1/task`; this requires the desktop app to be running.
- The CLI does not embed a browser/webview and does not spawn Node/Electron, preserving a low memory profile.
- The sidebar is a real session selector: `/sessions` refreshes it, `↑` / `↓` select entries when the input is empty, and empty `Enter` opens the selected JSONL.

### Installer / release flow

`scripts/install-kim.sh` is the macOS/Linux/Git-Bash installer scaffold. It detects OS/arch, downloads a private GitHub Release asset named like `kim-aarch64-apple-darwin.tar.gz` or `kim-x86_64-pc-windows-msvc.zip`, installs it to `~/.kim/bin/kim` or `~/.kim/bin/kim.exe`, and supports private release downloads through `GITHUB_TOKEN`.

`scripts/install-kim.ps1` is the native Windows PowerShell installer for the same `kim` TUI binary. It downloads `kim-x86_64-pc-windows-msvc.zip` or `kim-aarch64-pc-windows-msvc.zip`, extracts `kim.exe`, installs it to `%USERPROFILE%\.kim\bin\kim.exe`, and adds that directory to the user's PATH.

One-command Windows beta install target:

```powershell
powershell -ExecutionPolicy Bypass -c "iwr https://raw.githubusercontent.com/AdamMagued/kim/main/scripts/install-kim.ps1 -UseB | iex"
```

For private release assets, set `GITHUB_TOKEN` before running the installer. The release must publish the matching Windows ZIP asset containing `kim.exe`; the script reports the exact missing asset name if it cannot download it.

Known v1 limitation: macOS is the polished target. Linux and Windows installers are present, but full Windows QA still requires publishing signed or trusted release binaries and smoke testing `kim`, `/login`, `/provider`, `/model`, and one Ollama prompt on a real Windows terminal.

#### Windows-specific hardening (applied 2026-05-18)

| Issue | Fix |
|---|---|
| Duplicate/triple characters on key press/release | Key events filtered to `KeyEventKind::Press \| Repeat`; Release events ignored |
| Right-click paste corrupts commands | Bracketed paste fires `Event::Paste`; pasted text checked for existing file paths first; in chat view non-path text is accepted as-is; in session menu non-path paste is dropped |
| `ollama signin` hangs 15 s on Windows | Windows skips `signin` entirely, probes `ollama --version` to detect install, then opens browser immediately |
| Crash leaves terminal raw mode broken | `install_panic_hook()` restores `disable_raw_mode + LeaveAlternateScreen + DisableBracketedPaste` before printing panic message |
| Windows cross-compile target | `x86_64-pc-windows-gnu` builds cleanly; release CI uses `windows-latest` (MSVC, default target) matching installer asset name `pc-windows-msvc` |

---

## 2.9 `tests/`

| File | Style | Coverage |
|---|---|---|
| `test_ollama_provider.py` | unittest | 8 tests — context-limit parsing, env override, image normalization, tool-call format round-trip |
| `test_browser_protocol.py` | unittest | Codex bridge response mapping, dynamic transport markers, lighter recap for stored threads; BrowserProvider tests skip without playwright |
| `test_context_meter.py` | pytest fns | 5 tests — phase thresholds, budget coercion, fallback estimation |
| `test_gemini_oauth_provider.py` | pytest fns | 3 tests — auth-mode disambiguation, REST contract, response parsing. Uses `install_google_stubs()` to mock the SDK |
| `test_gemini_user_project_mode.py` | unittest classes (4) | 14 tests — `oauth_user_project` strict validation, error messages, no-fallback, mode transitions |
| `kim_test_suite.py` (1,200 lines) | custom runner | 38 end-to-end tests via `kimctl send` or `python -m orchestrator.agent`. Categories: math/files/shell/search/visual/chain/safety/recovery/stress/click/hard |
| `claw_test_suite.py` (1,286 lines) | custom runner | 67 tests for the `claw` Rust binary. **Binary not in repo** — all bridge-mode tests fail |

---

# 3. Method / Function Map

This section lists notable cross-references — where a key method is defined,
where it's called from, what it returns, and what would break if it changed.

## 3.1 `lib.rs:send_task` (line 7121)

- **Called from**: ChatView's `handleSend` (via `invoke('send_task')`), `/v1/task` HTTP route (kimctl).
- **Spawns**: a Python subprocess. Branch:
  - If `provider` starts with `browser` and the task is in Code mode → `python -m orchestrator.run_codex_bridge --task … --cwd … --provider browser:gemini`.
  - Else if direct Code mode → spawns the configured Codex/backend process directly.
  - Else → `python -m orchestrator.agent --task …`.
- **Env injection**: `KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`, `PROJECT_ROOT`, `KIM_PREFERRED_SITE`, `KIM_BROWSER_RESTORE_STATUS` (if a sidecar was restored), Google OAuth pairs from `google_oauth::AgentGoogleOAuthEnv::as_env_pairs()`.
- **Returns**: nothing (streams stdout via `agent-output` event; ends with `kim-agent-done`).
- **Depends on**: `find_python_interpreter`, `default_project_root`, `validate_session_id`, Codex/backend resolution helpers.
- **Breaks if changed**: every UI task path, all kimctl `send` calls, and Code-mode integration.

## 3.2 `agent.py:KimAgent.run` (line 646)

- **Called from**: `mcp_agent_context` (everywhere — tray, relay worker, send_task subprocess).
- **Depends on**: provider (`_call_with_retry`), MCP session (`_execute_tool`), `ConversationMemory`, `ContextMeter`, `SessionStore`, `UIBridge`.
- **Returns**: `{success: bool, summary: str, screenshot?: str}`.
- **Edge cases handled**: cancellation mid-loop, stuck detection (3 identical screenshots), conversational loop (3 text-only turns), retryable LLM errors (with backoff), `AUTH_FAILED:` short-circuit from `web_open`.
- **Breaks if changed**: every task. Test with `tests/kim_test_suite.py` math+files batches.

## 3.3 `lib.rs:handle_webview_bridge_request` (line 3866)

- **Called from**: `start_webview_bridge_server` for every HTTP request.
- **Auth gate**: line 3884 requires `X-Kim-Token` matching `WEBVIEW_BRIDGE_CFG.token` for everything except `GET /v1/health` and `GET /v1/status`.
- **Returns**: `tiny_http::Response`.
- **Depends on**: numerous globals (`WEBVIEW_BRIDGE_RESULTS`, `WEBVIEW_BRIDGE_NOTIFY`, `BRIDGE_TASK_PID`, …) and the JS bridge running in the hidden webview.
- **Breaks if changed**: every browser-provider task. The kimctl CLI also depends on `/v1/task`, `/v1/cancel`.

## 3.4 `providers/browser/provider.py:BrowserProvider.complete` (line ~270)

- **Two transports**: in-app webview bridge OR direct Playwright CDP. Selected at init by env vars (`KIM_WEBVIEW_BRIDGE_URL`).
- **State**: `_sent_system_prompt`, `_last_provider_url`, `_active_conversation_id`. Reset on URL change or provider change.
- **Depends on**: `bridge_client.py` (bridge mode), `playwright` (CDP mode), `SITE_CONFIGS` from `providers/browser/site_configs.py`.
- **Returns**: same shape as other providers (`tool_call` | `text`).
- **Side effects in CDP mode**: opens a new Playwright context **on every call** — very expensive (25 LLM iterations = 25 CDP handshakes).

## 3.5 `mcp_server/tools/files.py:handle_write_file` (line 26)

- **Called from**: every tool dispatch via `server.py:call_tool`.
- **Auto-detects binary**: `content.startswith("data:") and ";base64," in content[:64]` → strips prefix, base64-decodes, opens `"wb"`.
- **Path safety**: `validate_path(path)` — refuses paths outside `ALLOWED_PATHS` or inside `_SENSITIVE_PATHS`.
- **Side effects**: creates parent dirs (`parents=True, exist_ok=True`); overwrites file.

## 3.6 `relay_server/queue.py:TaskDB.dequeue` (line ~250)

- **Called from**: `GET /prompt/next` route.
- **Atomicity**: `BEGIN IMMEDIATE` then expire stale → SELECT highest-priority pending → UPDATE running. Commits or rolls back.
- **Returns**: `{task_id, task}` or `None`.
- **Fragile**: any concurrent `enqueue/complete` during the `BEGIN IMMEDIATE` window raises `OperationalError: database is locked`. Server is single-worker so this is rare but a real risk for any future `asyncio.gather` parallelism.

---

# 4. Reference / Dependency Map

## 4.1 Process-level dependencies

```
                            ┌──────────────────────────┐
                            │  Tauri app (Kim.app)     │
                            │  desktop/src-tauri       │
                            └──────────┬───────────────┘
                                       │ spawns subprocess
                                       ▼
                ┌──────────────────────────────────────────┐
                │  orchestrator/agent.py  (KimAgent)       │
                │  imports orchestrator/providers/*        │
                └──────────────┬───────────────────────────┘
                               │ stdio (MCP)
                               ▼
                ┌──────────────────────────────────────────┐
                │  mcp_server/server.py                    │
                │  imports mcp_server/tools/*              │
                │  imports mcp_server/sites/*              │
                └──────────────────────────────────────────┘

                ┌──────────────────────────────────────────┐
                │  relay_server (separate process,         │
                │  deployed to Railway/Render)             │
                └──────────────┬───────────────────────────┘
                               │ HTTP polled by
                               ▼
                ┌──────────────────────────────────────────┐
                │  orchestrator/relay_worker.py            │
                │  → mcp_agent_context → KimAgent          │
                └──────────────────────────────────────────┘

                ┌──────────────────────────────────────────┐
                │  tray/app.py (legacy)                    │
                │  imports orchestrator/agent + relay_worker│
                └──────────────────────────────────────────┘

                ┌──────────────────────────────────────────┐
                │  kimctl/__main__.py                      │
                │  HTTP → lib.rs's in-app bridge           │
                │  Direct read of session JSONL files      │
                └──────────────────────────────────────────┘
```

## 4.2 React component → Tauri command map

| Component | Tauri commands invoked |
|---|---|
| `App.tsx` | `get_app_version`, `summarize_session` |
| `ChatView.tsx` | `set_task_active_mode`, `show_screenshot_flash`, `save_run_history`, `load_run_history`, `load_session_messages`, `send_task`, `cancel_task`, `set_browser_keep_visible`, `session_browser_meta_*`, `session_browser_url_commit`, `restore_browser_for_session`, `navigate_browser_window_if_open`, `open_browser_signin_window` |
| `RevampSettings.tsx` | `read_voice_config`, `write_voice_config`, `read_relay_config`, `write_relay_url`, `ollama_get_status`, `ollama_test_model`, `ollama_pull_model`, `verify_github_pat`, `export_data`, `import_data`, `backup_to_gist`, `restore_from_gist`, `delete_all_sessions`, `clear_account`, `reset_onboarding`, `add_code_project`, `remove_code_project`, `open_in_finder`, `send_feedback`, `google_oauth_*` |
| `RevampSidebar.tsx` | `list_sessions` (via hook), `delete_sessions`, `list_codex_projects` |
| `OnboardingFlow.tsx` | `verify_github_pat`, `open_browser_signin_window`, `provider_signin`, `save_account` |
| `ProviderPicker.tsx` | `ollama_get_status`, `ollama_pull_model`, `provider_signin`, `provider_signout` |
| `BrowserProviderPicker.tsx` | `open_browser_signin_window` |
| `PairingModal.tsx` | `relay_pair_init`, `relay_pair_status` |
| `AuthIndicator.tsx` | `provider_check_auth`, `provider_signin` (via hook) |
| `UpdateModal.tsx` | `get_platform_info`, `run_update` |
| `CancelWidget.tsx` | `cancel_task` |

## 4.3 Tauri events emitted by Rust → consumed by React

| Event | Emitted from | Consumed by |
|---|---|---|
| `agent-output` | `send_task` stdout pump | `ChatView` (parses `[STATUS]/[TOOL]/[DIFF]/[USAGE]/[CONTEXT]/[PLAN]/...`) |
| `kim-agent-done` | `send_task` exit handler | `ChatView` (finalizes run, persists run history) |
| `kim-update-progress` | `run_update` step callbacks | `UpdateModal` |
| `kim-auth-changed` | `provider_signin`/`provider_signout`/post-signin watcher | `useAuthStatus` (re-probe after 400ms cookie settle) |
| `kim-browser-restore-result` | `restore_browser_for_session` | `ChatView` (toast on fallback) |

## 4.4 Env vars consumed across processes

| Var | Source | Consumed by |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env` | `providers/claude.py` |
| `OPENAI_API_KEY` (+ `openai_api_key_env` config override) | `.env` | `providers/openai_provider.py` |
| `DEEPSEEK_API_KEY` | `.env` | `providers/deepseek.py` |
| `GOOGLE_API_KEY` | `.env` | `providers/gemini.py` (legacy api_key mode) |
| `KIM_GOOGLE_ACCESS_TOKEN` | injected by `lib.rs` from `google_oauth.rs` | `providers/gemini.py` |
| `KIM_GOOGLE_USER_PROJECT_ID` | injected by `lib.rs` | `providers/gemini.py` |
| `KIM_GEMINI_AUTH_MODE` | injected by `lib.rs` | `providers/gemini.py` |
| `KIM_WEBVIEW_BRIDGE_URL` / `_TOKEN` | injected by `lib.rs:send_task` | `providers/browser/provider.py`, `kimctl` |
| `KIM_PREFERRED_SITE` | `lib.rs` | `providers/browser/provider.py` |
| `KIM_BROWSER_RESTORE_STATUS` | `lib.rs:restore_browser_for_session` | `providers/browser/prompt_builder.py` (lighter recap) |
| `KIM_OLLAMA_BASE_URL` / `_MODE` / `_LOCAL_MODEL` / `_CLOUD_MODEL` | `.env` or settings | `providers/ollama.py` |
| `RELAY_PC_API_KEY` | `.env` | `relay_worker.py`, `task_queue.py`, `lib.rs:relay_pair_*` |
| `RELAY_PHONE_API_KEY` | `.env` | `relay_server/auth.py` |
| `HUME_API_KEY` | `.env` | `tray/voice.py:HumeVoiceProvider` |

---

# 5. Feature Map

## 5.1 "Type a task → Kim does it" (the core feature)

| Step | Where |
|---|---|
| 1. User types in composer | `ChatView.tsx` |
| 2. Click Send | `handleSend` in `ChatView.tsx` |
| 3. `invoke('send_task', {task, sessionId?, provider?, projectRoot?})` | `lib.rs:send_task` (7121) |
| 4. Spawn Python subprocess | `lib.rs:send_task` (Tokio `Command::spawn`) |
| 5. `KimAgent.run(task)` | `agent.py:646` |
| 6. Provider `complete()` per iteration | `orchestrator/providers/*.py` |
| 7. MCP `call_tool()` per tool | `mcp_server/server.py:call_tool` |
| 8. stdout streamed back | `lib.rs:send_task` event emitter |
| 9. UI renders activity feed | `ChatView.tsx:ActivityFeed` |

## 5.2 Provider sign-in (browser:* modes)

| Step | Where |
|---|---|
| 1. User clicks provider chip | `ProviderPicker.tsx` or `AuthIndicator.tsx` |
| 2. `invoke('provider_signin', {provider})` | `lib.rs:provider_signin` (6814) |
| 3. Opens `kim-browser-signin` WebviewWindow | `open_browser_signin_window_with_visibility` (2243) |
| 4. User signs in inside webview | external (provider's auth page) |
| 5. Post-signin watcher detects success URL | `spawn_post_signin_watcher` (6766) |
| 6. Emits `kim-auth-changed` event | `lib.rs` |
| 7. `useAuthStatus` re-probes after 400ms | `useAuthStatus.ts` |
| 8. Chip turns green | `AuthIndicator.tsx` |

## 5.3 Phone pairing (relay)

| Step | Where |
|---|---|
| 1. PC user clicks "Pair phone" | Settings (`RevampSettings.tsx`) opens `PairingModal` |
| 2. `invoke('relay_pair_init')` | `lib.rs:relay_pair_init` (7955) — calls `POST /pair/init` |
| 3. QR + 6-char code rendered | `PairingModal.tsx` |
| 4. Phone scans QR | external |
| 5. Phone calls `POST /pair/complete` | `relay_server/main.py` |
| 6. Server INSERTs into `devices`, returns `device_token` | `queue.py:complete_pairing` |
| 7. PC polls `/pair/status/{code}` every 2s | `PairingModal.tsx` |
| 8. Modal shows "Paired with …", auto-closes | `PairingModal.tsx` |

## 5.4 Phone-submitted task → executed on PC

| Step | Where |
|---|---|
| 1. Phone `POST /prompt` with task | `relay_server/main.py` |
| 2. INSERT into `tasks` (pending) | `queue.py:enqueue` |
| 3. PC's `orchestrator/relay_worker.py` polls `/prompt/next` | `relay_worker.py` |
| 4. Task picked up; status → running | `queue.py:dequeue` |
| 5. `relay_worker._run_one` enters `mcp_agent_context` | `relay_worker.py:65` |
| 6. `KimAgent.run(task)` | `agent.py` |
| 7. PC `POST /result` with summary + screenshot | `relay_worker.py`/`task_queue.py` |
| 8. Server UPDATE → done; WS broadcast | `relay_server/main.py` |
| 9. Phone gets result via WS or polling | external |

## 5.5 Session persistence + browser thread restore

| Step | Where |
|---|---|
| 1. Per turn, message appended to JSONL | `session_store.py:append_message` |
| 2. After browser-provider run, sidecar URL committed | `lib.rs:session_browser_url_commit` (POST `/v1/browser/commit-url`) |
| 3. URL validated by allowlist (`browser_url_allowed_for_restore`) | `lib.rs:553` |
| 4. User reopens session | `ChatView.tsx:loadSession` → `invoke('restore_browser_for_session')` |
| 5. Rust reads `.browser.json`, validates URL, navigates webview | `lib.rs:6446` |
| 6. `KIM_BROWSER_RESTORE_STATUS=stored_thread` set for next task | `lib.rs:send_task` (8158) |
| 7. `BrowserProvider._format_prompt` uses lighter recap when restored | `providers/browser/prompt_builder.py` |

## 5.6 Context budget + compaction

| Step | Where |
|---|---|
| 1. Each LLM call updates cumulative tokens | `context_meter.py:ContextMeter.observe_usage` |
| 2. Snapshot persisted to `.context.json` | `agent.py:_save_context_state` |
| 3. Ring fills as % of `context_budget_tokens` (default 200k) | `kim-ui/ContextRing.tsx` |
| 4. User clicks ring → opens compact menu | `ContextRing.tsx` |
| 5. "Compact now" enqueues a special task | `_COMPACT_CONTROL_TASKS` in `agent.py:76` |
| 6. `_compact_and_reset_context()` runs | `agent.py:1396` |
| 7. Summary saved to `.compact.<stamp>.json`; memory cleared; `needs_fresh_chat` set | `agent.py` + `session_store.py` |
| 8. Next call uses `clear_chat=True` so browser provider starts a new chat | `BrowserProvider.complete` |

## 5.7 Voice (TTS)

| Step | Where |
|---|---|
| 1. Toggle in Settings / Tray | `RevampSettings.tsx` voice pane / `tray/ui.py` |
| 2. `write_voice_config` writes `config.yaml` voice section | `lib.rs:7757` |
| 3. `VoiceEngine` built from config | `tray/voice.py:_build_provider` |
| 4. Agent calls `voice.speak(text)` on TASK_COMPLETE / NEED_HELP / stuck / tool start | `agent.py:_voice_speak` |
| 5. Text sanitized (`clean_for_speech`) | `voice.py` |
| 6. Provider chain: primary → fallbacks (Kokoro, HTTP) | `voice.py:_build_fallback_chain` |
| 7. Audio played via `sounddevice` | `voice.py` per-provider `speak_sync` |

---

# 6. Debugging Guide

## 6.1 Where to look — by symptom

### "Task won't start"
1. `App.tsx` — is the send button disabled because `agentStatus` is stuck?
2. `lib.rs:send_task` (7121) — did `find_python_interpreter` find Python? Did `default_project_root` resolve?
3. MCP server: `python -m mcp_server.server` shouldn't crash on import. Watch for `web.py` import-time `USER_DATA_DIR` resolution.
4. Check `agent-output` event listener is attached in `ChatView`.

### "Agent runs but LLM never responds"
- **API mode**: `.env` keys; provider in `config.yaml`. Check `_call_with_retry` logs for 401/403/429.
- **Browser mode**: bridge env vars (`KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`); webview signed in (`show_browser_window`); `bridge_debug.log` in sessions dir; selector drift in `SITE_CONFIGS`.

### "Browser provider returns empty / old response"
- Selectors changed: check `SITE_CONFIGS` in `providers/browser/site_configs.py` and `PERSISTENT_BRIDGE_JS` in `lib.rs`.
- Stale completion hash: `lib.rs` URL-change observer should clear `_lastHash` when path changes.
- Race: ensure `_send_and_wait` is waiting for new response element BEFORE polling completion (fixed in 47-bug sweep — see CHANGELOG 2026-05-11).

### "Browser sign-in modal won't close after login"
- `spawn_post_signin_watcher` (6766) polls URL patterns from `post_signin_url_patterns(site)`. If the provider changed its post-signin URL, that's the cause.

### "Settings don't persist"
- `localStorage('kim-settings')` for frontend prefs.
- `~/.kim/account.json` for account data (via `save_account`).
- `config.yaml` for `voice.*` and `relay.url` (via `write_voice_config` / `write_relay_url`).

### "Sessions vanish after task completion"
- See `App.tsx:handleTaskDone` (236-280) — has multiple delayed `refresh()` calls because Python flushes the last JSONL line on exit and the OS may take a beat to make the file visible.
- Watch out for the "wipe `liveHistory`" bug (was fixed in 2026-05-11; check `ChatView` `useEffect`s).

### "Voice doesn't speak / agent hangs"
- See **BUGS_PENDING.md Bug 1** — Hume `urlopen(timeout=15)` blocks the loop for 15s when `HUME_API_KEY` is missing/invalid. Recommended fix: validate key at speak time, drop timeout to 3s, wrap in try/except, run TTS off the critical path with `asyncio.create_task`.

### "observe_ui says Accessibility is missing even though it isn't"
- See **BUGS_PENDING.md Bug 2** — preflight uses AppleScript that maps non-zero exits to a generic permission error. Most often triggered by a dev rebuild changing the binary signature. Workaround: toggle Kim off/on in System Settings → Accessibility.

### "Theme not applying"
- `useTheme.ts` reads `kim-theme` from localStorage.
- `index.css` `.dark` class toggles via `useTheme.applyTheme`.

### "Screenshot flash not working"
- Agent prints `[UI] SCREENSHOT_FLASH` to stdout.
- `App.tsx` listens on `agent-output`, calls `invoke('show_screenshot_flash')`.
- `lib.rs:show_screenshot_flash_impl` (5427) shows the `screenshot-flash` window briefly.

### "Provider switcher breaks mid-task"
- `ChatView.tsx` should block provider switching while a task runs (added in second patch). Verify the guard is intact.

### "Update modal looks frozen"
- `run_update` (6035) emits `kim-update-progress` events. `UpdateModal.tsx` appends each to `progress`. If no events arrive, check Tauri event channel and that the update payload exists for the platform (`get_platform_info`).

### "Tests fail with `module not found tests`"
- Run as `PYTHONPATH=. python3 tests/test_browser_protocol.py`. **Do not** use `python -m unittest tests.test_browser_protocol` — a third-party `tests` package on PYTHONPATH can shadow the local folder (see `KIM_BROWSER_RELIABILITY_PATCH_NOTES.md`).

## 6.2 Quick subsystem-to-file table

| Concern | Files |
|---|---|
| **Frontend bugs (UI rendering, state, events)** | `desktop/src/App.tsx`, `desktop/src/components/ChatView.tsx`, `desktop/src/components/kim-ui/*`, `desktop/src/hooks/*` |
| **Backend bugs (Tauri commands, bridge HTTP, JS bridge)** | `desktop/src-tauri/src/lib.rs`, `desktop/src-tauri/src/google_oauth.rs` |
| **Agent loop / LLM bugs** | `orchestrator/agent.py`, `orchestrator/providers/*.py`, `orchestrator/memory.py`, `orchestrator/context_meter.py` |
| **Tool bugs (clicks, files, shell)** | `mcp_server/tools/*.py`, `mcp_server/server.py` |
| **Cross-platform issues** | `mcp_server/os_utils.py`, `mcp_server/tools/windows.py`, `mcp_server/tools/shell.py` |
| **DB / queue / pairing bugs** | `relay_server/queue.py`, `relay_server/main.py`, `relay_server/auth.py` |
| **Routing/API bugs** | `relay_server/main.py`, `lib.rs:handle_webview_bridge_request` (3866) |
| **Authentication/authorization** | `lib.rs:provider_check_auth/_signin/_signout`, `lib.rs:google_oauth.rs`, `relay_server/auth.py` |
| **Voice / TTS** | `tray/voice.py`, `orchestrator/agent.py:_voice_speak` |
| **Session storage / restore** | `orchestrator/session_store.py`, `lib.rs:session_browser_meta_*`, `lib.rs:restore_browser_for_session` |

---

# 7. Risks and Unclear Areas

## 7.1 Dead / superseded code

| Code | Why it looks dead |
|---|---|
| `desktop/src/components/Sidebar.tsx` (867 lines) | `App.tsx` only imports `RevampSidebar`. Old version. Verify with `rg "from './Sidebar'"`. |
| `desktop/src/components/SettingsPanel.tsx` (1,680 lines) | `App.tsx` only imports `RevampSettings`. Old version. |
| `desktop/src/design-mocks/` | Mocks, not imported by the running app. |
| `mcp_server/tools/test_extract.py` | Scratch file with hardcoded personal path, executes at import. |
| `mcp_server/tools/windows.py:handle_open_url` | Defined but `_DISPATCH["open_url"]` maps to `handle_web_open` (Playwright). Unreachable. |
| `mcp_server/config.BLOCKED_COMMANDS` | Loaded from `config.yaml:shell.blocked_commands` but `shell.py` uses its own hardcoded `_DENY_COMMANDS` set; the config key is dead. |
| `mcp_server/config.BROWSER_HEADLESS` | Loaded but never referenced from `web.py`. |
| `mcp_server/logger.setup_structured_logging` | Defined but never called at server startup. Structured JSONL logs never produced. |
| `relay_server/queue.list_devices` / `revoke_device` | No HTTP endpoint exposes them. |
| `extension/*` (whole directory) | POSTs to `localhost:3000` which has no server. Desktop runs on `:18991`. Extension and Tauri app are disconnected. |
| Extension `content_*.js`'s initial `chrome.storage.local.get([\`loop_${chrome.runtime.id}\`, ...])` | Uses wrong key (extension ID instead of tab ID); is a no-op. |

## 7.2 Duplicate / parallel logic

| Pair | Note |
|---|---|
| `SettingsPanel.tsx` vs `kim-ui/RevampSettings.tsx` | The "Settings → Voice" pane lives in both. Voice catalog is in `types/index.ts`. |
| Selector maps in `providers/browser/site_configs.py:SITE_CONFIGS` vs `lib.rs:PERSISTENT_BRIDGE_JS` | Must be kept in sync manually. |
| `mcp_server/tools/shell.py:_DENY_COMMANDS` vs `config.BLOCKED_COMMANDS` | Two parallel deny lists; only the hardcoded one is enforced. |
| `tray/voice.py` providers vs voice catalog in `desktop/src/types/index.ts:VOICES_BY_ENGINE` | Engine names must match. |

## 7.3 Confusing naming

- `open_url` MCP tool actually means "open in the controlled Playwright browser", not the system default browser. The tool description still says "system default browser".
- `tools/test_extract.py` looks like a pytest file but isn't a test.
- `desktop/src/components/Sidebar.tsx` and `desktop/src/components/kim-ui/RevampSidebar.tsx` — only the second is active.
- `mcp_server/tools/windows.py` is named after the *platform* but contains cross-platform window-management code (not platform-specific).
- `voice.backend` and `voice.engine` config keys both exist; `voice.py` checks `voice.engine` first, falls back to `voice.backend`.

## 7.4 Missing connections

- Extension → bridge: `localhost:3000/sync` has no server in this repo.
- `claw` binary: `tests/claw_test_suite.py` expects `pythonExperimentTool/claw-code/rust/target/debug/claw`; not built into the repo.
- Relay healthcheck: `railway.toml` healthchecks `/status`, but `/status` requires `X-API-Key`. Without configuring the healthchecker, Railway will mark the deployment unhealthy.
- Dockerfile CMD hardcodes `--port 3001` ignoring `$PORT` — only Railway works because `railway.toml`'s `startCommand` overrides CMD.

## 7.5 Incomplete features

- `sites/guc_cms.py` and `sites/guc_mail.py` are stubs. Their `ConnectorsPanel` UI is wired but the tools are placeholders.
- `task_queue.py` module docstring marks itself "dormant — not yet wired into the Tauri send_task flow." `relay_worker.py` is the new piece that wires it up.
- `BrowserProvider`'s custom-site path exists but is gated behind config; UX is "Custom" tile in `BrowserProviderPicker`.
- `kimctl browser meta/commit-url/restore` subcommands exist but the JSON output formatting is inconsistent with the rest (uses `hasattr(args, "json")` instead of `args.json`).

## 7.6 Possible bugs (cross-listed in [§8](#8-bug-inventory))

See the next section for the full list.

---

# 8. Bug Inventory

The agents flagged ~50 distinct issues. Severity: 🔴 critical (breaks core
flows), 🟡 medium (annoying or insecure under specific conditions), ⚪ minor
(dead code, code quality, fragile).

## 8.1 Known live bugs (from `BUGS_PENDING.md`)

🔴 **Hume voice blocks the agent loop (~15s freezes)** — `tray/voice.py:~681`
`urllib.request.urlopen(req, timeout=15)`. When `HUME_API_KEY` is missing/
invalid, the call hangs the entire agent loop because `_voice_speak` in
`orchestrator/agent.py` is on the critical path (or under a lock). Fix
recipe: validate key at speak time, drop timeout to 3s, wrap in try/except,
make voice fire-and-forget.

🟡 **`observe_ui` spuriously claims macOS Accessibility is missing** —
`mcp_server/tools/ui_observe.py:~174-192`. AppleScript preflight maps any
non-zero exit to "permission needed". Most often triggered when a dev
rebuild changes the binary code-sign hash. Fix: replace AppleScript probe
with a native `AXIsProcessTrustedWithOptions` call.

## 8.2 Frontend (`desktop/`)

🟡 **`silentUpdateCheck` fires on every cold start** — `App.tsx:159`. Hits
`api.github.com` on every launch. GitHub rate-limits unauthenticated
requests to 60/hour per IP; users behind shared NATs (offices, dorms) will
hit the limit and get spurious "Could not reach GitHub" toasts.

⚪ **Sidebar.tsx and SettingsPanel.tsx are large dead files** — ~2,500 lines
total. Slow IDE indexing, confusing onboarding.

⚪ **`App.tsx:282-291` polls `kimSessions` for the just-completed
sessionId** — fine when it appears within `setTimeout(refresh, 400/1200/2400)`,
but if Python takes longer than 2.4s to flush JSONL, the auto-navigate
fails silently.

⚪ **Devtools shortcuts blocked via JS only** — `App.tsx:128-140`. A
determined user can still hit Inspect via right-click extensions; for
release builds this is belt-and-braces but for dev builds the JS handler
also fires.

## 8.3 Rust / Tauri (`lib.rs`, `google_oauth.rs`)

🟡 **9,798-line `lib.rs` is a god-file** — Tauri commands, HTTP server, JS
bridge script, helper functions, OS-specific stubs all in one file. Refactor
to modules (`commands/`, `bridge/`, `sessions/`, `oauth/`) when feasible.

🟡 **`/v1/status` requires auth but Railway healthcheck doesn't send a
key** — `railway.toml:healthcheckPath = "/status"`. Will mark deployments
unhealthy and trigger restart loops. Fix: either add a public
`/v1/health` route (already exists!) and switch Railway healthcheck to it,
or whitelist `/status` for unauthenticated GET.

🟡 **`build_bridge_complete_script` placeholder injection** — `lib.rs:2543`.
Builds a JS payload with hardcoded `__KIM_SITE__` / `__KIM_REQID__`
placeholders that get string-replaced. The test at line 9781 explicitly
verifies "no poisoning" — but the substitution is `str::replace` not regex
with anchors. A prompt containing the placeholder string would be replaced
verbatim. The test doesn't cover the case where the prompt itself contains
`__KIM_SITE__`.

⚪ **Test at line 9781 only checks that placeholders don't leak from the
*prompt* into the substitution markers** — does not test that user input
cannot escape the JS string context (XSS-like concern inside the WebView).

## 8.4 Orchestrator (`orchestrator/`)

🔴 **OpenAI/DeepSeek/Ollama multi-tool responses surface as text errors**
(`openai_provider.py:151-162`, inherited by `deepseek`, `ollama`). When the
LLM emits >1 `tool_calls` in one response, the provider returns
`{"type":"text","content":"SYSTEM ERROR: …"}`. The agent treats it as a
text turn, increments `consecutive_continues`, and after 3 returns
`NEED_HELP` ("stuck in loop"). Claude correctly wraps as `batch`. Fix:
wrap as `batch` in OpenAI provider too, OR pass through the first tool_call
and warn about the dropped ones.

🟡 **`DeepSeekProvider.__init__` does not call `super().__init__()`** —
`deepseek.py:21-36`. Works today because `complete()` only uses
`_client`, `_model`, `_max_tokens` — but any new attribute added to
`OpenAIProvider.__init__` will silently break DeepSeek.

🟡 **Default Claude model is `claude-opus-4-5`** — `claude.py:26`. CLAUDE.md
specifies `claude-opus-4-6`. Config.yaml currently has `claude-opus-4-6`
so this only bites users who don't customize config.

🟡 **MultiMCPClient silent last-wins on duplicate tool names** —
`agent.py:301`. If two MCP servers expose `take_screenshot`, the second
overwrites the first with no warning. Fix: raise on conflict or prefix
names with server label.

🟡 **`agent.py:329` env merge does not isolate** — `merged_env =
{**os.environ, **extra_env} if extra_env else None`. When `extra_env`
is empty, child inherits `None` → unrestricted parent env. Probably
intentional but worth documenting.

🟡 **`print()` to stdout from `KimAgent`** — `agent.py:654, 1007`. Lines like
`print("[STATUS] …", flush=True)` go to stdout. When the agent runs as
the MCP server's parent (orchestrator side), stdout contamination is
fine. But if anyone ever runs the agent as an MCP child, those prints
would corrupt the MCP protocol stream.

🟡 **`BrowserProvider` creates a new `async_playwright()` context per
call** — `browser_provider.py:507-575`. For a 25-iteration task this
means 25 CDP handshakes. Major perf issue in CDP mode. (Bridge mode is
not affected.)

🟡 **`providers/gemini.py:52` `GEMINI_OAUTH_SCOPE` may not grant
`generateContent`** — scope is `generative-language.retriever`. Real
Gemini API calls hit `:generateContent`. The code comment itself flags
uncertainty. Verify against Google's token policy.

⚪ **`agent.py:461-465` class-level attribute re-declarations** shadow
instance attributes set in `__init__`. Cosmetic; not a runtime bug.

⚪ **`session_store.py:list_sessions` opens every JSONL to count lines** —
O(N) blocking I/O on every session load. Cap at 50-100 sessions today
but will degrade.

⚪ **`session_store.py:find_session_file` scans all date dirs each call** —
sorted reverse, no index. Same O(N) concern.

⚪ **`context_loader.discover_instruction_files` walks to filesystem
root** — `context_loader.py:57-65`. No depth limit. On macOS this reads
`/`, `/Users`, etc. on every agent run. Add a max-depth of ~15.

⚪ **Lazy `import os as _os` inside `_execute_tool`** —
`agent.py:1044-1048`. Caches after first call; cosmetic only.

## 8.5 MCP server (`mcp_server/`)

🔴 **`git_diff`, `git_add`, `git_checkout` don't validate user paths** —
`git.py:97-186`. An LLM hallucinating `../../etc/passwd` would have `git`
happily diff or restore that path relative to the repo. Add
`validate_path` to all path-accepting git tools.

🟡 **`open_url` dispatch maps to `handle_web_open` (Playwright), not
`handle_open_url` (system browser)** — `server.py:740`. Tool description
says "system default browser"; behavior is the in-app Playwright browser.
Either fix the dispatch or update the description.

🟡 **`type_text` ignores its advertised `interval` argument** —
`keyboard.py:6-21`. Schema documents the parameter but the clipboard-paste
implementation can't honor it. Remove from schema or implement per-key
typing as a separate tool.

🟡 **`run_powershell` passes `allow_chaining=True` unconditionally** —
`shell.py:186`. Disables the metachar filter for all PS scripts. PS
scripts naturally chain, but this means any malicious payload an LLM
generates as PS won't be filtered for `;|$()`.

🟡 **`shell.blocked_commands` config key is dead** — `shell.py` uses its
own `_DENY_COMMANDS` frozenset; config edits do nothing. Either honor
the config or remove the dead config.

🟡 **`setup_structured_logging` never called** — `logger.py` exists but
`server.py` only sets up basic logging to stderr. Production-debugging
JSONL files are silently never created. Wire it up in `server.py` main.

🟡 **`USER_DATA_DIR` resolved at import time in `web.py:76`** — calls
`_resolve_user_data_dir()` which creates directories. If PROJECT_ROOT is
misconfigured, import fails and the MCP server can't start.

🟡 **`web_observe` clears `_element_map` but `web_open` doesn't** —
`web.py:50, 458-460`. After navigation, stale element IDs from the
previous page remain valid until the next `web_observe`. LLM may
click the wrong thing.

⚪ **`_find_node()` raises `RuntimeError` instead of returning error
string** — `code.py:95`. Caught by handler today but violates the "tools
return errors, never raise" contract.

⚪ **`web_wait_for` selector vs text heuristic is fragile** —
`web.py:593-600`. Strings containing `:` (like `"Price: $5"`) get treated
as CSS selectors. Pass `as_selector` explicitly instead.

⚪ **`BROWSER_HEADLESS` config key is loaded but unused** — `config.py:64-66`.

⚪ **`test_extract.py` ships in `mcp_server/tools/`** — scratch file with a
hardcoded `/Users/adammaged/Desktop/Personal/pongTEST` path that executes
at import. Move or delete.

⚪ **`windows.py:_run_cmd` Linux branch doesn't catch `asyncio.TimeoutError`** —
`windows.py:198-210`. macOS branch (`_run_osascript`) properly kills on
timeout; Linux leaves a zombie.

## 8.6 Relay server (`relay_server/`)

🟡 **Dockerfile CMD hardcodes `--port 3001`** — ignores `$PORT`. Works on
Railway only because `railway.toml startCommand` overrides. Render/Fly
direct Docker deployments will mis-port.

🟡 **`/v1/status` requires auth; Railway healthcheck fails** — see [§8.3].

🟡 **`POST /pair/complete` is unauthenticated** — only the 6-char pair
code protects it. ~10⁹ codes, 5-min TTL, no rate limiting. An attacker
on the public internet who knows the relay URL can brute-force ~10⁵
codes in 5 min. Add a per-IP rate limit (5 req/min/IP) and lockout
after N failures.

🟡 **`POST /result` does not check `complete()` return value before WS
broadcast** — `main.py:202-211`. If task_id is unknown, broadcasts the
old (running) status. Add a guard.

🟡 **`complete()` overwrites without status guard** — `queue.py:201-225`.
No `WHERE status='running'`. Double-post or unknown id silently
overwrites. Return 409 on already-done.

🟡 **CORS empty default blocks all cross-origin silently** —
`main.py:130-138`. Not documented as a required env var.

⚪ **WebSocket broadcast goes to all connected clients** — no per-client
`task_id` filter. Privacy: device A sees device B's results.

⚪ **WebSocket `close()` called before `accept()`** — `main.py:337-344`.
ASGI behavior here is undefined; the 4001 code may be dropped.

⚪ **Lexicographic timestamp comparison in `_iso` vs SQLite default
`strftime`** — works today because both use millisecond ISO + "Z", but
fragile.

⚪ **`_last_pc_seen.strftime` uses naive Z suffix** — `main.py:255`.
Container is UTC so fine, but non-UTC hosts would lie.

⚪ **`BEGIN IMMEDIATE` inside aiosqlite single-conn** — fragile if any
future `asyncio.gather` hits the queue concurrently.

⚪ **`list_devices`/`revoke_device` implemented in queue but no HTTP
endpoint** — dead until exposed.

## 8.7 Tray, extension, kimctl, tests

🔴 **Extension is fully disconnected from the Tauri app** — content scripts
POST to `localhost:3000/sync` (no such server in repo). The desktop app
runs at `:18991` with totally different routes. Either restore the
adapter or remove the extension from the repo.

🟡 **`tray/voice.py:982` `switch_engine` calls `_build_provider(cfg["voice"])`
without `config_dict`** — Hume can't hot-read voice profile changes from
the UI; will always use init-time voice name.

🟡 **Hume not added to fallback chain** — `voice.py:794-816`. If primary
fails and Hume isn't primary, Hume is excluded. Same for inverse.

🟡 **`tray/voice.py:884 `active_provider` property** reads `self._provider`
without holding the swap lock. AttributeError window during hot-swap.

⚪ **`content_deepseek.js` send-button selector takes last
`div[role='button']`** on the page — fragile, modal buttons could win.

⚪ **`overlay.js` sends full `data:…;base64,…` data URL** as `content` —
bridge must strip prefix; not stripped today (and there's no bridge).

⚪ **`kim_test_suite.py:safety_batch_blocks_write`** asserts on a `batch`
tool that doesn't exist in `mcp_server/`. Always fails on that assertion.

⚪ **`claw_test_suite.py` requires `claw` binary that's not built** —
all bridge tests fail with FileNotFoundError.

⚪ **`google.generativeai` is deprecated** — `providers/gemini.py` still
imports it. Will emit FutureWarning everywhere; will break when the pkg
is removed in favor of `google.genai`.

⚪ **`kimctl/__main__.py:496 cmd_browser` uses `hasattr(args, "json")`**
while other commands use `args.json` — inconsistent.

## 8.8 Additional bugs found in the final sweep

⚪ **`relay_worker.py:99-103` — `asyncio.CancelledError` re-raised after the
exception path returns a dict** — the structure `except CancelledError: raise`
+ `except Exception: return {...}` is correct, BUT the `finally`-block lock
release runs even when CancelledError propagates. The lock release uses
`try/except RuntimeError: pass` — and `asyncio.Lock.release()` raises
`RuntimeError("Lock is not acquired")` if called twice, so this guard works.
But the comment "Lock might already be released by `acquire()` raising"
describes a scenario that can't happen: `await acquire()` is outside the
try block, so if it raises, the finally never runs. The guard is defensive
but the comment is wrong.

⚪ **`lib.rs:relay_pair_init:7979` sends `body("{}")` with
`Content-Type: application/json`** — the relay's `POST /pair/init` takes no
body. Harmless today but if FastAPI is ever switched to strict body-parse
mode this would fail.

⚪ **`lib.rs:relay_pair_status:8034` collapses HTTP 404 into `expired=true`**
— intentional ("UI gives up cleanly") but it means a misconfigured relay
URL (returning 404 for every path) is indistinguishable from an actually
expired code. Add a small status-debounce that distinguishes "code expired"
from "endpoint missing".

⚪ **`PairingModal.tsx:108-110` swallows transient poll errors silently** —
network blips during `relay_pair_status` polling are caught with empty
`catch {}`. Good for transient-error suppression, but a persistent
misconfigure (wrong URL, wrong key) will show "Generating pair code…"
forever with no signal to the user. Add an error-count threshold that
escalates to `phase = error` after N consecutive failures.

⚪ **Bridge token leakage via `Content-Security-Policy`** —
`tauri.conf.json` CSP includes `connect-src 'self' ipc: http://ipc.localhost`.
The in-app bridge runs on `127.0.0.1:<random>` — outside `self`. The bridge
calls work today because Tauri's webview makes them via the IPC layer, but
any direct `fetch(<bridge-url>)` from frontend JS would be blocked by CSP.
Verify ChatView never does this directly; it should always go through
`invoke()`.

---

## End

Maintainer note: when adding a new MCP tool, update three places:
1. The tool handler in `mcp_server/tools/<file>.py`.
2. `_TOOLS` and `_DISPATCH` in `mcp_server/server.py`.
3. Section 2.3 of this file.

When adding a new Tauri command, update:
1. The handler function in `lib.rs` (or `google_oauth.rs`).
2. `tauri::generate_handler![…]` block at the bottom of `lib.rs`.
3. The Tauri capabilities file if it needs new permissions.
4. Section 2.1 of this file.
