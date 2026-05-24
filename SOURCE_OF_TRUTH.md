# Source of Truth

Each concept in Kim has exactly one authoritative location. If you need to change behavior, change it at the source. Don't duplicate logic.

---

## Concepts and their owners

| Concept | Source of truth | Consumers |
|---------|----------------|-----------|
| Stdout protocol markers | `orchestrator/agent.py` (`_emit_plan_markers`, `run()`) | `desktop/src/components/ChatView.tsx`, `kim-cli/src/provider.rs` |
| Provider response shape | `orchestrator/providers/base.py` (docstring + abstract method) | All provider implementations, `orchestrator/agent.py` |
| Provider factory | `orchestrator/providers/base.py` (`create_provider`) | `orchestrator/agent.py`, `tray/app.py` |
| MCP tool definitions | `mcp_server/tools/*.py` (each tool module) | `mcp_server/server.py` (registration), agent loop (via MCP client) |
| MCP server entry | `mcp_server/server.py` | Tauri (`send_task`), Claude Code, Claude Desktop |
| Session storage format | `orchestrator/session_store.py` | `orchestrator/agent.py`, `desktop/src-tauri/src/lib.rs` |
| Conversation memory | `orchestrator/memory.py` | `orchestrator/agent.py` |
| Config schema | `config.yaml` + `mcp_server/config.py` | All Python modules |
| TypeScript types | `desktop/src/types/index.ts` | All `.tsx` components |
| Tauri commands | `desktop/src-tauri/src/lib.rs` | `desktop/src/` (via `@tauri-apps/api`) |
| OS detection | `mcp_server/os_utils.py` | `mcp_server/tools/shell.py`, `mcp_server/tools/windows.py` |
| Voice engine | `tray/voice.py` | `orchestrator/agent.py`, `tray/app.py` |
| System prompt | `orchestrator/agent.py` (`_build_system_prompt`) | Agent loop |
| Browser site selectors | `orchestrator/providers/browser_provider.py` | Browser provider only |

## Rules

1. **One owner per concept.** If two files define the same thing, one must import from the other.
2. **Types flow downward.** `types/index.ts` defines TS types. `base.py` defines Python response shapes. Don't redefine in consumers.
3. **When splitting a file**, the original location must re-export (or the split module becomes the new source of truth and all imports update).
