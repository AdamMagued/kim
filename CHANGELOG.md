# Changelog

Changes on `fix/observe-ui-and-cancel` compared with `origin/main` as of 2026-05-11.

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
