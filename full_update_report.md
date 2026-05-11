# Comprehensive `main` Branch Update Report (2026-05-11)

This report details *every major system* that was merged into `main` over the 23 commits from the `fix/observe-ui-and-cancel` branch. The update includes a staggering **319 files changed, with 98,674 insertions and 1,194 deletions**.

---

## 1. The Massive Rust Engine Port (`pythonExperimentTool/claw-code/rust`)
This accounts for the vast majority of the 98k insertions. The core Claw backend engine has been fully ported to Rust for massive performance gains.

*   **`crates/api`**: Native Rust providers for Anthropic, OpenAI, DeepSeek, and Google GenAI.
*   **`crates/runtime`**: Full execution registry, async worker boot sequences, team cron registries, and trust resolvers.
*   **`crates/rusty-claude-cli`**: The new `claw` binary entrypoint, replacing the old Python CLI. Includes the new `file_bridge.rs` which Kim uses to proxy browser LLM requests.
*   **`crates/tools`**: Native Rust implementations for `bash`, `read_file`, `write_file`, `glob_search`, `grep_search`, `pdf_extract`, and `lane_completion`.
*   **Legacy Python Cleanup**: Introduced `pythonExperimentTool/claw-code/src/parity_audit.py` and extensive test harnesses to ensure the new Rust binary has 100% feature parity with the old Python engine.

## 2. Desktop UI & Chat Persistence (`desktop/src/`)
A complete overhaul of the Kim Chat and Code tabs.

*   **`App.tsx` & `ChatView.tsx`**: 
    *   Fixed the catastrophic "Chat Reset" bug where Claw Code-tab tasks would wipe the UI and create a blank screen.
    *   Introduced `liveHistory` syncing so the user sees a seamless transition from their prompt to the agent's work.
    *   Implemented "Worked for X" duration tracking for older Claw sessions loaded directly from disk.
*   **`Sidebar.tsx`**: Made the sidebar fully resizable (persisted to `localStorage`), added a modern account dropdown trigger, and built right-click context menus that portal safely above the chat frame.
*   **`MessageBubble.tsx`**: Added an inline edit button (pencil) that allows users to seamlessly edit a past message, truncate the timeline, and auto-resend. Added copy-to-clipboard functionality.
*   **`ToolCallCard.tsx`**: Now parses and safely displays native `bash`, `grep`, and `write` tool calls directly from the Rust Claw binary.

## 3. Tauri Rust Backend (`desktop/src-tauri/`)
*   **`lib.rs`**: 
    *   **Binary Discovery**: Added intelligent traversal to find the `claw` binary in both sibling and nested directory layouts.
    *   **Run History**: Added `save_run_history` and `load_run_history` to save the activity feed of a task into a `.runs.json` sidecar file, allowing "Worked for X" to survive app reloads.
    *   **Routing**: Decoupled the Code tab. It now natively spawns `claw` via the shell, either directly (if an Anthropic API key exists) or through the new Python file bridge.

## 4. The Python Orchestrator & File Bridge (`orchestrator/` & `mcp_server/`)
*   **`orchestrator/run_claw_bridge.py` & `run_claw_relay.py`**: Brand new scripts that run the Claw Rust binary, intercept its LLM calls via a file-based bridge (`bridge_request.json`), and proxy them through Kim's authenticated Browser Provider. This allows users to run the Code tab *without an API key*.
*   **`mcp_server/tools/claw_bridge.py`**: Contains the complex retry-logic and prompt-injection defense required to make Gemini/ChatGPT output strict JSON tool calls that the Rust binary can understand.
*   **Noise Scrubbing**: Aggressively strips out technical artifacts (e.g., `[SUCCESS] Claw completed (1 LLM calls, exit code 0)`) and replaces provider branding (e.g., "sending to gemini") with clean, native "Kim is thinking" UI text.

## 5. Web & Structured UI Tools
*   **Playwright Automation**: Added a full suite of web-automation tools (`web_click`, `web_fill`, `web_screenshot`) utilizing a persistent, detached Chrome profile.
*   **Structured UI**: Added `observe_ui` and `click_ui` tools leveraging native macOS Accessibility APIs for sub-second interface traversal, replacing slow, token-heavy screenshot OCR.

## 6. App Infrastructure
*   **`CHANGELOG.md` & `KIM_PROJECT_KNOWLEDGE_BASE.md`**: Created massive, comprehensive documentation covering architectural decisions, subsystems, and the changelog history.
*   **Icons**: Remastered the macOS dock icons from full-bleed squares to native Apple squircle templates across all DPI resolutions.
