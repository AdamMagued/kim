# KIM — Product Requirements Document
### AI Agent Platform | v1.0 | April 2026 | Build with Claude Opus

---

## Table of Contents
1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Product Vision](#3-product-vision)
4. [System Architecture](#4-system-architecture)
5. [Full Tech Stack](#5-full-tech-stack)
6. [Build Phases](#6-build-phases)
7. [iOS App Readiness](#7-ios-app-readiness)
8. [Milestones & Timeline](#8-milestones--timeline)
9. [Security Model](#9-security-model)
10. [Failure Modes & Mitigations](#10-failure-modes--mitigations)
11. [Success Metrics](#11-success-metrics)
12. [Open Questions](#12-open-questions)
13. [Instructions for Building with Claude Opus](#13-instructions-for-building-with-claude-opus)
14. [Appendix — Key Design Decisions](#14-appendix--key-design-decisions)

---

## 1. Executive Summary

**KIM** is a local AI agent platform that connects any cloud LLM (Claude, GPT-4o, Gemini, DeepSeek) directly to your Windows PC — giving it full control over your screen, browser, file system, and OS. It is the personal equivalent of Claude Code + Claude Computer Use + a custom browser extension, all wired together into a single always-on agent.

The system is built around the **Model Context Protocol (MCP)**, which means any MCP-compatible client can talk to it. It includes a multi-LLM orchestrator, a browser extension that works across all major AI chat interfaces, a real-time execution loop with self-healing error correction (inherited from the existing Bridge codebase), full OS and mouse/keyboard control, and a system tray UI for frictionless management.

A **remote prompt relay layer** is designed into the architecture from day one, enabling future iOS and Android apps to send natural-language commands to the PC over the internet — the phone sends a message, the PC executes it autonomously.

| Dimension | Capability |
|-----------|------------|
| LLM Brain | Swappable — Claude Opus, GPT-4o, Gemini 2.0, DeepSeek |
| OS Control | Full mouse, keyboard, window, process management |
| Browser Control | Playwright + extension across all LLM chat UIs |
| File / Code | Read, write, execute, diff — equivalent to Claude Code |
| Self-Healing Loop | Error captured → fed to LLM → fix generated → re-run |
| Remote Prompts | Phone → relay server → PC agent (iOS app later) |
| MCP Compatible | Any MCP client (Claude Desktop, Claude Code, Cursor) |

---

## 2. Problem Statement

### 2.1 What exists today

Claude Computer Use, Claude Code, and Claude Dispatch are powerful but exist in silos controlled by Anthropic. They are Claude-only, require specific subscription tiers, and cannot be extended or customized at the protocol level. You cannot swap in GPT-4o as the brain, add your own tools, or control the execution loop.

The user has already built a functional version of the core idea — a Chrome extension that scrapes Gemini's DOM, a Flask server that writes files and runs commands, and a tkinter overlay for triggering syncs. This proves the concept works. What is missing:

- Multi-LLM support (currently Gemini-only via DOM scraping)
- Screen vision — the LLM never sees the actual screen
- OS-level control — clicks, keyboard, window management
- MCP compatibility — the server speaks a custom protocol, not MCP
- Remote prompt capability — no way to send commands from a phone
- Production robustness — no session management, no sandboxing, no permission model

### 2.2 What Kim solves

- Replaces DOM scraping with direct API calls to any cloud LLM
- Adds full screen vision via screenshot-to-base64 pipeline
- Adds OS control via MCP tools wrapping pyautogui and Windows APIs
- Wraps everything in the MCP protocol for universal client compatibility
- Adds a relay server so a phone can send prompts to the PC
- Preserves the auto-loop error correction that already works

---

## 3. Product Vision

Kim is your personal AI that lives on your PC, connected to the internet, that you can command from anywhere. You pick up your phone, type *"book me a flight to Dubai for next Friday, cheapest option"*, and your PC opens Chrome, navigates to Google Flights, fills in the details, and completes the booking — sending you a confirmation when it is done.

That is the north star. Everything in this PRD is a step toward that experience.

| Principle | What it means in practice |
|-----------|--------------------------|
| LLM-agnostic | Claude, GPT-4o, Gemini, DeepSeek are all first-class. Provider is a config line. |
| MCP-first | All capabilities exposed as MCP tools. Any MCP client works. |
| Self-healing | Errors feed back to the LLM automatically. Loops until success or human needed. |
| Remote-ready | Architecture supports phone-to-PC from day one, even if the iOS app is built later. |
| Transparent | Every action is logged. You can inspect, pause, or roll back at any time. |
| Extensible | New MCP tools can be added without touching the orchestrator or UI. |

---

## 4. System Architecture

### 4.1 Component map

Kim has six components. Each is independently deployable and communicates over well-defined interfaces.

| Component | Role | Language / Stack |
|-----------|------|-----------------|
| MCP Server | Exposes all OS, file, browser, screen tools | Python, mcp SDK |
| Multi-LLM Orchestrator | The agent brain — calls LLM, executes tools, loops | Python, asyncio |
| Browser Extension | Works inside all AI chat UIs, syncs files/cmds | JS, Manifest V3 |
| Relay Server | Receives phone prompts, queues them for the PC | Python, FastAPI, WebSocket |
| Tray App + UI | Start/stop, log view, provider switch, task input | Python, pystray + tkinter |
| Windows-MCP Bridge | Low-level Windows API hooks for native UI control | Python, windows-mcp |

### 4.2 Data flow — browser extension path

```
1. User types a task or LLM generates a response in the browser
2. Extension detects completion (send button re-enables) and extracts text
3. Extension POSTs to MCP server /sync with parsed ## FILE: and ## CMD: blocks
4. MCP server writes files, executes commands, captures stdout/stderr
5. If error detected → server responds with has_error: true + log
6. Extension injects error back into chat UI and triggers LLM re-generation
7. Loop continues until success or max retries reached
```

### 4.3 Data flow — autonomous agent path

```
1. User types task in Tray UI or sends via phone relay
2. Orchestrator receives task, builds system prompt with tool definitions
3. Orchestrator calls cloud LLM API with screenshot + task + history
4. LLM returns a tool call: {"tool": "click", "args": {"x": 340, "y": 220}}
5. Orchestrator forwards tool call to MCP server
6. MCP server executes action, returns result
7. New screenshot taken, appended to conversation history
8. Loop repeats until LLM returns TASK_COMPLETE or error threshold hit
```

### 4.4 Remote prompt flow (phone-to-PC)

```
1. User opens iOS app (future) or sends HTTP POST to relay server
2. Relay server stores prompt in queue (SQLite or Redis)
3. PC orchestrator polls relay server every 2 seconds
4. Prompt dequeued, injected into autonomous agent loop as task
5. Agent executes task on PC
6. Result (screenshot + summary) POSTed back to relay server
7. iOS app receives push notification with result
```

> **Key design choice:** The relay server is a thin message bus with no LLM and no agent logic. The iOS app only needs the relay URL and an API key — it never connects directly to the PC. The PC polls outbound, so no firewall changes or port forwarding are needed.

---

## 5. Full Tech Stack

### 5.1 Core Python dependencies

```bash
pip install mcp                    # MCP server SDK
pip install anthropic              # Claude API
pip install openai                 # GPT-4o API
pip install google-generativeai    # Gemini API
pip install pyautogui              # Mouse, keyboard automation
pip install mss                    # Fast screenshots
pip install Pillow                 # Image processing
pip install playwright             # Browser automation
pip install fastapi uvicorn        # Relay server
pip install pystray                # System tray
pip install pygetwindow            # Window management
pip install pywin32                # Windows API bindings
pip install python-dotenv          # .env files
pip install aiofiles               # Async file I/O
pip install httpx                  # Async HTTP client
pip install redis                  # Optional: relay queue
pip install keyboard               # Global hotkeys
pip install win10toast             # Windows notifications
playwright install chromium        # Browser binaries
uvx windows-mcp                    # Windows-native UI control
```

### 5.2 Browser extension

- Manifest V3, zero external dependencies
- One content script per LLM site with site-specific DOM selectors
- Unified background.js routing all sites to MCP server
- Popup with provider badge, loop toggle, relay status indicator

### 5.3 Directory structure

```
kim/
  mcp_server/
    server.py              ← Main MCP server (stdio transport)
    tools/
      files.py             ← read_file, write_file, list_dir, delete_file
      shell.py             ← run_command, run_powershell
      screen.py            ← take_screenshot, get_screen_info
      mouse.py             ← click, double_click, right_click, drag, scroll
      keyboard.py          ← type_text, hotkey, key_press
      windows.py           ← get_windows, focus_window, resize_window
      browser.py           ← open_url, browser_click, fill_form, get_page_text
      git.py               ← git_status, git_diff, git_commit, git_log
      code.py              ← run_python, run_node, run_tests, lint_file
      search.py            ← search_in_files, find_files
    config.py
  orchestrator/
    agent.py               ← Main async agent loop
    providers/
      base.py              ← Abstract provider interface
      claude.py
      openai_provider.py
      gemini.py
      deepseek.py
    memory.py
    task_queue.py
  relay_server/
    main.py                ← FastAPI app
    auth.py
    queue.py               ← SQLite queue
    models.py
  extension/
    manifest.json
    background.js
    content_claude.js
    content_chatgpt.js
    content_gemini.js
    content_deepseek.js
    popup.html / popup.js
    overlay.js
  tray/
    app.py                 ← pystray tray
    ui.py                  ← tkinter control panel
    settings.py
  tests/
    test_mcp_tools.py
    test_orchestrator.py
    test_relay.py
  .env.example
  config.yaml
  requirements.txt
  CLAUDE.md
  README.md
```

---

## 6. Build Phases

Build **one phase per Claude Opus session**. Each phase is a fully runnable increment. Test before moving to the next.

---

### Phase 1 — MCP Server (Foundation)

**Goal:** Replace the Flask server with a proper MCP server. When done, Claude Desktop and Claude Code can connect to Kim immediately.

**Deliverables:**

- `mcp_server/server.py` — MCP server with stdio transport
- `mcp_server/tools/` — all tool modules (files, shell, screen, mouse, keyboard, windows)
- `config.yaml` — default config with PROJECT_ROOT, timeouts, allowed paths
- `requirements.txt`
- `.env.example`
- Claude Desktop config JSON snippet

**All MCP tools:**

| Tool | Description | Key args |
|------|-------------|----------|
| `read_file` | Read file as text | path |
| `write_file` | Write/create file with dirs | path, content |
| `list_dir` | List files/folders | path, recursive |
| `delete_file` | Delete a file | path |
| `run_command` | Shell command with stdout/stderr capture | cmd, cwd, timeout |
| `run_powershell` | PowerShell script block | script |
| `take_screenshot` | Full screen as base64 PNG | scale |
| `click` | Click at coordinates | x, y, button, clicks |
| `type_text` | Type string at cursor | text, interval |
| `hotkey` | Key combination | keys |
| `scroll` | Scroll at coordinates | x, y, clicks, direction |
| `get_windows` | List open windows + coords | — |
| `focus_window` | Bring window to front | title |
| `get_screen_info` | Resolution and DPI | — |
| `open_url` | Open URL in default browser | url |

**Test:** Connect Claude Desktop, ask it to write a file to PROJECT_ROOT, confirm file appears.

---

### Phase 2 — Multi-LLM Orchestrator

**Goal:** Autonomous agent loop. Takes a task, sees the screen, calls any LLM, executes tools, loops until done. Provider swappable via one config line.

**Deliverables:**

- `orchestrator/agent.py` — async agent loop with iteration guard and progress detection
- `orchestrator/providers/` — Claude, OpenAI, Gemini, DeepSeek with identical interface
- `orchestrator/memory.py` — sliding window history, screenshot compression
- `orchestrator/task_queue.py` — local queue + relay server poller (2s interval)
- CLI: `python -m orchestrator.agent --task "open Chrome and search for X"`

**Agent loop:**

```
receive task
  → build system prompt (task + tools + safety rules)
  → screenshot → base64
  → call LLM (system_prompt + screenshot + history)
  → if tool_call: execute → append result → screenshot → repeat
  → if TASK_COMPLETE: exit, return summary
  → if NEED_HELP: pause, notify user
  → if iterations > max: stop, ask user
  → if last 3 screenshots identical: abort (stuck detection)
```

**Provider interface:**

All providers normalize to:
- Input: `messages`, `tools`, `system`
- Output: `{"type": "tool_call", "tool": str, "args": dict}` or `{"type": "text", "content": str}`

**Test:** Give agent task "open Notepad and type Hello World". Confirm it does it autonomously.

---

### Phase 3 — Browser Extension v2

**Goal:** Extend existing Gemini bridge to all LLM sites. Add relay status and provider switcher.

**Deliverables:**

- Four content scripts with site-specific selectors
- Updated `background.js` — routes all sites to MCP server `/sync`
- Updated `popup.html/js` — provider badge, relay dot, max-retries slider
- All existing auto-loop logic preserved exactly

**Selector map:**

| Site | Response selector | Input selector | Send button |
|------|------------------|----------------|-------------|
| claude.ai | `[data-testid='conversation-turn']` last | `div[contenteditable='true']` | `button[aria-label*='Send']` |
| chatgpt.com | `div.markdown` last | `div#prompt-textarea` | `button[data-testid='send-button']` |
| gemini.google.com | `model-response` last | `rich-textarea > div[contenteditable]` | `button[aria-label*='Send']` |
| chat.deepseek.com | `div.ds-markdown` last | `textarea` | `div[role='button']` last |

**Test:** Enable auto-loop on ChatGPT. Ask for a Python script with a bug. Confirm loop corrects it.

---

### Phase 4 — Relay Server (Phone-to-PC Bridge)

**Goal:** Thin FastAPI message bus. Phone POSTs task → relay queues it → PC polls → PC executes → result returned to phone.

**Deliverables:**

- `relay_server/main.py` — FastAPI with all endpoints
- `relay_server/auth.py` — separate phone_key and pc_key
- `relay_server/queue.py` — SQLite backend
- `relay_server/models.py` — Pydantic schemas
- `Dockerfile` + `railway.toml` / `render.yaml` for free-tier deployment

**API:**

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/prompt` | POST | phone_key | Submit task → returns task_id |
| `/prompt/next` | GET | pc_key | PC dequeues oldest pending task |
| `/result` | POST | pc_key | PC posts summary + screenshot |
| `/result/{id}` | GET | phone_key | Phone polls for result |
| `/ws` | WS | token in query | Real-time push to phone |
| `/status` | GET | any | PC connected? Queue depth? |

**SQLite schema:**

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending | running | done | failed
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    picked_up_at TIMESTAMP,
    completed_at TIMESTAMP,
    summary TEXT,
    screenshot TEXT,                  -- base64 PNG
    success BOOLEAN
);
```

**Test:** `curl -X POST https://your-relay.railway.app/prompt -H "X-API-Key: phone_key" -d '{"task": "open Notepad"}'` → PC opens Notepad → result returned.

---

### Phase 5 — Tray App + UI Shell

**Goal:** Replace overlay.py with a proper always-on system tray app. No terminal needed.

**Deliverables:**

- `tray/app.py` — pystray icon with right-click menu
- `tray/ui.py` — tkinter control panel (task input, logs, preview, stop button)
- `tray/settings.py` — API keys, project root, relay URL, hotkey config
- Global hotkey: Ctrl+Alt+J → open task input from anywhere
- Windows toast notification on task complete

**Tray menu:**
```
[Kim icon]
  ├── Open Control Panel
  ├── Run Task...
  ├── ─────────────────
  ├── Provider: Claude ▸  (Claude / GPT-4o / Gemini / DeepSeek)
  ├── Agent: Running ▸    (Pause / Resume)
  ├── ─────────────────
  ├── Settings...
  └── Quit
```

**Test:** Launch tray app, use Ctrl+Alt+J, type a task, confirm agent runs and toast appears on completion.

---

### Phase 6 — Claude Code Compatibility

**Goal:** Register Kim as an MCP server for Claude Code CLI. Add code/git tools.

**Additional tools:**

`git.py`: `git_status`, `git_diff`, `git_add`, `git_commit`, `git_log`, `git_checkout`

`code.py`: `run_python`, `run_node`, `run_tests`, `lint_file`, `format_file`

`search.py`: `search_in_files` (ripgrep), `find_files` (glob)

**Registration:**
```bash
claude mcp add Kim -- python -m mcp_server.server
```

**Test:** `claude` CLI in a project dir. Ask Claude to read a file, make a change, run tests. Confirm it uses Kim tools.

---

### Phase 7 — Production Hardening

**Goal:** Daily-driver reliability. Not polish — correctness and safety.

**Deliverables:**

- Path validation — reject writes outside PROJECT_ROOT
- Command blocklist — `rm -rf /`, `format c:`, `del /S /Q C:\` blocked
- Action preview mode — config option to show + wait 3s before executing
- Stuck detection — abort if last 3 screenshots are identical (md5 compare)
- Retry with exponential backoff on LLM API 429/500/503
- Playwright session persistence — cookies saved to `sessions/`
- Structured JSON logging to `logs/kim_{date}.jsonl`
- Full pytest suite — all MCP tools tested with mocks
- `install.bat` — one-command setup: clone → pip install → .env → launch

**Test:** Full test suite passes. Write a file to a path outside PROJECT_ROOT — confirm it's rejected.

---

## 7. iOS App Readiness

The iOS app is built later. The backend is ready for it now — nothing changes when you build the app.

What the iOS app needs to do: send a `POST /prompt` to the relay server with an API key and a task string. That is it.

| Feature | Backend ready? | Notes |
|---------|----------------|-------|
| Send a task | Yes — POST /prompt | Returns task_id immediately |
| See task status | Yes — GET /result/{id} | Poll every 2s or use WebSocket |
| Get screenshot | Yes — in result payload | base64 PNG |
| Auth | Yes — phone_api_key | Phone gets its own key |
| Push notification | Partial — WebSocket ready | APNs integration is iOS-side |
| Task history | Planned — relay DB | SQLite stores all tasks + results |

### Minimal iOS client (future, ~1 week)

- SwiftUI or React Native
- Single screen: text field + send + result view
- Relay URL and phone API key in Keychain
- WebSocket listener for real-time result push
- Show last screenshot from PC as confirmation

Build the phone app after the PC agent is rock-solid.

---

## 8. Milestones & Timeline

Each phase is an independent milestone. Claude Opus writes code; you test and deploy.

| Phase | Milestone | Est. Time | Key test |
|-------|-----------|-----------|----------|
| 1 | MCP Server | 1–2 days | Claude Desktop connects, all tools callable |
| 2 | Orchestrator | 2–3 days | Agent completes multi-step browser task |
| 3 | Extension v2 | 1–2 days | Auto-loop works on Claude, ChatGPT, Gemini |
| 4 | Relay Server | 1–2 days | Postman → relay → PC executes → result returned |
| 5 | Tray App | 1–2 days | Tray live, task input works, logs stream |
| 6 | Claude Code | 1 day | `claude mcp add kim` works, git tools tested |
| 7 | Hardening | 3–5 days | Test suite passes, sandboxing verified |
| iOS App | Phone app | 1–2 weeks | Phone → task → PC executes → screenshot returned |

**Total to production-ready PC agent:** ~10–17 focused days.

---

## 9. Security Model

Kim has root-level access to your PC by design. The security model prevents accidents and unauthorized access — not what the agent can do.

### 9.1 Local

- **PROJECT_ROOT enforcement** — MCP server validates all file paths against allowlist
- **Command blocklist** — destructive commands require explicit confirmation
- **Action preview mode** — optional: show every planned action before execution
- **Max iteration limit** — agent stops and asks if task takes more than N steps
- **Sensitive path protection** — `/Windows/System32`, `NTUSER.dat` blocked by default

### 9.2 Relay server

- Separate API keys for phone and PC — compromise of one doesn't expose both
- All relay traffic is HTTPS — keys encrypted in transit
- Task payload size limit — prevents large prompt injection attacks
- Rate limiting — max N tasks per minute per API key
- Task expiry — tasks not picked up within 5 minutes are discarded

### 9.3 API keys

- All keys in `.env` — never committed to git
- Keys never logged, never sent to relay server
- Separate provider keys — one compromise doesn't affect others

---

## 10. Failure Modes & Mitigations

| Failure | Impact | Mitigation |
|---------|--------|------------|
| LLM API rate limit hit | Agent pauses mid-task | Exponential backoff, fallback provider |
| Screenshot too large for context | LLM refuses input | Scale to 0.5, compress with Pillow |
| Agent clicks wrong element | Task goes sideways | Action preview mode, max iterations guard |
| Relay server down | Phone prompts lost | Local queue fallback, retry on reconnect |
| CAPTCHA encountered | Browser task blocked | Agent pauses, notifies user, waits |
| LLM loops without progress | Infinite loop | Hash last 3 screenshots — if identical, abort |
| File write outside PROJECT_ROOT | Security violation | MCP server rejects before write |
| Windows app crashes during control | Lost window state | Recover via get_windows, screenshot to reorient |

---

## 11. Success Metrics

Kim is production-ready when:

- Agent completes a form-filling task end-to-end with no human intervention
- Agent creates an account on a website from a single natural language prompt
- Auto-loop corrects a code error and re-runs in under 30 seconds
- Switching LLM provider requires one line change in `config.yaml`
- Phone POST → relay → PC execution → result back in under 15 seconds
- All MCP tools pass pytest test suite
- Claude Desktop connects with zero extra config beyond the JSON snippet
- Claude Code can use file and git tools via `claude mcp add`

---

## 12. Open Questions

| Question | Options | Decision needed by |
|----------|---------|-------------------|
| Relay server hosting | Railway (free), Render (free), self-host | Phase 4 |
| Queue backend | SQLite (simple) vs Redis (scalable) | Phase 4 |
| Screenshot compression | Scale factor vs JPEG quality vs region-only | Phase 2 |
| Agent memory | Sliding window vs full history vs vector store | Phase 2 |
| iOS app framework | Swift native vs React Native vs Flutter | iOS phase |
| CAPTCHA handling | Manual solve prompt vs 2captcha vs skip | Phase 7 |
| Browser engine | Playwright (recommended) vs Selenium | Phase 1 |

---

## 13. Instructions for Building with Claude Opus

Claude Opus is the coder. You are the executor and tester.

### 13.1 Prompt template for each phase

Open a fresh Claude.ai session with Opus selected. Paste:

```
You are building Kim — a local AI agent platform for Windows.

Rules:
- Output ALL code as ## FILE: path/to/file.py blocks
- Output ALL shell commands as ## CMD: command
- Every file must be complete — no placeholders, no TODOs
- Include all imports, error handling, and logging
- When done with a file, say DONE: filename
- Ask clarifying questions before writing if spec is ambiguous

[PASTE THE CLAUDE.md CONTENTS HERE]

Phase: [PASTE THE DELIVERABLES FROM SECTION 6 FOR THIS PHASE]
```

### 13.2 Using your existing bridge to sync Opus output

1. Keep your existing `server.py` running during build
2. When Opus outputs `## FILE:` blocks, hit Ctrl+Shift+Y or enable Auto-Loop
3. Bridge writes files and runs `## CMD:` blocks automatically
4. If a command fails, bridge feeds error back to Opus automatically
5. Once Phase 1 (MCP server) is done, switch to the new MCP server

### 13.3 Session discipline

- Read CLAUDE.md before every session — it has full context Opus needs
- Do one phase per session — do not ask Opus to build everything at once
- Test each phase before starting the next
- `git commit` after each working phase — you want rollback points

---

## 14. Appendix — Key Design Decisions

### 14.1 Why MCP instead of a custom protocol

MCP is already supported by Claude Desktop, Claude Code, Cursor, Windsurf, and a growing list of tools. By speaking MCP, Kim gets all of these clients for free. The custom Flask approach in Bridge v3 only works with the custom extension. MCP is the right long-term bet.

### 14.2 Why stdio transport instead of HTTP

Stdio transport means the MCP server is a local subprocess — no port conflicts, no firewall rules, no authentication needed for local connections. Claude Desktop and Claude Code both default to stdio. HTTP/SSE transport is used for the relay server (remote) only.

### 14.3 Why keep the browser extension

Direct API calls are more reliable than DOM scraping, but the extension provides something the API cannot: the ability to have a conversation in a familiar UI (Claude.ai, ChatGPT) while automatically syncing output to your local machine. The extension is the human-friendly interface. The orchestrator is the autonomous interface. Both are needed.

### 14.4 Why SQLite for the relay queue

SQLite requires zero infrastructure. The relay server can run on the cheapest Render/Railway free tier and persist tasks across restarts with no additional services. Redis is faster and scales better but adds complexity and cost. SQLite is the right default — swap to Redis later if needed.

### 14.5 Why polling instead of WebSocket for PC-to-relay

The PC polls the relay server every 2 seconds. This means the PC never needs an inbound connection — no port forwarding, no ngrok, no VPN. Polling every 2s is negligible bandwidth and adds at most 2s of latency to task pickup. The WebSocket is optional for the phone side where real-time results matter for UX.

### 14.6 Why the iOS app is last

The phone app is simple once the backend is solid — it is literally a text field that calls a POST endpoint. Building it before the backend is stable means rebuilding it every time the API changes. Build the PC agent first, make it reliable, then add the phone app in a week.

---

*End of document — Kim PRD v1.0*
