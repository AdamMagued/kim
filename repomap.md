# repomap.md — Kim Agent Platform

## Overview
Kim is a local AI agent platform for Windows, macOS, and Linux that connects cloud LLMs (Claude, GPT-4o, Gemini, DeepSeek) to full OS control — screen vision, mouse/keyboard, file system, browser automation, and shell execution.

---

## desktop/src (React/TypeScript UI)

**Main application shell:**
- `App.tsx` — Root component: settings, session management, theme loading, update checks
- `main.tsx` — React entry point with Tauri initialization

**Core chat interface:**
- `components/ChatView.tsx` — Main chat UI: activity feed, trace display, plan parsing, run history, provider switching, retry/cancel logic
- `components/MessageBubble.tsx` — Message display with role-based styling and typing animation
- `components/Sidebar.tsx` — Navigation sidebar for sessions and settings

**Thinking & plan display:**
- `components/kim-ui/ThinkingWithPlan.tsx` — Live thinking panel: trace items (`TraceItem[]`), plan card, auto-collapse
- `components/kim-ui/CollapsiblePlan.tsx` — `PlanStep` / `PlanStepStatus` types and collapsible plan cards

**Settings & configuration:**
- `components/kim-ui/RevampSettings.tsx` — Multi-pane settings UI (appearance, AI, voice, paths, data, account, MCP, feedback, about)
- `components/SettingsPanel.tsx` — Settings panel shell
- `components/ProviderPicker.tsx` — LLM provider selection dropdown
- `components/BrowserProviderPicker.tsx` — Browser provider (Claude/ChatGPT/Gemini) selector

**Other UI components:**
- `components/OnboardingFlow.tsx` — Initial onboarding walkthrough
- `components/ToolCallCard.tsx` — Tool call display with args and results
- `components/PairingModal.tsx` — QR code relay pairing modal (NOTE: missing `qrcode.react` types, pre-existing build error)
- `components/UpdateModal.tsx` — App update notification and installation
- `components/Toast.tsx` — Transient notification system
- `components/CancelWidget.tsx` — Floating task cancellation widget
- `components/kim-ui/ConnectorsPanel.tsx` — Site-specific connector configuration UI
- `components/kim-ui/WorkedForPill.tsx` — "Worked for Xs" disclosure pill with run history

**Hooks:**
- `hooks/useTheme.ts` — Theme switching and persistence
- `hooks/useAccount.ts` — User account state management
- `hooks/useAuthStatus.ts` — OAuth / authentication status tracking
- `hooks/useSessions.ts` — Session list and navigation

**Types:**
- `types/index.ts` — Central type definitions: messages, settings, voice config, providers, sessions

---

## desktop/src-tauri/src (Rust Tauri backend)

- `main.rs` — Tauri app entry point and configuration
- `lib.rs` — ~7420-line Rust shell; central webview bridge, browser sign-in lifecycle, task spawning, and remaining shared Tauri commands
- `google_oauth.rs` — Google OAuth 2.0 handler for Gemini authentication via system keychain
- `account.rs`, `codex_projects.rs`, `data_io.rs`, `feedback.rs`, `ollama.rs`, `relay.rs`, `run_history.rs`, `session_commands.rs`, `voice_config.rs` — extracted Tauri command modules registered by `lib.rs`

---

## orchestrator (Python agent engine)

**Agent core:**
- `agent.py` — Main async agent loop: task execution, LLM provider calls, MCP tool dispatch, screenshot capture, stuck detection, context budgeting, `_build_system_prompt`, `_build_lean_system_prompt`
- `agent_states.py` — Explicit run-loop termination enum and result helper
- `cli.py` — CLI parser/entrypoint extracted from `agent.py`
- `mcp_client.py` — `MultiMCPClient` and MCP stdio session startup
- `ui_bridge.py` — UI bridge/log handler extracted from `agent.py`
- `tool_utils.py` — Tool-name normalization and JSON tool-call extraction
- `task_queue.py` — Local task queue with relay server poller

**LLM providers:**
- `providers/base.py` — Abstract `BaseProvider` interface and factory
- `providers/claude.py` — Anthropic API (streaming, tool use, vision)
- `providers/openai_provider.py` — OpenAI GPT integration
- `providers/gemini.py` — Google Gemini API with OAuth support
- `providers/deepseek.py` — DeepSeek API integration
- `providers/ollama.py` — Local Ollama with native tool calls, vision-error retry, image stripping
- `providers/browser_provider.py` — 25-line compatibility shim
- `providers/browser/provider.py` — Playwright/CDP and in-app webview bridge BrowserProvider implementation
- `providers/browser/bridge_client.py` — HTTP client for the Rust in-app bridge
- `providers/browser/prompt_builder.py` — Prompt/history formatting and attachment extraction
- `providers/browser/response_parser.py` — Scraped response parsing
- `providers/browser/site_configs.py` — Site selector maps

**Memory & context:**
- `memory.py` — Conversation history + compression, sliding window, automatic screenshot pruning
- `context_meter.py` — Context window budget tracking and per-message token estimation
- `context_loader.py` — Instruction file discovery and prompt building from project files
- `compaction.py` — Deterministic message compaction for stateless API providers

**Session management:**
- `session_store.py` — JSONL session persistence with summaries (kim_sessions/)
- `relay_worker.py` — Relay server integration for phone-to-PC task dispatch

---

## mcp_server (Model Context Protocol server — 31 tools)

- `server.py` — Main MCP server (stdio transport); imports tools/dispatch from `tool_registry.py`; Claude Desktop / Claude Code compatible
- `tool_registry.py` — MCP tool definitions and dispatch map
- `config.py` — Configuration loading from config.yaml and environment variables
- `logger.py` — JSON Lines structured logging to logs/kim_{date}.jsonl
- `os_utils.py` — Cross-platform OS detection and command translation (Windows / macOS / Linux)

