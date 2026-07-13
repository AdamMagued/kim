> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Kim Desktop — Complete Project Knowledge Base

> **Purpose**: Self-contained reference for AI agents. Read this file to instantly understand the architecture, every file's role, data flows, and common debugging patterns.

---

## 1. HIGH-LEVEL ARCHITECTURE

Kim is a **desktop AI agent** that controls a user's computer. It has 4 layers:

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1: Desktop UI  (React + Tauri)                   │
│  React frontend → Tauri IPC → Rust backend              │
├─────────────────────────────────────────────────────────┤
│  LAYER 2: Rust Backend  (lib.rs)                        │
│  HTTP bridge server, session I/O, agent subprocess mgmt │
├─────────────────────────────────────────────────────────┤
│  LAYER 3: Python Orchestrator  (agent.py)               │
│  Vision-tool loop: screenshot → LLM → tool → repeat     │
├─────────────────────────────────────────────────────────┤
│  LAYER 4: MCP Server  (mcp_server/)                     │
│  OS tools: mouse, keyboard, files, shell, git, screen   │
└─────────────────────────────────────────────────────────┘
```

### Core Data Flow (Task Execution)

1. User types task in **ChatView.tsx** → calls `invoke('send_task', {task})`
2. **lib.rs** `send_task` spawns Python subprocess: `python -m orchestrator.agent --task "…"`
3. **agent.py** `KimAgent.run()` enters vision-tool loop:
   - Takes screenshot via MCP → sends to LLM provider → parses response
   - If tool_call → executes via MCP → feeds result back → loops
   - If TASK_COMPLETE → returns summary
4. Agent stdout is streamed back to lib.rs → emitted as Tauri events → rendered in ChatView

### Browser Provider Flow (No API Key Mode)

Instead of calling an API, Kim can use a **browser-based LLM** (Claude.ai, ChatGPT, Gemini):

1. **lib.rs** starts an HTTP bridge server on `127.0.0.1:<random_port>`
2. Agent's `BrowserProvider` POSTs prompts to the bridge
3. Bridge injects JS into a hidden Tauri WebviewWindow → pastes prompt → clicks Send
4. JS scrapes the LLM response → POSTs back to bridge → agent receives it

---

## 2. FILE-BY-FILE REFERENCE

### 2.1 Root Config Files

| File | Purpose |
|------|---------|
| `config.yaml` | Runtime config: provider name, max_iterations, browser_provider settings, relay config |
| `.env` | API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) |
| `install.sh` | One-command installer: creates venv, installs deps, writes `~/.kim_root` |
| `requirements.txt` | Python dependencies |
| `KIM.md` | Project-level instructions injected into agent system prompt (like CLAUDE.md) |

---

### 2.2 Desktop Frontend — `desktop/src/`

#### `desktop/src/App.tsx` (~350 lines)
**Role**: Root React component. Orchestrates all UI state and routing.

**Key state**:
- `view`: `'chat' | 'history' | 'settings'` — controls which panel is shown
- `messages`: `ChatMessage[]` — live chat messages
- `agentStatus`: `'idle' | 'running' | 'error'` — agent lifecycle
- `selectedProvider`: which browser AI is active

**Key behaviors**:
- Listens to Tauri event `agent-output` for streaming agent stdout
- Parses `[STATUS]`, `[TOOL]`, `[DIFF]`, `[STATS]` prefixed lines from agent stdout
- Calls `invoke('send_task')` to start tasks, `invoke('cancel_task')` to stop
- Manages sidebar navigation between Chat, History, Settings

**Debugging**: If messages aren't appearing → check the `agent-output` event listener. If tasks won't start → check `send_task` invoke and agentStatus state.

---

#### `desktop/src/components/ChatView.tsx` (~700 lines)
**Role**: The main chat interface. Renders messages, input bar, status indicators.

**Key features**:
- Message rendering with markdown support (user messages, assistant responses, tool calls)
- Auto-scroll to bottom on new messages
- Screenshot flash animation (`SCREENSHOT_FLASH` event triggers aura effect)
- File attachment support via drag-and-drop
- Status bar showing real-time agent activity ("Sending to Gemini…", "Running click…")
- Export chat as text/JSON

**Key CSS classes**: `.kim-chat`, `.kim-message`, `.kim-input-bar`, `.kim-status-pill`

**Debugging**: If chat feels frozen → check agentStatus isn't stuck on 'running'. If auto-scroll breaks → check the `useEffect` that watches `messages.length`.

---

#### `desktop/src/components/SettingsPanel.tsx` (~500 lines)
**Role**: Settings UI with tabs for General, Browser AI, Account, About.

**Key sections**:
- **General**: Theme toggle (dark/light/system), session directories
- **Browser AI**: Provider picker, Gemini account selector
- **Account**: Kim account login/signup fields
- **About**: Version info, links

**Tauri commands used**: `invoke('save_settings')`, `invoke('load_settings')`, `invoke('set_preferred_provider')`

**Debugging**: If settings don't persist → check `save_settings` in lib.rs and the JSON file at `~/.kim/settings.json`.

---

#### `desktop/src/components/OnboardingFlow.tsx` (~400 lines)
**Role**: First-run wizard. Guides user through provider selection and sign-in.

**Steps**: Welcome → Choose Provider → Sign In → Ready

**Key behavior**: After provider selection, calls `invoke('open_browser_signin_window')` to open the in-app browser for authentication.

---

#### `desktop/src/components/BrowserProviderPicker.tsx` (~213 lines)
**Role**: Grid of AI provider cards (Claude, ChatGPT, Gemini, Grok, DeepSeek, Custom).

**Key behavior**: Selecting a provider calls `onSelect(providerId)`. "Open in Kim" button calls `invoke('open_browser_signin_window', {url, providerName})`.

**Debugging**: If provider won't open → check `open_browser_signin_window` in lib.rs.

---

#### `desktop/src/components/KimLogo.tsx`
**Role**: Animated SVG brand logo. Pure React component, no external dependencies.

#### `desktop/src/components/Bloop.tsx` (~179 lines)
**Role**: Animated mascot character with 6 states: idle, thinking, processing, success, error, waiting. Pure CSS animations + SVG.

#### `desktop/src/components/Toast.tsx`
**Role**: Toast notification system. Exported `toast(message, type, duration)` function.

---

### 2.3 Frontend Hooks — `desktop/src/hooks/`

#### `useTheme.ts` (49 lines)
**Role**: Theme management. Reads from `localStorage('kim-theme')`, applies `dark` class to `<html>`.
- Supports 3 modes: `'dark' | 'light' | 'system'`
- Listens to OS `prefers-color-scheme` changes when set to 'system'
- Returns `{ theme, resolvedTheme, setTheme }`

#### `useSessions.ts` (42 lines)
**Role**: Loads session history via `invoke('list_sessions', {kimDir, clawDir})`.
- Returns `{ kimSessions, clawSessions, loading, error, refresh }`
- Filters by `session_type === 'kim'` vs `'claw'`

#### `useAccount.ts` (34 lines)
**Role**: Kim account persistence via `invoke('load_account')` / `invoke('save_account')`.
- Returns `{ account, loading, setAccount, clearAccount }`

---

### 2.4 Frontend Types — `desktop/src/types/index.ts`

**Key types**:
```typescript
type Theme = 'dark' | 'light' | 'system';
interface Settings { kim_sessions_dir, claw_sessions_dir, ... }
interface SessionInfo { session_id, title, date, message_count, session_type }
interface KimAccount { email, google_accounts, google_active_account }
interface ChatMessage { id, role, content, timestamp, type?, tool_name?, status? }
```

---

### 2.5 Frontend Styling — `desktop/src/index.css`

**Role**: Complete design system. All CSS variables, component styles, animations.
- Dark/light mode via `.dark` class on `<html>`
- CSS custom properties: `--bg-primary`, `--text-primary`, `--accent`, etc.
- Glassmorphism effects, smooth transitions, responsive layouts
- Screenshot flash overlay animation

---

### 2.6 Tauri Rust Backend — `desktop/src-tauri/src/lib.rs` (~6345 lines)

This is the **largest and most critical file**. It contains:

#### Tauri Commands (invokable from React via `invoke()`)

| Command | Purpose |
|---------|---------|
| `send_task` | Spawns Python agent subprocess, streams stdout via events |
| `cancel_task` | Kills running agent subprocess |
| `list_sessions` | Reads JSONL session files from disk |
| `get_session_messages` | Parses a specific session's JSONL |
| `delete_session` | Removes session files |
| `load_settings` / `save_settings` | JSON settings persistence |
| `load_account` / `save_account` | Account data persistence |
| `open_browser_signin_window` | Creates hidden WebviewWindow for AI provider auth |
| `show_browser_window` / `hide_browser_window` | Toggle browser webview visibility |
| `set_preferred_provider` | Updates KIM_PREFERRED_SITE static |
| `detect_google_accounts` | Scrapes Google account info from webview cookies |

#### HTTP Bridge Server (for BrowserProvider)

Started on app launch. Runs on `127.0.0.1:<random_port>` with a token for auth.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/send` | POST | Accept prompt from agent, inject into browser webview, return req_id |
| `/v1/result/{req_id}` | GET | Long-poll for LLM response (blocks until done or timeout) |
| `/v1/complete` | POST | Legacy monolithic send+wait (fallback) |
| `/v1/task` | POST | Start a new agent task (from external callers) |
| `/v1/cancel` | POST | Cancel running task |
| `/v1/provider` | POST | Switch active browser provider |
| `/v1/health` | GET | Health check |

