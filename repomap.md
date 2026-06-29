# repomap.md — Kim Agent Platform

## Overview
Kim is a local AI agent platform for Windows, macOS, and Linux that connects cloud LLMs (Claude, GPT-4o, Gemini, DeepSeek) to full OS control — screen vision, mouse/keyboard, file system, browser automation, and shell execution.

---

## desktop/src (React/TypeScript UI)

**Main application shell:**
- `App.tsx` — Root component: settings panels, session list management, theme configuration, auto-updates
- `main.tsx` — React entry point with Tauri application initialization and design-tokens CSS import

**Core chat interface:**
- `components/ChatView.tsx` — Main chat UI: activity stream, plan checking, input composer, and provider switches
- `components/MessageBubble.tsx` — Custom conversation bubble rendering with markdown and typing animations
- `components/kim-ui/RevampSidebar.tsx` — Active navigation sidebar managing sessions, settings, and workspace profiles

**Thinking & plan display:**
- `components/kim-ui/ThinkingWithPlan.tsx` — Stream trace viewer representing LLM thoughts, tool outputs, and collapsible plans
- `components/kim-ui/CollapsiblePlan.tsx` — Nested checklist UI components reflecting dynamic agent tasks

**Settings & configuration:**
- `components/kim-ui/RevampSettings.tsx` — Tabbed settings dialog (Appearance, AI config, paths, MCP tools, and accounts)
- `components/ProviderPicker.tsx` — Provider model selector dropdown
- `components/BrowserProviderPicker.tsx` — Browser LLM selector for Playwright session setups

**Other UI components:**
- `components/OnboardingFlow.tsx` — Guided user walkthrough for initial desktop application onboarding
- `components/ToolCallCard.tsx` — Structured view showing MCP tool execution inputs, parameters, and return states
- `components/PairingModal.tsx` — Mobile pairing QR code display for the tasks relay server
- `components/UpdateModal.tsx` — Tauri update alert popup
- `components/Toast.tsx` — Application toast notifications
- `components/CancelWidget.tsx` — Persistent overlay for quick execution cancellations
- `components/kim-ui/ConnectorsPanel.tsx` — Third-party site-specific credentials and cookie status manager
- `components/kim-ui/WorkedForPill.tsx` — Run-time history indicator showing execution metrics

**Hooks & Styles:**
- `hooks/useTheme.ts` — Persistent application theme state loader
- `hooks/useAccount.ts` — Load/save user accounts state via Tauri commands
- `hooks/useAuthStatus.ts` — OAuth state checkers for third-party cloud auth
- `hooks/useSessions.ts` — Active chat sessions hook
- `types/index.ts` — Shared TypeScript type interfaces (messages, providers, settings)
- `styles/design-tokens.css` — Global CSS variables for design system themes and typography (relocated from design-mocks)

---

## desktop/src-tauri/src (Rust Tauri backend)

- `main.rs` — Rust application bootstrap and Tauri build initialization
- `lib.rs` — Tauri 2 desktop shell; sets up core command mapping, WebView window parameters, and module declarations
- `google_oauth.rs` — Google OAuth 2.0 PKCE desktop flow handling Google profile linkings
- `http_bridge.rs` — `tiny_http` server exposing `/v1/*` endpoints (task spawn, split send/receive for browser provider, WebView state controls, health probe)
- `browser_bridge.rs` — Manages the persistent in-app WebView automaton window and injects `bridge.js` for browser-provider interception
- `bridge.js` — Injected JavaScript (loaded via `include_str!`) that intercepts fetch/XHR inside Claude/ChatGPT/Gemini and routes through the HTTP bridge
- `subprocess.rs` — Agent subprocess spawning, Python executable discovery, process group management, and task cancel/kill logic
- `window_manager.rs` — Task-active window mode, show/hide main window, screenshot flash window lifecycle
- `updater.rs` — Tauri auto-update check logic
- `config.rs` — `AppConfig` struct; deserializes `config.yaml` into Tauri managed state
- `provider_auth.rs` — Provider authentication helpers (API key validation, token storage)
- `schedule_commands.rs`, `scheduler.rs` — Scheduled task commands and background scheduler management
- `secrets.rs` — Secure secret retrieval helpers
- `speed_access.rs` — Quick-access shortcut commands
- `account.rs`, `codex_projects.rs`, `data_io.rs`, `feedback.rs`, `ollama.rs`, `relay.rs`, `run_history.rs`, `session_commands.rs`, `voice_config.rs` — Additional Tauri command modules registered into the main handler

---

## orchestrator (Python agent engine)