**Tools:**
- `tools/files.py` — read_file, write_file, list_dir, delete_file (path validation vs PROJECT_ROOT)
- `tools/shell.py` — run_command, run_powershell (cross-platform via os_utils.translate_command)
- `tools/keyboard.py` — type_text, hotkey, key_press
- `tools/mouse.py` — click, double_click, right_click, drag, scroll
- `tools/windows.py` — get_windows, focus_window, resize_window (pygetwindow / osascript / wmctrl)
- `tools/screen.py` — take_screenshot, take_annotated_screenshot, get_screen_info
- `tools/screen_annotator.py` — Screenshot annotation with bounding boxes
- `tools/web.py` — web_open, web_observe, web_click, web_fill, web_press, web_text, web_screenshot, web_wait_for, web_wait_for_url, web_back, web_close, web_resolve
- `tools/web_element_scoring.py` — Element scoring helpers for web resolver
- `tools/web_observe_js.py` — DOM observation JavaScript blob
- `tools/codex_bridge.py` — Browser-backed Codex proxy bridge library
- `tools/git.py` — git_status, git_diff, git_add, git_commit, git_log, git_checkout
- `tools/code.py` — run_python, run_node, lint_file (ruff / flake8)
- `tools/search.py` — search_in_files (grep/ripgrep), find_files (glob)

---

## pythonExperimentTool/claw-code/rust/crates/kim-cli/src (Rust TUI CLI)

- `main.rs` — CLI entry point; non-blocking tokio event loop (current_thread); `App` struct with follow-mode scroll, mouse capture, streaming task spawn/cancel; `drain_events` / `apply_app_event` for channel-fed `AppEvent`s
- `ui.rs` — Ratatui rendering: header, body (chat + bottom-docked thinking panel), input, status, session browser, model picker, slash palette; bottom-anchored auto-scroll via `follow: bool` + `last_max_scroll: Cell<u16>`
- `thinking.rs` — Thinking visualization: braille/line/moon/block spinners, shimmer text, pulse dot, `TraceItem` enum (Thought / Tool / Plan), `draw_thinking_panel`
- `theme.rs` — `Theme` struct (Copy); two themes: `DarkNeovim`, `QuietLight`
- `provider.rs` — `AppEvent` enum; `stream_kim_request` (desktop bridge → streaming SSE); `stream_openai_compatible` (OpenAI/Ollama/Gemini/DeepSeek); `stream_anthropic` (Anthropic SSE); `ThinkParser` (<think> tag state machine)
- `commands.rs` — `/login`, `/provider`, `/model`, `/git`, `/run`, `/search`, `/files`, `/compact`, `/sessions`, `/help` and other slash commands
- `config.rs` — `KimConfig` load/save (provider, model, theme, API keys, base URLs)
- `sessions.rs` — Session discovery (`discover_sessions`, `discover_project_sessions`), `load_session_messages`, `find_session_by_id`

---

## relay_server (FastAPI relay — phone-to-PC task dispatch)

- `main.py` — FastAPI app: POST /prompt, GET /prompt/next, POST /result, GET /result/{task_id}, WS /ws, GET /status
- `auth.py` — API key middleware (phone_key vs pc_key)
- `queue.py` — SQLite task queue (tasks table)
- `models.py` — Pydantic request/response schemas

---

## extension (Chrome extension)

- `manifest.json` — Permissions and content_scripts for claude.ai, chatgpt.com, gemini.google.com, chat.deepseek.com
- `background.js` — Service worker: parses `## FILE:` / `## CMD:` blocks, POSTs to Kim bridge, relay status tracking
- `content_claude.js` — Claude.ai selectors and auto-loop logic
- `content_chatgpt.js` — ChatGPT.com selectors and auto-loop logic
- `content_gemini.js` — Gemini selectors and auto-loop logic
- `content_deepseek.js` — DeepSeek selectors and auto-loop logic
- `popup.js` — Popup UI: auto-loop toggle, relay/bridge connection status

---

## tray (System tray app)

- `app.py` — pystray tray icon + VoiceEngine init; spawns KimAgent; speaks on task done
- `ui.py` — Tkinter ControlPanel window: task input, provider switch, live log, voice toggle
- `settings.py` — Settings window: API keys, paths, preferences
- `voice.py` — `VoiceEngine`: dual-backend TTS (Kokoro local / HTTP API), `clean_for_speech()`, non-blocking playback via ThreadPoolExecutor

---

## Key architectural patterns

1. **MCP protocol** — All OS control flows through `mcp_server/server.py` over stdio
2. **Provider abstraction** — All LLM calls go through `BaseProvider`; unified `{"role","content"}` messages
3. **Cross-platform** — `os_utils.py` translates commands; platform-specific window backends
4. **Session persistence** — JSONL in `kim_sessions/` with per-session `.runs.json` sidecar for run history
5. **Context budgeting** — `ContextMeter` tracks input tokens; compaction activates at thresholds
6. **Screenshot lifecycle** — Auto-pruned from older messages to preserve token budget
7. **Browser provider** — Playwright CDP + persistent cookie storage (`sessions/chrome_data/`)
8. **Streaming TUI** — tokio single-thread runtime; `stream_kim_request` → `UnboundedSender<AppEvent>` → `drain_events` at 33ms cadence; `ThinkParser` routes `<think>` tags to trace panel
9. **Follow-mode scroll** — `App.follow: bool`; `last_max_scroll: Cell<u16>` published by `draw_chat` each frame; mouse wheel + arrow keys engage/disengage follow
