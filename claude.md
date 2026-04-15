# CLAUDE.md — Kim Agent Platform
## Context for Claude Opus building this project

---

## What you are building

**Kim** is a local AI agent platform for **Windows, macOS, and Linux**. It connects any cloud LLM (Claude, GPT-4o, Gemini, DeepSeek) to full OS control — screen vision, mouse/keyboard, file system, browser automation, and shell execution. It is the personal equivalent of Claude Code + Claude Computer Use, running locally, controlled by the user.

> **Cross-Platform (Phase 6):** All MCP tools now auto-detect the host OS
> via `mcp_server/os_utils.py`. Shell commands are translated between
> platforms (e.g. `start notepad` → `open -a TextEdit` on Mac). Window
> management uses `pygetwindow` (Windows), `osascript` (macOS), or
> `wmctrl`/`xdotool` (Linux). Unsupported operations return structured
> `OS_LIMITATION` messages so the LLM can adapt its approach.

The user has an existing working prototype (Bridge V3) that:
- Scrapes Gemini's DOM via a Chrome extension
- POSTs parsed `## FILE:` and `## CMD:` blocks to a Flask server
- Flask server writes files and runs shell commands
- Auto-loop feeds errors back to Gemini for self-correction

You are rebuilding this as a production system using the Model Context Protocol (MCP).

---

## Output format — CRITICAL

Every piece of code you write MUST be in this format so the bridge can sync it automatically:

```
## FILE: path/to/file.py
[complete file content]

## CMD: pip install mcp anthropic pyautogui mss playwright
```

- `## FILE:` paths are relative to the project root (e.g. `mcp_server/server.py`)
- `## CMD:` runs in the project root directory
- Every file must be **complete and runnable** — no placeholders, no `# TODO`, no `pass`
- Include all imports at the top of every file
- Include error handling and logging everywhere
- When you finish a file, say `DONE: filename`

---

## Project structure

```
kim/
  mcp_server/
    server.py              ← Main MCP server (stdio transport)
    os_utils.py            ← Cross-platform OS detection & command translation
    tools/
      files.py             ← read_file, write_file, list_dir, delete_file
      shell.py             ← run_command, run_powershell (cross-platform)
      screen.py            ← take_screenshot, get_screen_info
      mouse.py             ← click, double_click, right_click, drag, scroll
      keyboard.py          ← type_text, hotkey, key_press
      windows.py           ← get_windows, focus_window, resize_window (cross-platform)
      browser.py           ← open_url, browser_click, fill_form, get_page_text
      git.py               ← git_status, git_diff, git_commit, git_log
      code.py              ← run_python, run_node, run_tests, lint_file
      search.py            ← search_in_files, find_files
    config.py              ← PROJECT_ROOT, allowed paths, safety rules
  orchestrator/
    agent.py               ← Main async agent loop
    providers/
      base.py              ← Abstract provider interface
      claude.py            ← Anthropic API
      openai_provider.py   ← OpenAI API
      gemini.py            ← Google GenAI API
      deepseek.py          ← DeepSeek API
      browser_provider.py  ← Playwright CDP API-free provider
    memory.py              ← Conversation history + compression
    task_queue.py          ← Local queue + relay server poller
  relay_server/
    main.py                ← FastAPI relay server
    auth.py                ← API key middleware
    queue.py               ← SQLite task queue
    models.py              ← Pydantic request/response models
  extension/
    manifest.json
    background.js
    content_claude.js      ← Claude.ai selectors
    content_chatgpt.js     ← ChatGPT selectors
    content_gemini.js      ← Gemini selectors
    content_deepseek.js    ← DeepSeek selectors
    popup.html
    popup.js
    overlay.js
  tray/
    app.py                 ← pystray system tray
    ui.py                  ← tkinter control panel
    settings.py            ← Settings window
  tests/
    test_mcp_tools.py
    test_orchestrator.py
    test_relay.py
  .env.example
  config.yaml
  requirements.txt
  README.md
```

---

## Phase-by-phase build order

Build ONE phase per session. Do not combine phases.

### Phase 1 — MCP Server (build this first)

**Goal:** Replace the Flask server with a proper MCP server that Claude Desktop, Claude Code, and the orchestrator can all connect to.

**Files to produce:**
- `mcp_server/server.py` — MCP server with stdio transport
- `mcp_server/config.py` — config loading from config.yaml
- `mcp_server/tools/files.py` — file operations
- `mcp_server/tools/shell.py` — command execution
- `mcp_server/tools/screen.py` — screenshot capture
- `mcp_server/tools/mouse.py` — mouse control
- `mcp_server/tools/keyboard.py` — keyboard control
- `mcp_server/tools/windows.py` — window management
- `config.yaml` — default config
- `requirements.txt` — all dependencies
- `.env.example` — API key template