#### Persistent JS Bridge (`PERSISTENT_BRIDGE_JS`)

~800 lines of JavaScript injected into the browser WebviewWindow via `initialization_script`. This JS:
- Detects which AI site is loaded (claude.ai, chatgpt.com, gemini.google.com, etc.)
- Finds the input editor, send button, stop button, response containers per site
- On `window.__kimBridge.send(prompt, reqId, site, attachments)`:
  - Clears the editor
  - Pastes the prompt text
  - Handles image attachments (clipboard paste for Gemini, file upload for others)
  - Clicks Send
  - Polls for response completion (streaming stop, no more mutations)
  - Scrapes the final response text
  - Emits result back to Rust via Tauri IPC

**Debugging the bridge**: Enable `WEBVIEW_KEEP_VISIBLE` to see the browser window. Check `bridge_debug.log` in sessions dir. Common issues: site selectors changed, popup blocking send button, auth expired.

#### Key Static Globals

| Static | Purpose |
|--------|---------|
| `WEBVIEW_BRIDGE_CFG` | Bridge URL + auth token |
| `WEBVIEW_BRIDGE_RESULTS` | HashMap of req_id → response |
| `WEBVIEW_BRIDGE_NOTIFY` | Condvar for result notification |
| `BRIDGE_TASK_PID` | PID of running agent subprocess |
| `KIM_PREFERRED_SITE` | Currently selected AI provider |
| `WEBVIEW_LAST_GEMINI_AUTHUSER` | Last Gemini authuser index |

