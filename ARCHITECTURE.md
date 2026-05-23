# Architecture

Kim is a local AI agent platform. This document maps the system's layers and data flow.

---

## System layers

```
┌─────────────────────────────────────────────────────┐
│  Desktop UI (React 19 + Tauri 2)                    │
│  ChatView.tsx → parses stdout events → renders UI   │
├─────────────────────────────────────────────────────┤
│  Tauri Shell (lib.rs)                               │
│  55 commands · spawns Python agent as child process  │
│  Reads agent stdout line-by-line · forwards to UI   │
├─────────────────────────────────────────────────────┤
│  Agent Loop (orchestrator/agent.py)                 │
│  Emits [STATUS]/[TOOL]/[PLAN]/[STEP]/[DONE] on     │
│  stdout · calls LLM provider · executes MCP tools   │
├─────────────────────────────────────────────────────┤
│  LLM Providers (orchestrator/providers/)            │
│  claude · openai · gemini · deepseek · ollama ·     │
│  browser (API-key-free via Playwright CDP)           │
├─────────────────────────────────────────────────────┤
│  MCP Server (mcp_server/ — stdio transport)         │
│  31 tools: files, shell, screen, mouse, keyboard,   │
│  windows, browser/web, git, code, search             │
├─────────────────────────────────────────────────────┤
│  OS (Windows / macOS / Linux)                       │
│  pyautogui · mss · playwright · pygetwindow/wmctrl  │
└─────────────────────────────────────────────────────┘
```

## Data flow: task execution

```
User types task in UI
  → ChatView.tsx calls Tauri command `send_task`
    → lib.rs spawns `python -m orchestrator.agent` with task on stdin
      → agent.py builds system prompt, takes screenshot
        → calls LLM provider with [system, screenshot, history, tools]
          → provider returns {"type": "tool_call", ...} or {"type": "text", ...}
        → if tool_call: agent calls MCP tool, appends result, loops
        → agent prints [STATUS], [TOOL], [PLAN], [STEP], [DONE] to stdout
      → lib.rs reads stdout lines, forwards to frontend via Tauri events
    → ChatView.tsx parses events, updates activity feed + plan checklist
  → task completes with TASK_COMPLETE or NEED_HELP
```

## Data flow: browser provider (API-key-free)

```
agent.py calls BrowserProvider.complete()
  → Playwright connects to Chrome on CDP port 9222
    → navigates to Claude/ChatGPT/Gemini
    → injects prompt via clipboard paste
    → waits for response generation
    → scrapes response from DOM
  → parses response into {"type": "tool_call"} or {"type": "text"}
  → returns to agent loop (same as any other provider)
```

## Key directories

| Directory | Owner | Description |
|-----------|-------|-------------|
| `orchestrator/` | Agent team | Agent loop + LLM providers |
| `mcp_server/` | Tools team | MCP server + 31 OS-control tools |
| `desktop/src/` | UI team | React frontend components |
| `desktop/src-tauri/src/` | Shell team | Rust Tauri backend |
| `tray/` | UX team | System tray + voice engine |
| `extension/` | Browser team | Chrome extension for DOM scraping |
| `relay_server/` | Infra team | Phone-to-PC task relay |
| `tests/` | All | Test suites |

## Key interfaces (see CONTRACTS.md for full spec)

1. **Stdout protocol**: agent.py → lib.rs → ChatView.tsx
2. **Provider response**: providers → agent.py
3. **MCP tool schema**: mcp_server → agent.py (via stdio)
4. **Session JSONL**: agent.py → disk → lib.rs → ChatView.tsx
5. **Tauri events**: lib.rs → ChatView.tsx (via `emit()`)