**MCP server requirements:**
- Use `mcp` Python SDK (`pip install mcp`)
- stdio transport (not HTTP — this is for local connections)
- Each tool has a proper JSON schema for its arguments
- Structured logging to stderr (not stdout — stdout is reserved for MCP protocol)
- All file operations validate path is within PROJECT_ROOT
- All tool handlers use try/except and return error strings on failure (never raise)

**MCP tool schema pattern:**
```python
from mcp.server import Server
from mcp.types import Tool, TextContent

server = Server("kim")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="write_file",
            description="Write content to a file. Creates parent directories if needed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to project root"},
                    "content": {"type": "string", "description": "File content to write"}
                },
                "required": ["path", "content"]
            }
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "write_file":
        # implementation
        return [TextContent(type="text", text="Written successfully")]
```

**Claude Desktop config snippet to output:**
```json
{
  "mcpServers": {
    "kim": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "C:\\path\\to\\kim"
    }
  }
}
```

---

### Phase 2 — Multi-LLM Orchestrator

**Goal:** An autonomous agent loop that takes a task, sees the screen, calls any LLM, executes tool calls via the MCP server, and loops until done. Includes a 100% API-key free option using browser automation.

**Files to produce:**
- `orchestrator/agent.py`
- `orchestrator/providers/base.py`
- `orchestrator/providers/claude.py`
- `orchestrator/providers/openai_provider.py`
- `orchestrator/providers/gemini.py`
- `orchestrator/providers/deepseek.py`
- `orchestrator/providers/browser_provider.py`
- `orchestrator/memory.py`
- `orchestrator/task_queue.py`

**Agent loop logic:**
```
1. receive task string
2. build system prompt (task + tool list + safety rules)
3. take screenshot → encode base64
4. call LLM with [system_prompt, screenshot, history]
5. parse response:
   - if tool_call → call MCP tool → append result → goto 3
   - if "TASK_COMPLETE: summary" → exit, return summary
   - if "NEED_HELP: reason" → pause, notify user
6. if iterations > max_iterations → stop, ask user
```

**Browser Provider (No-API Option) Requirements:**
- Must use `playwright` to connect to an existing, open browser session (e.g., using `playwright.chromium.connect_over_cdp("http://localhost:9222")`).
- Locate the active AI chat tab (ChatGPT, Claude, or Gemini).
- Inject the system prompt + user task + conversation history into the input box and click send.
- Wait for the generation to complete (detecting UI state changes), scrape the final response, and parse it into the standard `{"type": "tool_call", ...}` format.

**Provider interface (base.py):**
```python
from abc import ABC, abstractmethod

class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str
    ) -> dict:
        """Returns: {"type": "tool_call", "tool": str, "args": dict}
                  | {"type": "text", "content": str}"""
        pass
```

**Screenshot encoding:**
```python
import mss
import base64
from PIL import Image
import io

def take_screenshot(scale=0.75):
    with mss.mss() as sct:
        screenshot = sct.grab(sct.monitors[1])
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        if scale != 1.0:
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        return base64.b64encode(buffer.getvalue()).decode()
```

**config.yaml structure:**
```yaml
provider: browser         # browser | claude | openai | gemini | deepseek
model:
  claude: claude-opus-4-6
  openai: gpt-4o
  gemini: gemini-2.0-flash
  deepseek: deepseek-chat
project_root: "A:\\Projects"
max_iterations: 25
screenshot_scale: 0.75
relay:
  url: ""                 # https://your-relay.railway.app
  poll_interval: 2        # seconds
  pc_api_key: ""          # set in .env
```

---

### Phase 3 — Browser Extension v2

**Goal:** Extend the existing extension to work on Claude.ai, ChatGPT, and DeepSeek in addition to Gemini. Add relay status indicator and provider switcher to popup.

**Selector map per site:**

| Site | Response selector | Input selector | Send button |
|------|------------------|----------------|-------------|
| claude.ai | `[data-testid='conversation-turn-3']` last | `div[contenteditable='true']` | `button[aria-label*='Send']` |
| chatgpt.com | `div.markdown` last | `div#prompt-textarea` | `button[data-testid='send-button']` |
| gemini.google.com | `model-response` last | `rich-textarea > div[contenteditable]` | `button[aria-label*='Send']` |
| chat.deepseek.com | `div.ds-markdown` last | `textarea` | `div[role='button']` last |

**manifest.json content_scripts must match all four sites.**

**The auto-loop error correction logic from the existing content.js must be preserved exactly.** The ## FILE: and ## CMD: parsing in background.js must be preserved exactly.

**New features to add:**
- Popup shows which site is active and whether auto-loop is running
- Popup shows relay server connection status (green/red dot)
- File drag-and-drop: if user drops a file in the chat, extension reads it via FileReader API and sends content to MCP server as a write_file call