#### Helper Functions

| Function | Purpose |
|----------|---------|
| `default_project_root()` | Resolves Kim project root (compile-time baked → ~/.kim_root → env → exe ancestor → ~/.kim) |
| `find_python_interpreter()` | Finds Python: venv/bin/python → python3 → python |
| `read_sessions_from_dir()` | Walks date dirs, parses JSONL files into SessionInfo |
| `parse_jsonl()` | Reads a JSONL file into KimMessage vec |
| `normalize_site()` | Maps aliases ("gpt" → "chatgpt", "google" → "gemini") |
| `prepare_gemini_webview()` | Navigates browser to correct Gemini authuser URL |
| `write_first_png_to_clipboard()` | macOS: writes PNG to system clipboard via osascript |

---

### 2.7 Tauri Config — `desktop/src-tauri/tauri.conf.json`

```json
{
  "productName": "Kim",
  "version": "0.9.6",
  "identifier": "com.kim.desktop",
  "app": {
    "windows": [{ "titleBarStyle": "Overlay", "transparent": true }],
    "macOSPrivateApi": true
  }
}
```

**Dev server**: `http://localhost:1420` (Vite)

---

### 2.8 Python Orchestrator — `orchestrator/`

#### `orchestrator/agent.py` (~1077 lines)
**Role**: The brain. Runs the vision-tool agent loop.

