# Proposal: Speed & access (Prompt 11 — K2, K7, K8)

Status: accepted · Scope: quick-ask overlay, tray, command palette.

## K8 — Command palette (the shared core)
- New `desktop/src/actions/registry.ts`: a single source of truth for app
  actions — `{ id, title, keywords, group, run }`. Palette, global shortcut, and
  tray all consume this registry instead of duplicating invoke calls.
- Actions: new chat, new code session, switch session (fuzzy), switch provider,
  toggle mode, cancel run, open settings (panes), privacy pause toggle.
- `filterActions(query)` does fuzzy/substring ranking — **unit-tested** (Vitest).
- ⌘K / Ctrl+K opens an in-app palette overlay listing filtered actions.

## K2 — Quick-ask overlay
- `tauri-plugin-global-shortcut` (default ⌥Space / Alt+Space, rebindable in
  Settings → System). Toggles a small frameless always-on-top WebviewWindow with
  one composer input; submit routes to the active/newest chat session via the
  existing send path. Esc hides. Focuses the main window only when the run needs
  attention (HITL/need-help).
- Registration failure (hotkey taken) → toast + Settings link, never a crash.

## K7 — Tray
- `tauri` `tray-icon` feature. Menu: status line (idle / "Running: <task 40ch>"),
  Cancel current run, last 3 sessions, Quick ask, Privacy pause toggle (K9),
  Quit. Status driven from the Rust running-task state (TaskState), not frontend.

## Verifiability
- K8 registry: Vitest. K2/K7 are OS integrations — CI can't exercise global
  shortcuts or the tray, so the report carries a manual verification checklist of
  exactly what was confirmed by hand (or, on this headless/disk-constrained box,
  what could not be and why).