---

### Phase 4 — Relay Server

**Goal:** A FastAPI server deployable to Railway/Render free tier. Phone POSTs a task. PC polls for tasks. PC POSTs result. Phone gets result.

**Files to produce:**
- `relay_server/main.py`
- `relay_server/auth.py`
- `relay_server/queue.py`
- `relay_server/models.py`
- `Dockerfile`
- `railway.toml`

**API endpoints:**

```
POST   /prompt              body: {task: str, priority: int}         auth: phone_key
                            response: {task_id: str, queued: bool}

GET    /prompt/next         response: {task_id: str, task: str} | null  auth: pc_key

POST   /result              body: {task_id: str, summary: str,        auth: pc_key
                                   screenshot: str, success: bool}
                            response: {ok: bool}

GET    /result/{task_id}    response: {status: pending|done|failed,   auth: phone_key
                                       summary: str, screenshot: str}

WS     /ws                  query: token=phone_key                    WebSocket
                            server pushes result JSON when task completes

GET    /status              response: {pc_connected: bool,            any
                                       last_seen: str, queue_depth: int}
```

**SQLite schema:**
```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    picked_up_at TIMESTAMP,
    completed_at TIMESTAMP,
    summary TEXT,
    screenshot TEXT,
    success BOOLEAN
);
```

**PC polling in orchestrator/task_queue.py:**
```python
async def poll_relay(self):
    while True:
        try:
            resp = await self.client.get(f"{self.relay_url}/prompt/next",
                                          headers={"X-API-Key": self.pc_key})
            if resp.status_code == 200 and resp.json():
                task = resp.json()
                await self.run_task(task["task_id"], task["task"])
        except Exception as e:
            logger.warning(f"Relay poll failed: {e}")
        await asyncio.sleep(self.poll_interval)
```

---

### Phase 5 — Tray App ✅ COMPLETE

**Goal:** System tray icon that lets the user start/stop the agent, input tasks, view live logs, and switch providers — without opening a terminal.

**Files produced:**
- `tray/app.py`
- `tray/ui.py`
- `tray/settings.py`

**Status:** All deliverables implemented and tested.

---

### Phase 5.5 — Cross-Platform Architecture ✅ COMPLETE

**Goal:** Make `mcp_server` tools seamlessly support macOS, Linux, and Windows.

**Files produced:**
- `mcp_server/os_utils.py` — OS detection, command translation dictionary, app-launch mapping
- `mcp_server/tools/shell.py` — Updated with cross-platform command interception via `os_utils.translate_command()`
- `mcp_server/tools/windows.py` — Platform-dispatching backends: `pygetwindow` (Windows), `osascript` (macOS), `wmctrl`/`xdotool` (Linux)
- `requirements.txt` — Platform-conditional dependencies (`sys_platform` markers)

**Cross-platform translation summary:**

| Windows command | macOS equivalent | Linux equivalent |
|----------------|-----------------|------------------|
| `start notepad` | `open -a 'TextEdit'` | `gedit` |
| `start calc` | `open -a 'Calculator'` | `gnome-calculator` |
| `cls` | `clear` | `clear` |
| `dir` | `ls -la` | `ls -la` |
| `tasklist` | `ps aux` | `ps aux` |
| `notepad.exe file.txt` | `open -a 'TextEdit' file.txt` | `gedit file.txt` |
| PowerShell `-Command` | Uses `pwsh` if installed | Uses `pwsh` if installed |

**Window management backends:**

| Operation | Windows | macOS | Linux |
|-----------|---------|-------|-------|
| List windows | `pygetwindow` | `osascript` (AppleScript) | `wmctrl -l -G` |
| Focus window | `pygetwindow` | `osascript` + `AXRaise` | `wmctrl -a` / `xdotool` |
| Resize window | `pygetwindow` | `osascript` position/size | `wmctrl -e` / `xdotool` |
| Fallback | — | Built-in (no install) | Clean `OS_LIMITATION` message |

**Status:** All deliverables implemented and tested.

---

### Phase 6 — Claude Code Compatibility & Developer Tooling ✅ COMPLETE

**Goal:** Make Kim work as a drop-in MCP server for Claude Code CLI. Add git, code execution, and search tools.

**Files produced:**

- `mcp_server/tools/git.py` — Git repository management (6 tools)
- `mcp_server/tools/code.py` — Code execution and linting (3 tools)
- `mcp_server/tools/search.py` — Project-wide search (2 tools)
- `mcp_server/server.py` — Updated with all 11 new tool registrations

**Git tools (`git.py`):**