**Class `KimAgent`**:
- `run(task)` → main loop (up to `max_iterations`, default 25):
  1. Call LLM via provider.complete(messages, tools, system)
  2. If response is `tool_call` → execute via MCP → add result to memory → loop
  3. If response is `text` with `TASK_COMPLETE:` → return success
  4. If response is `text` with `NEED_HELP:` → return failure
  5. If 3 identical screenshots → stuck detection → stop
  6. If 3 text responses without tool calls → conversational loop → stop

**Key methods**:
- `_call_with_retry()` — exponential backoff for LLM errors (429, 5xx)
- `_execute_tool()` — calls MCP, handles screenshot flash (hide window → capture → show)
- `_build_system_prompt()` — constructs system prompt with OS info, tool list, KIM.md instructions
- `_is_stuck()` — MD5 of last 3 screenshots, returns True if all identical

**Class `UIBridge`**:
- Thread-safe channel between async agent and UI
- `log_queue` → UI log messages
- `_confirm_queue` → preview mode confirmations
- `_visibility_queue` → hide/show for screenshot blink
- `cancel()` / `reset()` → task lifecycle

**`mcp_session_context(config)`**: Async context manager that starts MCP server subprocess and creates ClientSession.

**Debugging**: Agent logs go to stderr AND UIBridge queue. Check `[STATUS]`, `[TOOL]`, `[DIFF]`, `[STATS]` prefixed lines. If agent hangs → check MCP server subprocess, LLM provider timeout.

---

#### `orchestrator/providers/base.py` (80 lines)
**Role**: Abstract provider interface + factory.

**`BaseProvider.complete(messages, tools, system) → dict`**: Returns `{"type": "tool_call", "tool": str, "args": dict}` or `{"type": "text", "content": str}`.

**`create_provider(name, config)`**: Factory. Supports: `claude`, `openai`, `gemini`, `deepseek`, `browser`, `browser:claude`, `browser:chatgpt`, etc.

---

#### `orchestrator/providers/browser_provider.py` (~1695 lines)
**Role**: API-key-free LLM access. Two modes:

**Mode 1 — In-App WebView Bridge** (default for desktop):
- POSTs to lib.rs HTTP bridge → bridge injects into Tauri WebviewWindow
- Split API: `POST /v1/send` (instant) → `GET /v1/result/{req_id}` (long-poll)
- Fallback: `POST /v1/complete` (monolithic, for older binaries)

**Mode 2 — Direct CDP** (legacy/tray mode):
- Connects to Chrome via Playwright CDP on port 9222
- Finds the active AI chat tab
- Injects prompt via clipboard paste (Cmd/Ctrl+V)
- Uploads screenshots via file chooser or clipboard
- Scrapes response text from DOM

**Key methods**:
- `complete()` → routes to bridge or CDP
- `_format_prompt()` → builds text prompt with system prompt + tool list + history
- `_parse_response()` → extracts JSON tool_call from LLM text, or returns as text
- `_send_and_wait()` → CDP: paste + click Send + poll for response
- `_complete_via_webview_bridge()` → bridge: POST send → GET result

**Per-site selectors** (`SITE_CONFIGS`): Each AI site has CSS selectors for input, send button, stop button, response container, upload button.

