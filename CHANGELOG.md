# Changelog

Changes on `fix/observe-ui-and-cancel` compared with `origin/main` as of 2026-05-09.

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

## Config And Project State

- Added `use_real_browser` configuration support.
- Normalized `project_root` to `.` in the active config.
- Removed stale `kim.sh` from this branch relative to `origin/main`.
- Added local web/session-related ignores and package metadata changes from branch work.