| Tool | Description | Key args |
|------|-------------|----------|
| `git_status` | Working tree status | cwd, short |
| `git_diff` | Diff of changes (file or all, staged or unstaged) | path, staged, cwd |
| `git_add` | Stage files for commit | paths (string or array), cwd |
| `git_commit` | Commit with message | message (required), cwd |
| `git_log` | Recent commit history | n, oneline, cwd |
| `git_checkout` | Switch branch or restore file | target (required), create, cwd |

**Code tools (`code.py`):**

| Tool | Description | Key args |
|------|-------------|----------|
| `run_python` | Execute .py file or inline snippet | file, code, cwd, timeout |
| `run_node` | Execute .js file or inline snippet | file, code, cwd, timeout |
| `lint_file` | Lint Python file (ruff preferred, flake8 fallback) | path (required), fix, cwd |

**Search tools (`search.py`):**

| Tool | Description | Key args |
|------|-------------|----------|
| `search_in_files` | grep/ripgrep across project | pattern (required), path, include, case_sensitive, regex, context_lines |
| `find_files` | Glob pattern file finder with sizes | pattern (required), path, type |

**Registration command:**
```bash
## CMD: claude mcp add Kim -- python -m mcp_server.server
```

**Total MCP tools:** 31 (20 Phase 1 + 11 Phase 6)

**Status:** All deliverables implemented. Git and search tools verified on macOS.

---

### Phase 7 — Production Hardening

**Goal:** Make it reliable enough to use daily without surprises.

**Checklist:**
- [ ] Path validation in all file tools — reject anything outside PROJECT_ROOT
- [ ] Blocked commands list in shell.py — `rm -rf /`, `format c:`, `del /S` etc.
- [ ] Action preview mode — add `preview: bool` to config.yaml; if true, print action and wait 3s before executing
- [ ] Progress detection — if last 3 screenshots are identical (compare hashes), abort with "Agent appears stuck"
- [ ] Retry with backoff on LLM API errors (429, 500, 503)
- [ ] Playwright session persistence — save cookies/storage to `sessions/` dir
- [ ] Structured JSON logging to `logs/kim_{date}.jsonl`
- [ ] Full pytest test suite for all MCP tools
- [ ] `setup.py` or `install.bat` — one command to install all deps and configure

---

## Key technical patterns

### Calling MCP tools from the orchestrator

The orchestrator does NOT import the MCP server directly. It calls it over stdio like any other MCP client:

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def get_mcp_session():
    server_params = StdioServerParameters(
        command="python",
        args=["-m", "mcp_server.server"],
        cwd=PROJECT_ROOT
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return session  # use for tool calls
```

### LLM tool call format

All providers must normalize to this internal format:

```python
# What the orchestrator sends to providers:
tools = [
    {
        "name": "click",
        "description": "Click at screen coordinates",
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]}
            },
            "required": ["x", "y"]
        }
    }
]

# What providers must return:
{"type": "tool_call", "tool": "click", "args": {"x": 340, "y": 220}}
# or
{"type": "text", "content": "TASK_COMPLETE: Clicked the submit button."}
```

### Error handling in MCP tools

Every tool handler must follow this pattern:

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "write_file":
            result = await handle_write_file(arguments)
            return [TextContent(type="text", text=result)]
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except PermissionError as e:
        return [TextContent(type="text", text=f"PERMISSION_ERROR: {e}")]
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return [TextContent(type="text", text=f"ERROR: {e}")]
```

---

## Environment variables (.env)

```
# LLM API Keys
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...
DEEPSEEK_API_KEY=sk-...

# Relay Server
RELAY_PC_API_KEY=your-secret-pc-key
RELAY_PHONE_API_KEY=your-secret-phone-key
RELAY_URL=https://your-relay.railway.app

# Kim Config (can also be in config.yaml)
PROJECT_ROOT=A:\Projects
ACTIVE_PROVIDER=claude
```

---

## What already works (DO NOT rewrite unless asked)

The user has a working Bridge V3 with these files:
- `extension/background.js` — `## FILE:` and `## CMD:` parsing — **preserve this logic**
- `extension/content.js` — auto-loop error detection — **preserve this logic**
- `overlay/overlay.py` — draggable tkinter sync button — **preserve, enhance in Phase 5**
- `server/server.py` — Flask file writer and command runner — **replace with MCP server in Phase 1**

The `## FILE:` and `## CMD:` block format is sacred. The user's bridge depends on it. Always output code in this format.

---

## Clarifying questions to ask before coding

If the user's request is ambiguous, ask:
1. Which phase are we building?
2. What has already been built and tested?
3. Are there any files I should read first?
4. Any constraints on dependencies or Python version?

Do not assume. Ask. Then build completely.

---

## Definition of done per phase

A phase is done when:
- All listed files are output as complete `## FILE:` blocks
- All `## CMD:` install commands are provided
- There are no placeholders, TODOs, or stub functions
- Basic usage is demonstrated in a docstring or README section
- The user can run it and confirm it works before you start the next phase