**Debugging**: If LLM never responds → check bridge connection (env vars `KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`). If response parsing fails → check `_parse_response()` regex for tool_call JSON extraction.

---

#### `orchestrator/memory.py` (148 lines)
**Role**: Sliding-window conversation history.

- `max_messages=40` — hard cap, oldest dropped first
- `keep_screenshots=4` — only last 4 user messages keep their base64 images
- Older screenshots replaced with "(screenshot removed)" text
- `get_messages()` returns deep copy safe to modify

**Debugging**: If agent loses context → check max_messages. If token count explodes → check keep_screenshots.

---

#### `orchestrator/session_store.py` (270 lines)
**Role**: JSONL session persistence.

**File layout**:
```
kim_sessions/
  2026-05-08/
    abc123.jsonl          ← incremental messages
    abc123.summary.txt    ← AI-generated 1-paragraph summary
```

**Key methods**:
- `append_message(msg)` — appends one JSONL line (strips base64 images)
- `save_summary(text)` — writes .summary.txt
- `load_session(id)` — finds and reads JSONL across all date dirs
- `recent_summaries(n)` — last N summaries for context
- `list_sessions()` — all sessions with metadata

---

#### `orchestrator/context_loader.py` (152 lines)
**Role**: Discovers `KIM.md` / `KIM.local.md` instruction files.

- Walks from CWD upward to filesystem root
- Deduplicates by content hash
- Truncates to 4000 chars/file, 12000 chars total
- Returns formatted prompt section for system prompt

---

#### `orchestrator/task_queue.py` (139 lines)
**Role**: **DORMANT** — future relay-server task queue. Not currently wired into desktop app.

- Local asyncio queue + optional remote relay poller (GET /prompt/next)
- Designed for phone-to-desktop remote control
- Do not delete — will be integrated later

---

### 2.9 MCP Server — `mcp_server/`

#### `mcp_server/server.py` (602 lines)
**Role**: Exposes 27 OS control tools over MCP stdio transport.

**Tool categories**:

| Category | Tools |
|----------|-------|
| **Files** | `read_file`, `write_file`, `list_dir`, `delete_file` |
| **Shell** | `run_command`, `run_powershell` |
| **Screen** | `take_screenshot`, `take_annotated_screenshot`, `get_screen_info` |
| **Mouse** | `click`, `double_click`, `right_click`, `drag`, `scroll` |
| **Keyboard** | `type_text`, `hotkey`, `key_press` |
| **Windows** | `get_windows`, `focus_window`, `resize_window`, `open_url` |
| **Git** | `git_status`, `git_diff`, `git_add`, `git_commit`, `git_log`, `git_checkout` |
| **Code** | `run_python`, `run_node`, `lint_file` |
| **Search** | `search_in_files`, `find_files` |

**Important**: stdout is reserved for MCP protocol. All `print()` is redirected to stderr.

**Tool handler files** (in `mcp_server/tools/`):
- `files.py` — file I/O with PROJECT_ROOT resolution
- `screen.py` — screenshot capture, annotated screenshot with grid overlay
- `mouse.py` — pyautogui mouse control
- `keyboard.py` — pyautogui keyboard control
- `shell.py` — subprocess execution with timeout
- `windows.py` — window management (platform-specific)
- `git.py` — git operations via subprocess
- `code.py` — Python/Node execution, linting
- `search.py` — ripgrep/grep file search

#### `mcp_server/config.py`
**Role**: Reads PROJECT_ROOT from env, sets LOG_LEVEL. Used by all tool handlers.

---

### 2.10 Tray App — `tray/` (Legacy/Alternative UI)

#### `tray/app.py` (522 lines)
**Role**: System tray application using pystray + Tkinter. Alternative to the Tauri desktop app.

**Architecture**: 3 threads:
1. Main → Tkinter event loop
2. Daemon → pystray icon
3. Daemon → asyncio event loop (agent tasks)

