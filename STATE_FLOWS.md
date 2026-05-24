# State Flows

How state transitions happen in Kim's key subsystems.

---

## 1. Agent Loop State Machine

```
          ┌──────────┐
          │  IDLE     │  (waiting for task)
          └────┬─────┘
               │ receive task
          ┌────▼─────┐
          │ STARTING  │  print [STATUS], reset plan state
          └────┬─────┘
               │ build system prompt + take screenshot
          ┌────▼─────┐
     ┌───►│ THINKING  │  call LLM provider
     │    └────┬─────┘
     │         │ response received
     │    ┌────▼──────────────────────────┐
     │    │ response["type"] == ?         │
     │    └──┬──────────┬────────────────┘
     │       │          │
     │  tool_call      text
     │       │          │
     │  ┌────▼─────┐  ┌▼───────────────────┐
     │  │ EXECUTING │  │ CHECK_COMPLETION   │
     │  │ MCP tool  │  │ scan for           │
     │  └────┬─────┘  │ TASK_COMPLETE or    │
     │       │         │ NEED_HELP           │
     │       │ result  └──┬──────────┬──────┘
     │       │            │          │
     └───────┘       found          not found
                      │               │
                 ┌────▼─────┐    ┌────▼─────┐
                 │ COMPLETE  │    │ CONTINUE  │──────┐
                 │ or HELP   │    │ (loop)    │      │
                 └──────────┘    └──────────┘      │
                                                    │
                                      stuck (3 same screenshots)
                                                    │
                                              ┌─────▼────┐
                                              │  STUCK    │
                                              │  (abort)  │
                                              └──────────┘
```

### Iteration limits
- `max_iterations` (default 25): hard stop, returns to user
- Stuck detection: 3 consecutive identical screenshot hashes → abort

---

## 2. Browser Provider State Machine

```
          ┌──────────┐
          │ INIT      │  no browser connected
          └────┬─────┘
               │ complete() called
          ┌────▼─────┐
          │ CONNECT   │  connect to Chrome CDP port 9222
          └────┬─────┘
               │ connected
          ┌────▼─────┐
          │ FIND_TAB  │  locate active AI chat tab
          └────┬─────┘
               │ tab found (or navigate to site)
          ┌────▼──────┐
          │ INJECT     │  paste prompt via clipboard
          └────┬──────┘
               │ sent
          ┌────▼──────┐
          │ WAITING    │  poll for response completion
          └────┬──────┘
               │ response detected
          ┌────▼──────┐
          │ SCRAPE     │  extract response from DOM
          └────┬──────┘
               │ parsed
          ┌────▼──────┐
          │ RETURN     │  normalize to provider response shape
          └──────────┘
```

### Site selector map

| Site | Response element | Input element | Send button |
|------|-----------------|---------------|-------------|
| claude.ai | `[data-testid='conversation-turn-3']` last | `div[contenteditable='true']` | `button[aria-label*='Send']` |
| chatgpt.com | `div.markdown` last | `div#prompt-textarea` | `button[data-testid='send-button']` |
| gemini.google.com | `model-response` last | `rich-textarea > div[contenteditable]` | `button[aria-label*='Send']` |

---

## 3. MCP Tool Execution Flow

```
agent.py calls MCP client
  → stdio writes JSON-RPC to mcp_server
    → server.py dispatches to tools/<module>.py
      → tool validates args
      → tool executes (with try/except)
      → returns [TextContent(text="result")]
    → server.py sends JSON-RPC response
  → agent receives result string
  → agent appends to conversation: "[Tool result: tool_name]\nresult"
```

### Error envelope
```
"ERROR: <message>"           → general error
"PERMISSION_ERROR: <message>" → path/safety violation
"OS_LIMITATION: <message>"    → unsupported on current OS
```

---

## 4. Session Lifecycle

```
New task received
  → agent checks for resume_session_id
    → if found: load existing messages from JSONL
    → if not: create new session file
  → each turn: append message to JSONL
  → on TASK_COMPLETE: flush, generate summary
  → session file persists for history/reload
```

---

## 5. Plan Lifecycle (UI)

```
LLM emits "PLAN: N steps\n1. step1\n2. step2\n..."
  → agent._emit_plan_markers() parses it
    → emits [PLAN]{"steps":["step1","step2"]}
  → frontend parsePlanFromActivity() picks it up
    → renders plan checklist

LLM emits "STEP 2: doing step2"
  → agent._emit_plan_markers() parses it
    → emits [STEP]{"index":1,"label":"step2","status":"running"}
  → frontend updates checklist item

Task completes
  → agent emits [DONE]{"index":1,"label":"step2","status":"done"}
  → frontend marks step as complete
```
