# Changelog

## Claw-style Compaction, WorkedForPill, Model Picker Polish (2026-05-18)

### New: Claw-style Local Compaction for API Providers

When a user types `/compact` with Ollama or any other API provider (Claude, OpenAI, etc.), Kim now uses the same deterministic, no-LLM compaction algorithm as the Claw binary instead of sending an extra LLM round-trip.

- **`orchestrator/compaction.py`** (new file): Python port of Claw's `compact.rs`. Summarises old messages locally — extracts user requests, tools used, key file paths, and a chronological timeline — then injects the result as a pinned system-level sentinel. The verbatim recent tail (last 6 messages by default) is always preserved. Tool-use/tool-result boundary protection walks back the split point to avoid orphaning a tool result without its paired tool call. Repeated compactions merge the old summary with the new one.
- **`orchestrator/agent.py`**: `_compact_and_reset_context()` now branches on `isinstance(self.provider, BrowserProvider)`. Browser providers keep the existing LLM-based compact path (with `_clear_chat_on_next_call`). All API providers (Ollama, Claude, OpenAI, etc.) use the new `_compact_api_provider()` method — no LLM call, no browser flags. The compact summary is injected at the top of the system prompt on each subsequent call so every provider (including Anthropic's API, which forbids system-role messages inside the messages array) receives it correctly.
- **`orchestrator/memory.py`**: New `compact_summary` property exposes the pinned sentinel text. `get_messages()` skips the sentinel (it goes into the system prompt instead). `_enforce_limits()` pins the sentinel at index 0 — it is never dropped by the sliding-window trim.
- **`desktop/src/types/index.ts`**: Added `'compact_summary'` to the `KimMessage.role` union so the Rust → TypeScript message boundary handles it without a type error.
- **`desktop/src/components/ChatView.tsx`**: `compact_summary` messages are filtered out at `setMessages()` time so they never appear as chat bubbles.

### New: WorkedForPill for Saved Ollama Sessions

Ollama chat histories (loaded from disk) now show the "Worked on this" activity disclosure pill before each assistant answer, matching the behaviour of live Claw sessions.

- **`desktop/src/components/kim-ui/WorkedForPill.tsx`** (new file): Standalone pill component that renders a collapsed "Worked for Ns" badge expandable into a list of tool-call trace rows.
- **`desktop/src/components/ChatView.tsx`**: Added `isIntermediateToolCall()` (detects assistant messages that are pure tool calls, not final answers) and `synthesizeExchangeActivity()` (walks the saved message list for a given user-message index and builds a `WorkedForTraceItem[]` from tool calls found between that turn and the next user message). The message renderer uses these to show `WorkedForPill` before final answers even when `runHistory` has no entry for the exchange.
- **`desktop/src/components/kim-ui/index.ts`**: Exports `WorkedForPill` and its types.

### Improvement: Ollama Model Picker — Toggle + Free-Text for Cloud Models

The Ollama model selector no longer forces every cloud model into a dropdown. Cloud mode now has a toggle between "pick from list" and "enter model name", making it easy to use any Ollama-hosted model without waiting for us to add it to the list.

- **`desktop/src/components/ProviderPicker.tsx`**: Replaced the single `<select>` with a mode toggle (pill buttons: list vs. custom). In list mode the dropdown is shown as before. In custom mode a text input appears pre-filled with the current model name; pressing Enter or clicking the checkmark confirms the selection. Toggle automatically collapses when switching to local mode.
- **`desktop/src-tauri/src/lib.rs`** (`known_ollama_cloud_models`): Expanded the cloud model list with Llama 3.1/3.3, Qwen 2.5 (including Coder), DeepSeek R1/V3/Coder-v4, Mistral Large, and Gemma 3 — all via Ollama's cloud routing suffix.
- **`desktop/src/index.css`**: Added styles for the model-picker toggle and custom-model input field.

### Fix: Ollama Operational Guidelines — Screenshot vs get_windows

The Ollama lean system prompt now explicitly tells the model to call `take_screenshot` rather than `get_windows` for screen awareness, and to use `observe_ui` before any click when exact coordinates are uncertain. This reduces wasted tool-call turns on vision-capable Ollama models.

---

## File Attachments, Blank Response Fixes & Agent Polish (2026-05-16)

### New: File Attachments & Drag-and-Drop

Users can now attach any file to a message before sending — no more copy-pasting content manually.

- **Drag-and-drop on composer** (`ChatView.tsx`): Drop any file onto the message composer to attach it. The composer shows a blue tint while a file is dragged over.
- **Paperclip button** (`ChatView.tsx`): Paperclip icon in the toolbar opens a file picker as an alternative to drag-and-drop.
- **File chips** (`ChatView.tsx`, `index.css`): Attached files appear as inline chips above the textarea showing a thumbnail (for images), filename, file size, and an × to remove. Multiple files can be attached at once.
- **Text file inlining** (`ChatView.tsx`): Text-based files (`.txt`, `.py`, `.js`, `.md`, etc.) are read and inlined into the message as fenced code blocks. The model sees the full file content.
- **Image handling** (`ChatView.tsx`, `lib.rs`): Images are saved to disk at `/tmp/kim_attachments/<filename>` via a new `save_attachment` Rust command, and the path is included in the message so the agent can reference the file. A thumbnail is shown in the chip.
- **Binary file notice** (`ChatView.tsx`): Unsupported binary files (e.g. `.zip`, `.exe`) show a toast explaining they can't be read, rather than silently failing.
- **Submit with attachments only** (`ChatView.tsx`): You can send a message that contains only attachments and no text — Kim will default to "Please look at the attached file(s)."
- **`save_attachment` Rust command** (`lib.rs`): New Tauri command that takes a filename + base64-encoded payload, validates the filename (strips path traversal), and writes the bytes to a temp directory. Returns the saved path.

### Bug Fix: Blank Response After Task Completion

Tasks whose summary contained noise words (like "screenshot", "traceback") were silently dropped and the chat appeared to go blank after completing.

- **Root cause** (`ChatView.tsx` → `parseLogLine`): `isNoiseLine()` was running before the `[SUCCESS]` check, so a line like `[SUCCESS] Captured a screenshot of the primary monitor.` was matched by the `'screenshot'` entry in `HIDDEN_SUBSTRINGS` and discarded before it could become a chat bubble.
- **Fix**: Moved `[SUCCESS]` and `[FAILED]` checks to the very top of `parseLogLine`, before any noise suppression runs. Also added `[SUCCESS]`, `[FAILED]`, and `[ERROR]` to the `isNoiseLine` whitelist as a second layer of protection. These lines now always reach `liveHistory` regardless of what words appear in the summary.

### Bug Fix: `task_complete` Called as MCP Tool

Some models (e.g. gpt-oss:20b) call `task_complete` as an MCP tool call instead of emitting `TASK_COMPLETE:` text, causing a wasted turn and an error shown to the user.

- **Fix** (`orchestrator/agent.py`): The agent now intercepts tool calls named `task_complete` or `TASK_COMPLETE` before dispatching to MCP. It extracts the summary from the tool arguments (`message`, `summary`, or `result` fields) and treats the call as a successful `TASK_COMPLETE:` completion, including voice playback and session flush.

### Bug Fix: Ollama Vision Models — Automatic Image Stripping on Error

Ollama models that don't support vision previously failed the entire request when images were in the message history.

- **Fix** (`orchestrator/providers/ollama.py`): The provider now catches vision-related errors and automatically retries the request with all images stripped from the message history. When an image is stripped, a text note is inserted in its place so the model knows an image was present. Vision support is cached per model name so subsequent calls skip images proactively without waiting for an error.

### Improvement: Main Window Restored After Screenshot

After Kim takes a screenshot to look at the screen, the main app window now automatically comes back so you can see the thinking panel and monitor what Kim is doing in real time.

- **Before**: Window was hidden when `[UI] SCREENSHOT_FLASH` fired and never re-shown until the task completed.
- **Fix** (`ChatView.tsx`, `agent.py`): `agent.py` emits `[UI] SHOW` after the screenshot is captured. `ChatView.tsx` now handles `[UI] SHOW` by calling `show_main_window`, restoring the window immediately after the capture without waiting for the task to finish.

### Improvement: ThinkingWithPlan — History Mode & Auto-Collapse

- **History mode** (`ThinkingWithPlan.tsx`): Component now accepts a `live` prop (default `true`). When `live=false` (used when displaying past runs), the pulse dot, shimmer animation, and fade-in effects are disabled. All trace items render at 80% opacity with a static dot — making it visually clear the run is historical, not active.
- **Auto-collapse on completion** (`ThinkingWithPlan.tsx`): The plan card now automatically collapses once every step is marked done, keeping the UI tidy after a task finishes without requiring the user to close it manually.
- **Target rendering fixed** (`ThinkingWithPlan.tsx`): Tool target text (the secondary label on each trace row) is now only rendered when a target exists, preventing spurious empty space in trace rows.

### Improvement: Agent Thinking & Planning Instructions

- **Thinking out loud** (`agent.py`, `browser_provider.py`): System prompts now explicitly instruct the model to write 1–2 sentences of plain text before each tool call narrating what it's about to do. These appear live in the Thinking panel and make Kim feel like a capable colleague working through a problem.
- **Plan protocol** (`agent.py`, `browser_provider.py`): Clearer PLAN/STEP/DONE format instructions so the live plan checklist in the UI stays accurate across providers. Models are told to emit the plan block on its own turn before any tool calls.
- **`task_complete` reminder** (`agent.py`): After bare text responses (no tool call, no `TASK_COMPLETE:`), the agent now injects a reminder telling the model to either call a tool or emit `TASK_COMPLETE:`, eliminating wasted turns where the model replies conversationally.

### New: Discord Feedback Webhook

- **`send_feedback` command** (`lib.rs`): Feedback submitted via the in-app button is POSTed to a private Discord webhook. The URL is embedded at compile time via `KIM_DISCORD_WEBHOOK` env var (`option_env!`) and is never exposed to the frontend or stored in source.
- **`TO_BE_DONE.md`**: Added a security roadmap describing how to proxy the webhook through a Cloudflare Worker before open-sourcing so the raw Discord URL can't be extracted from the binary.

---

## 47-Bug Fix Sweep (2026-05-11)

- **JS Variable Declarations** (`lib.rs`): Fixed implicit globals `waited` and `thumbnailFound` in `injectAttachments` that created silent bugs in strict mode.
- **Send Button Click** (`lib.rs`, `browser_provider.py`): Replaced triple-Enter shotgun with send button click (preferred) + single Enter fallback, preventing duplicate sends and unwanted newlines.
- **Response Scraping** (`browser_provider.py`): Increased idle threshold from 3→8 with 5s minimum wait, fixing the root cause of "returns old response" bug. Added visibility checks to `_find_selector`.
- **JSON Scanner** (`browser_provider.py`): Made string-aware (ignores braces inside JSON strings), guarded against negative depth, and made json5/json_repair imports safe.
- **Completion Hash Timing** (`lib.rs`): Moved `_lastHash` assignment to after submit confirmation. Added URL change observer to clear stale hashes on new chats.
- **Bridge Lock** (`lib.rs`): Moved `/v1/send` lock acquisition to before clipboard/window work, preventing concurrent sends from corrupting state.
- **Process-Aware Task State** (`lib.rs`): `is_bridge_task_running()` now checks if the PID is actually alive, auto-clearing stale PIDs.
- **Superseded Sends** (`lib.rs`): Superseded send() calls now emit error events instead of silently returning, preventing Rust-side timeouts.
- **Result Delivery** (`lib.rs`): Result retrieval now also cleans up `_sent` markers, progress entries, and hidden-state entries. Timeout path cleans leaked entries.
- **Context Loss Prevention** (`browser_provider.py`): System prompt now resets when conversation path changes within the same provider (e.g. `/chat/123` → `/chat/456`).
- **Process Group Kill** (`lib.rs`): Cancel now kills the process group (`-pid`) on Unix so child processes (Playwright, etc.) are also terminated.
- **Dead Code Removal** (`lib.rs`): Removed `extractBridgeTextField` (was returning empty string), broken `/v1/ping` image fallback. Fixed `checkReady` to use Gemini shadow DOM finder.
- **Monitor Position** (`lib.rs`): `show_browser_window_impl` now uses monitor origin offset for correct positioning on non-primary displays.
- **Browser Show** (`lib.rs`): `/v1/browser/show` now uses `show_browser_window_impl` instead of raw `win.show()`, preventing offscreen windows.
- **Docstring Fix** (`browser_provider.py`): `_format_prompt` docstring now correctly documents 3-tuple return.
- **Bridge Version Bump**: Persistent bridge version bumped to v9.

## Browser Bridge Stability & Game Implementation (2026-05-11)

- **Typography Normalization** (`browser_provider.py`, `lib.rs`): Fixed the "Prompt changed after injection" error. Rich-text editors (like Gemini) automatically format pasted text (converting straight quotes to smart quotes, double-dashes to em-dashes, and ellipses). Both the Python and Rust layers now normalize these typographic characters before verification.
- **Fuzzy Boundary Matching** (`browser_provider.py`, `lib.rs`): Added a fuzzy match fallback for prefix and suffix comparisons that strips non-word characters, ensuring that minor typography discrepancies do not break the injection loop.
- **Race Condition Fix** (`browser_provider.py`): Resolved a critical race condition where slow browser response times (TTFT) caused the orchestrator to scrape the previous conversation turn. The bridge now strictly waits for a new response element to appear before starting the generation-complete polling loop.
- **Reasoning JSON Extraction** (`claw_bridge.py`): Fixed a bug where truncated or malformed JSON payloads leaked into the Activity Feed. Added aggressive regex to strip structural JSON brackets (`{"text": "`), ensuring only natural language reasoning reaches the user.
- **Technical Log Suppression** (`claw_bridge.py`): Changed the default completion message in `run_claw_subtask` from exposing internal loop counts and exit codes to a clean, user-friendly "Task completed successfully."
- **Game Features** (`pong.html`, `tower_sim.html`): Implemented a neon-styled CPU version of Pong with Easy/Medium/Hard difficulties, and a new physics simulation featuring dropping balls navigating through rotating platforms.


## Chat Persistence & Display Polish (2026-05-11)

- **Chat Reset Fix** (`App.tsx`): Fixed a critical bug where completing a task in the Code tab caused the UI to navigate away from the current session and wipe the `liveHistory`, resulting in a blank screen and lost conversation history. The old session now correctly stays active.
- **Chat Reset Prevention** (`ChatView.tsx`): Removed the Claw-mode exclusion so assistant responses are correctly captured in `liveHistory`. Added a guard to prevent `liveHistory` from being wiped on silent same-session reloads unless disk messages actually grew.
- **"Worked for X" Badges** (`ChatView.tsx`): Fixed a bug where old Claw sessions loaded from disk always showed "Worked on this" (duration 0). The `runHistory` data is now correctly merged into the `clawRuns` entries to restore accurate elapsed times for past tasks.
- **Technical Output Filtering** (`ChatView.tsx`): Added aggressive noise filtering for Claw bridge internals (`Claw completed`, `LLM calls`, `bridge_request`) and provider leaks (`sending to gemini/claude/chatgpt`, `Routing Claw`).
- **Clean Task Completion** (`run_claw_bridge.py`, `ChatView.tsx`): Stripped the technical "Claw completed (X LLM calls, exit code 0)" stdout message and replaced it with a clean, user-friendly "Task completed".
- **Clean Reasoning Output** (`claw_bridge.py`): Enhanced `_surface_bridge_reasoning` to strip JSON wrapping, drop pure JSON fragments, replace provider brand names with "Kim", and remove technical prefixes (e.g., "Calling tool_name.").
- **Clean Status Messages** (`run_claw_bridge.py`, `claw_bridge.py`): Replaced technical status messages (like "Routing Claw through Kim's browser provider" or "Browser response format was invalid") with clean, polished user-facing messages (like "Kim is working on your task…" and "Kim is refining its response…").
- **Provider Brand Rewriting** (`ChatView.tsx`): Expanded regex rewriting to catch more patterns (e.g., `{Provider} is/says/returned` → `Kim`, `sending to {Provider}` → `Kim is working`) and added DeepSeek to the clean-thinking regex to ensure all internal logs appear to come natively from Kim.


## Bug fixes — binary discovery, status visibility (2026-05-09)

- **Claw binary discovery** (`lib.rs find_claw_binary`): now searches both `<kim_root>/pythonExperimentTool/…` (nested layout where pythonExperimentTool lives inside kim-pro) **and** `<kim_root.parent()>/pythonExperimentTool/…` (sibling layout). Previously only the sibling path was checked, causing "Claw binary not found" when pythonExperimentTool is inside kim-pro. Users no longer need to set `CLAW_BIN` manually.
- **Removed noisy interpreter-path message** (`lib.rs`): the `[STATUS] Routing to Kim (/path/to/venv/bin/python interpreter)` line that was being shown in the activity feed is now gone — it exposed internal implementation details with no user value.
- **Status messages now visible during headless tasks** (`ChatView.tsx`): browser_provider STATUS messages arrive on stderr with a Python log prefix (`[INFO] …: [STATUS] …`). `isNoiseLine` now protects any line containing `[STATUS]` from being filtered. `parseLogLine` now also detects `[STATUS]` embedded inside stderr log records. This fixes the "nothing happens" appearance when Kim is routing through the browser provider in headless mode.
- **Immediate task-start feedback** (`agent.py`): `KimAgent.run()` now prints `[STATUS] Kim is working on it…` to stdout at the very start of every task, so the activity feed is never blank while the provider is loading.

## Claw via Browser Provider (no API key)

Wired up the long-dormant file-bridge path so Code-tab tasks can run Claw without an Anthropic API key — they relay through Kim's logged-in browser session instead.

- `pythonExperimentTool/claw-code/rust/crates/rusty-claude-cli/src/file_bridge.rs`: `FileBridgeClient::new` now honors the `CLAW_BRIDGE_DIR` env var (falls back to `/tmp/claw_bridge`). Two concurrent Claw runs no longer clobber each other on `bridge_request.json`. Bridge timeout extended from 5 → 10 minutes for browser-scrape latency. Rebuilt release binary.
- `orchestrator/run_claw_bridge.py` (NEW): one-shot Python entrypoint. Takes `--task`, `--cwd`, `--provider`. Builds a `BrowserProvider`, calls the existing `mcp_server.tools.claw_bridge.run_claw_subtask` to spawn Claw with `CLAW_FILE_BRIDGE=1` and relay every LLM request through the browser. Honors `CLAW_BIN` env var.
- `desktop/src-tauri/src/lib.rs send_task`: Code-tab branch now picks between
  - **Browser-bridge mode** (provider starts with `browser`): spawns `python -m orchestrator.run_claw_bridge --task … --cwd … --provider browser:gemini` instead of Claw directly.
  - **Direct API mode** (any other provider): spawns Claw with `ANTHROPIC_API_KEY` from `.env` (existing behavior).
  The pre-flight error for direct mode now points users back to "Browser" if they don't have a key.
- Chrome CDP launch now triggers for browser-bridge Claw runs too (was previously gated on `!is_claw`), so the BrowserProvider has a CDP target to attach to.
- Webview-bridge env (`KIM_WEBVIEW_BRIDGE_URL`, `KIM_WEBVIEW_BRIDGE_TOKEN`) is forwarded to the Python relay so the in-app sign-in window remains drivable.
- Activity feed shows `[STATUS] Routing to Claw via Kim's browser provider (browser:gemini)` on bridge runs and `[STATUS] Routing to Claw (direct Anthropic API)` on direct runs.

## Browser Provider Lifecycle

- Added stronger lifecycle guards for the in-app browser provider so active tasks keep the existing provider chat instead of navigating or replacing it.
- Removed mid-task navigation/reload fallbacks that could reset Gemini, Claude, or ChatGPT to a fresh chat.
- Preserved Gemini conversation URLs during active tasks instead of rewriting them to account-selection URLs.
- Hid the provider webview offscreen during normal sends, including the legacy completion fallback, so it does not steal focus or cover the user’s target app.
- Added a debug-visible browser setting for testing provider behavior without making that the default.
- Removed broken Google account switching UI and background Google account scraping.

## Structured UI Observation

- Added `observe_ui` and `click_ui` MCP tools for fast macOS Accessibility-based UI inspection and interaction.
- Updated Kim’s system prompt to prefer structured UI tools for normal desktop tasks and reserve screenshots for visual inspection.
- Added guidance so browser-provider models do not claim they lack access to the Mac when local tools are available.

## Controlled Web Browser Tools

- Added Playwright-backed `web_*` MCP tools:
  - `web_open`
  - `web_observe`
  - `web_click`
  - `web_fill`
  - `web_press`
  - `web_text`
  - `web_screenshot`
  - `web_wait_for`
  - `web_back`
  - `web_close`
- Routed `open_url` through the controllable web browser path.
- Made the dedicated Kim browser persistent across task/MCP process lifetimes by launching it as a detached Chrome process with Kim’s own profile and CDP port.
- Kept `web_close` non-destructive so browser sessions, tabs, and logins remain available for later tasks.
- Added secure Basic Auth handling through `username` and `password` arguments instead of credentials embedded in URLs.
- Reworked auth states so blocked pages return `AUTH_REQUIRED` or `AUTH_FAILED` instead of fake success.
- Added `chrome-error://` detection so Kim does not treat browser error pages as successfully opened content.

## Connector UI Scaffold

- Added a top-right `Connectors` button in the chat pane.
- Added a scrollable connectors side panel with search.
- Added placeholder connector cards for:
  - GUC CMS
  - GUC Mail
- Left connector sign-in and enable toggles disabled until connector auth and MCP tool injection are implemented.

## Desktop UI And Settings

- Added browser-provider picker updates and Gemini URL normalization to open Gemini at `/app`.
- Added queue/voice light-mode toggle styling fixes from the branch work.
- Added UI changes for the connector drawer and browser visibility testing.
- Updated Tauri capabilities for the browser bridge.

## Topbar And App Layout

- Restructured the root layout to a sidebar/main row, with the sidebar running edge-to-edge (Codex-style) and the macOS traffic-lights cluster getting proper breathing room.
- Moved the active chat title and the Code session badge into the topbar (`kim-topbar__title`).
- Replaced the duplicate top-of-app `Kim` wordmark with the K logo and version label inside the sidebar brand strip.
- Added a `Summarize` / `Re-summarize` button next to the chat title that calls the `summarize_session` Tauri command on demand.
- Removed the in-page summary block that previously appeared above the conversation.
- Replaced the `Light / Auto / Dark` theme toggle on the top with a compact `Connectors` button (4-dot accent grid). Connectors logic is unchanged — the trigger now lives in the topbar, dispatches a `kim-open-connectors` window event, and the panel still renders inside the chat pane.
- Tightened greeting typography so the Syne descender (`g`) no longer clips.

## Sidebar

- Made the sidebar resizable: a 5px handle on the right edge supports drag-to-resize between 200px and 420px, persists to `localStorage`, and double-click restores the default width.
- Replaced the bottom Settings button with an account dropdown (`kim-account-trigger`) showing Settings, Get help, Upgrade plan, Learn more, and Log out, anchored bottom-left.
- Added pin/delete via right-click on a Kim session: the menu is now portaled to `document.body` so it can paint above the chat pane’s stacking context (previously it was clipped/covered by `.kim-chat`).
- Pinned chats are persisted in `localStorage` (`kim-pinned-sessions`) and sorted to the top of the list.
- Single-item delete from the right-click menu reuses the existing two-step confirmation modal.

## Activity Feed — Claw Tool Visibility

- **`claw_bridge.py`**: After each browser-LLM relay turn, the relay loop now emits `[TOOL] tool_name({"path": "…"})` lines to stdout for every tool call in the response. ChatView's `TOOL_MAP` picks these up and renders them as meaningful activity items (`Writing \`index.html\``, `Running \`npm install\``, etc.) instead of raw bridge-communication noise.
- **`ChatView.tsx` TOOL_MAP**: Added Claw's tool names — `bash`, `grep_search`, `glob_search`, `list_files` — so they display with descriptive labels.
- **`ChatView.tsx` setActivity**: Consecutive `status`-kind items now collapse: the previous status line is replaced rather than appended, so repeated "gemini is thinking…" / "Sending message to gemini…" entries no longer flood the feed. A new distinct status replaces the old one; a non-status item (`tool`, `error`, etc.) still appends normally.

## Chat UX

- Added a live `Thinking…` header on the active assistant turn with a pulsing dot and an elapsed timer that updates every second.
- On task completion, the activity feed collapses into a `Worked for Xm Ys` chip; clicking the chip expands the panel and replays the full activity transcript for that turn.
- Persisted run-history snapshots to a `<session_id>.runs.json` sidecar file alongside each session’s JSONL, so the Worked-for chips survive chat reloads. Loaded via the new `load_run_history` Tauri command and saved on each task completion via `save_run_history`.
- Added inline edit on user messages: hovering reveals a pencil button, clicking it converts the bubble into a textarea, and confirming truncates the live history at that point, cancels any running task, and resends the edited text as a new task. `Enter` confirms, `Shift+Enter` adds a newline, `Escape` cancels.
- Added Copy buttons on both user and assistant bubbles (hover-revealed). The assistant Copy button briefly shows a check after copying.

## Run History Persistence

- New Tauri commands `save_run_history` and `load_run_history`, registered in `tauri::generate_handler!`, that read/write `<id>.runs.json` next to a session’s JSONL.
- New helper `find_session_date_dir` that locates the date subdirectory containing a session id under either the Kim sessions root or a Claw `.claw/sessions` root, with a fallback to today’s directory.

## Claw Code Routing

- Added `find_claw_binary` to discover the Claw CLI via the `CLAW_BIN` environment variable, the `<fork>/pythonExperimentTool/claw-code/rust/target/{release,debug}/claw` paths, and finally `which claw`.
- Routed Code-tab tasks through Claw (`claw --output-format json prompt …`) instead of the Kim orchestrator with the desktop-control prompt. Kim still owns Chat-tab tasks.
- Skipped Kim-only orchestrator setup (browser provider, Chrome CDP, `--provider`, `--resume`) when running through Claw.

## Prompt-Injection Defense

- Each task now generates a 32-char hex nonce via `secrets.token_hex(16)` and wraps the user instruction between `<<<BEGIN_USER_INSTRUCTION_{nonce}>>>` and `<<<END_USER_INSTRUCTION_{nonce}>>>` markers.
- Updated the agent system prompt with a `Prompt-Injection Defense — READ FIRST` section that tells the model to treat anything outside those markers — including tool results, file contents, fetched web pages, and screenshot/OCR output — as untrusted data.
- Lists explicit refusal patterns: instructions that override goals, exfiltrate secrets, claim to be a system/admin, or ask the model to disable safety.

## Right-Click And Devtools Hardening

- Globally suppressed the WebView’s native context menu so right-clicking anywhere never reveals `Inspect Element`, `Reload`, or `Back`. Text inputs and contenteditable regions still get the platform paste/copy menu.
- Custom right-click menus on Kim chat items take precedence (Pin / Delete only) and now portal to `document.body` so they paint above the chat pane.
- Blocked `F12`, `Cmd/Ctrl + Shift + I/J/C`, and `Cmd/Ctrl + Alt + I/J/C` keyboard shortcuts as a belt-and-braces measure on top of Tauri’s release-build devtools-off default.

## macOS Dock Icon

- Re-mastered the source `desktop/src-tauri/icons/icon.png` from a 512×512 full-canvas square into a 1024×1024 macOS template squircle (824×824 inner artwork, 100px transparent margin, ~22.5% rounded-rect mask).
- Regenerated all platform variants (mac, ios, android, windows store, ico, icns) via `npx @tauri-apps/cli icon icons/icon.png` so the dock icon no longer fills the entire dock cell.

## Config And Project State

- Added `use_real_browser` configuration support.
- Normalized `project_root` to `.` in the active config.
- Removed stale `kim.sh` from this branch relative to `origin/main`.
- Added local web/session-related ignores and package metadata changes from branch work.