**Key features**:
- System tray icon with colour states (idle=blue, running=green, error=red)
- Hotkey Ctrl+Alt+J to prompt for task
- Provider selection menu
- Control panel with live log viewer
- Voice engine integration

#### `tray/voice.py`
**Role**: Text-to-speech via Maya-1 or system TTS. Fire-and-forget audio playback.

---

## 3. DEBUGGING QUICK REFERENCE

### "Task won't start"
1. Check `agentStatus` in App.tsx — must be `'idle'`
2. Check `send_task` in lib.rs — does `find_python_interpreter()` find Python?
3. Check `default_project_root()` — does it resolve to the right directory?
4. Check MCP server starts: `python -m mcp_server.server` should not crash

### "Agent runs but LLM never responds"
1. **API mode**: Check `.env` for API keys, check provider in config.yaml
2. **Browser mode**: Check bridge env vars (`KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`)
3. Check browser webview is logged in (open it with `show_browser_window`)
4. Check `bridge_debug.log` in sessions dir

### "Browser provider gets empty response"
1. Site CSS selectors may have changed — check `SITE_CONFIGS` in browser_provider.py and `PERSISTENT_BRIDGE_JS` in lib.rs
2. Popups blocking send button — check `_dismiss_popups()` / popup dismiss logic in bridge JS
3. Auth expired — user needs to re-sign in via the provider picker

### "Sessions not loading"
1. Check `kim_sessions_dir` in settings
2. Check `read_sessions_from_dir()` in lib.rs — walks `<base>/YYYY-MM-DD/*.jsonl`
3. Check `validate_session_id()` — only `[A-Za-z0-9._-]` allowed

### "Theme not applying"
1. Check `useTheme.ts` — reads `localStorage('kim-theme')`
2. Check `index.css` — `.dark` class on `<html>` toggles CSS variables
3. Check `applyTheme()` — adds/removes `dark` class

### "Screenshot flash not working"
1. Agent prints `[UI] SCREENSHOT_FLASH` to stdout
2. App.tsx parses this and triggers flash animation in ChatView
3. `_execute_tool()` in agent.py calls `hide_for_screenshot()` / `show_after_screenshot()`

---

## 4. KEY ENVIRONMENT VARIABLES

| Variable | Purpose |
|----------|---------|
| `KIM_PROJECT_ROOT` | Override project root directory |
| `KIM_SESSIONS_DIR` | Override sessions directory |
| `KIM_WEBVIEW_BRIDGE_URL` | HTTP bridge URL (set by lib.rs for agent subprocess) |
| `KIM_WEBVIEW_BRIDGE_TOKEN` | Auth token for bridge |
| `KIM_PREFERRED_SITE` | Override preferred browser AI |
| `PROJECT_ROOT` | MCP server working directory |
| `ANTHROPIC_API_KEY` | For Claude API provider |
| `OPENAI_API_KEY` | For OpenAI API provider |
| `GOOGLE_API_KEY` | For Gemini API provider |
| `DEEPSEEK_API_KEY` | For DeepSeek API provider |

---
w
## 5. SESSION DATA FORMAT

**JSONL messages** (one per line):
```json
{"role":"user","content":"Task: open Chrome"}
{"role":"assistant","content":"{\"type\":\"tool_call\",\"tool\":\"run_command\",\"args\":{\"cmd\":\"open -a 'Google Chrome'\"}}"}
{"role":"user","content":"[Tool result: run_command]\nSuccess"}
```

**Multimodal content** (screenshots stripped on disk):
```json
{"role":"user","content":[{"type":"text","text":"[Tool result: take_screenshot]\nScreenshot captured."},{"type":"text","text":"(screenshot — stripped for disk)"}]}
```

---

## 6. BUILD & RUN

```bash
# Dev mode
cd desktop && npm run tauri dev

# Production build
cd desktop && npm run tauri build

# Run Python agent standalone
python -m orchestrator.agent --task "open Notepad" --provider browser

# Run MCP server standalone
python -m mcp_server.server

# Run tray app (legacy)
python -m tray.app
```
