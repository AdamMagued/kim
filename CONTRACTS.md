# Contracts

These are the inviolable interfaces between Kim's subsystems. Changing any of these without updating ALL consumers will break the system silently.

---

## 1. Stdout Protocol

**Producer:** `orchestrator/agent.py`
**Consumers:** `desktop/src/components/ChatView.tsx` (parseLogLine, parsePlanFromActivity), `kim-cli/src/provider.rs` (lines 1087-1504)

### Marker format

Each marker is a complete line printed to stdout with `flush=True`.

```
[STATUS] <free text>
[TOOL] <tool_name>(<json_args_truncated_to_120_chars>)
[SUCCESS] <result text>
[FAILED] <error text>
[PLAN]{"steps":["step1","step2",...]}
[STEP]{"index":<n>,"label":"<step_label>","status":"running"}
[DONE]{"index":<n>,"label":"<step_label>","status":"done"}
```

### Rules

- No whitespace between marker and content (e.g., `[STATUS] text` — one space after `]`)
- `[PLAN]`, `[STEP]`, `[DONE]` have NO space between `]` and `{` — the JSON is immediately adjacent
- JSON in plan markers uses compact separators: `separators=(",", ":")`
- `[PLAN]` steps array: max 12 items, each truncated to 120 chars
- `[TOOL]` args JSON truncated to 120 chars
- All markers end with newline (via `print(..., flush=True)`)

### UI markers (special)

```
[UI] SCREENSHOT_FLASH
[UI] SHOW
```

These are consumed by `lib.rs` directly (not the frontend JS).

---

## 2. Provider Response Shape

**Definition:** `orchestrator/providers/base.py`
**Implementers:** claude.py, openai_provider.py, gemini.py, deepseek.py, ollama.py, browser_provider.py

### Shape

Every provider's `complete()` method must return exactly one of:

```python
{"type": "tool_call", "tool": str, "args": dict}
{"type": "text", "content": str}
```

### Additional fields (optional, backward-compatible)

```python
{"type": "tool_call", "tool": str, "args": dict, "content": str}  # thinking/narration
{"type": "text", "content": str, "tool_call_id": str}              # tool result context
```

The agent loop checks `response["type"]` first, then reads `response["tool"]` / `response["content"]`.

---

## 3. Canonical Message Format

**Definition:** `orchestrator/providers/base.py` (docstring)

```python
{"role": "user" | "assistant", "content": str | list[ContentItem]}
```

Where ContentItem is:
```python
{"type": "text",  "text": "..."}
{"type": "image", "data": "<base64>", "media_type": "image/png"}
```

---

## 4. MCP Tool Schema

**Definition:** `mcp_server/tools/*.py`
**Registration:** `mcp_server/server.py`

Each tool is registered with:
```python
Tool(
    name="tool_name",
    description="What it does",
    inputSchema={
        "type": "object",
        "properties": { ... },
        "required": [ ... ]
    }
)
```

Tool handlers return:
```python
[TextContent(type="text", text="result string")]
```

On error:
```python
[TextContent(type="text", text="ERROR: description")]
[TextContent(type="text", text="PERMISSION_ERROR: description")]
```

---

## 5. Session JSONL Format

**Producer:** `orchestrator/session_store.py`
**Consumers:** `lib.rs` (`load_session_messages`), `ChatView.tsx`

Each line is a JSON object:
```json
{"role": "user", "content": "..."}
{"role": "assistant", "content": "..."}
{"role": "user", "content": "[Tool result: tool_name]\nresult text"}
```

Session files stored in `sessions/` directory, named by session ID.

---

## 6. Task Completion Signals

**Producer:** `orchestrator/agent.py`
**Consumer:** `lib.rs` (process exit), `tray/app.py`

The agent signals completion via text content matching these patterns:

```
TASK_COMPLETE: <summary>     → success, return summary
NEED_HELP: <reason>          → pause, notify user
```

Or via tool call: `{"type": "tool_call", "tool": "task_complete", "args": {"message": "..."}}`

---

## 7. Provider Names

**Definition:** `orchestrator/providers/base.py` (`create_provider`)
**Also in:** `desktop/src/types/index.ts` (Provider type)

Valid names: `claude`, `openai`, `gemini`, `deepseek`, `browser`, `ollama`
Extended: `browser:claude`, `browser:chatgpt`, `browser:gemini`

Both Python and TypeScript must agree on this set.