**Agent core:**
- `agent.py` — Core async execution loop: prompts assembly, token limits checking, screenshots, stuck triggers, and tool calls
- `agent_states.py` — Run states and execution outcome enums
- `cli.py` — Command-line interface parameters wrapper for standalone invocations
- `mcp_client.py` — Stdio-based client connection launcher mapping tool calls
- `ui_bridge.py` — Formatted stdout output logger mapping data streams
- `tool_utils.py` — Normalizer for tool calls and JSON responses extraction
- `tool_errors.py` — Tool error classification helpers
- `tool_risk.py` — Risk tier definitions mirroring `mcp_server/tool_tiers.py`
- `stuck_detection.py` — Detects agent runloop stuck conditions and triggers recovery
- `interaction_policy.py` — Policy logic for human-in-the-loop interaction gates
- `codex_bridge_service.py` — Consolidated Codex bridge launcher (replaced legacy `run_codex_bridge.py` entrypoint; imports the engine from the top-level `codex_engine/engine.py` package); manages `_CodexProxy` lifecycle with `atexit`/SIGTERM cleanup
- `scheduled_runner.py` — Background runner for scheduled/cron agent tasks
- `cron_store.py` — Persistent cron schedule storage
- `obs_logging.py` — Observability / structured logging helpers
- `compare.py` — Utility for comparing agent outputs across runs

**LLM providers:**
- `providers/base.py` — Core provider abstract classes and initialization maps
- `providers/claude.py` — Anthropic Claude SDK adapter
- `providers/openai_provider.py` — OpenAI API integration
- `providers/gemini.py` — Gemini developer API adapter with OAuth Bearer access
- `providers/deepseek.py` — DeepSeek API client
- `providers/ollama.py` — Local Ollama provider support with local vision retries
- `providers/browser_provider.py` — Compatibility endpoint interface for Webview-backed providers
- `providers/browser/provider.py` — Playwright browser executor utilizing CDP and WebView hooks
- `providers/browser/bridge_client.py` — REST proxy link between browser backend and Rust Tauri bridge
- `providers/browser/prompt_builder.py` — Scraper-compatible prompt and attachments constructor
- `providers/browser/response_parser.py` — Scrape target analyzer extracts text or tool calls
- `providers/browser/site_configs.py` — Provider selector DOM maps

**Memory & context:**
- `memory.py` — Session history compilation, pruning, and screenshot budgets management
- `context_meter.py` — Token usage estimator and boundary checks for browser/API provider states
- `context_loader.py` — Instruction files discoverer and prompt context injector
- `compaction.py` — History summarizing compaction logic for context preservation

**Session management:**
- `session_store.py` — JSONL session logging, message caches, and runs log sidecars
- `archive/relay_worker.py` — Legacy phone-to-PC relay task poller client (archived; `relay_server/` remains active)

---

## codex_engine (Codex bridge runtime)

- `engine.py` — Codex bridge "engine": `_CodexProxy` (aiohttp), `run_codex_subtask`, the Codex subprocess launcher, and the `~/.codex/config.toml` writer. Consumed by `orchestrator/codex_bridge_service.py` as a sibling package. Not an MCP tool (not registered in `mcp_server/tool_registry.py`).

---

## mcp_server (Model Context Protocol server — 50 tools)

- `server.py` — Stdio transport MCP server router; processes incoming tool calls
- `tool_registry.py` — Central catalog and schema definitions for the 50 OS-control tools (source of truth: `len(TOOLS)` at startup)
- `config.py` — Environment variables and config.yaml loader
- `logger.py` — JSON structured logger outputting execution histories
- `os_utils.py` — Platform translator adapting file, process, and system commands to Windows/macOS/Linux

**Tools:**
- `tools/files.py` — Projects root-scoped read, write, listing, and delete actions
- `tools/shell.py` — Process exec shell triggers matching OS environments
- `tools/keyboard.py` — PyAutoGUI keystrokes, text injections, and shortcuts actions
- `tools/mouse.py` — Target pointer controls (clicks, drags, double clicks, scrolls)
- `tools/windows.py` — Desktop window listings and focus management via system APIs
- `tools/screen.py` — Desktop screen vision capture and monitors queries
- `tools/screen_annotator.py` — Graphic annotator applying numeric bounding boxes
- `tools/web.py` — Browser navigation, page inspection, DOM click, form fillers, and screenshots actions
- `tools/web_element_scoring.py` — Numeric prioritizer scoring elements for target queries
- `tools/web_observe_js.py` — Client JS observer recording interactive elements
- `tools/git.py` — Sandboxed Git status, diffs, commits, branches, and logs execution
- `tools/code.py` — Local runtime builders for Python and NodeJS snippets with linter hooks
- `tools/search.py` — Quick local file searchers (ripgrep / file finder)
- `tools/github.py` — GitHub API tool integration (PR, issue, and repo operations)
- `tools/memory.py` — Agent memory store/recall tools
- `tools/ui_observe.py` — UI observation tools for accessibility and element inspection

---

## relay_server (FastAPI phone-to-PC tasks coordinator)

- `main.py` — API endpoints managing phone connections, prompt lists, status queries, and WebSocket relays
- `auth.py` — Simple API-key verification middleware
- `queue.py` — Local SQLite queue storing queued tasks
- `models.py` — Pydantic schemas validating task states

---

## pythonExperimentTool/claw-code (TUI client)

- `rust/crates/kim-cli/src` — Complete TUI interface: single-threaded event loop, Ratatui render interfaces, TUI configurations, local TUI session explorers, and TUI shortcuts
