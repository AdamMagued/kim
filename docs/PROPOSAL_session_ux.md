# Proposal: Session & composer UX (Prompt 12 — K4, K5, K10)

> **Status:** done, then largely removed — implemented in commit c74c0c1, but most of it was
> subsequently deleted: `rename_session`/`set_session_pinned`/`search_sessions` (+ `write_session_meta`,
> `SearchHit`, `search_in_dir`) and the K5 `feedback.rs region_screenshot` in commit 36a67d9
> (2026-06-29); the K10 export (`desktop/src/export/runMarkdown.ts`) in commit 6da68b0;
> `delete_session` in commit 9f6371f (2026-07-06, roadmap A5/S6 dead-command decommission).
> What survives: the read-only `.meta.json` sidecar reader (`session_commands.rs:370
> read_session_meta`, consumed by `session_store.rs:318` — title override + pinned) and the K5
> composer attachment path (`ChatComposer.tsx` → `save_attachment`; `tests/test_attachment_payload.py`)
> — 2026-07-13

Scope: session management, paste/region capture, export.

## K4 — Session management
- **Meta sidecar**: `<kim_sessions>/<date>/<id>.meta.json` holding
  `{ "title": str, "pinned": bool }`. `session_commands.rs` read path is extended
  to merge meta into the listed session; absence = defaults.
- Tauri commands: `rename_session(id, date, title)`, `set_session_pinned(id, date,
  pinned)`, `delete_session(id, date)` (removes JSONL + summary + meta, with a
  frontend confirm), and `search_sessions(query)` — greps title + message content,
  caps at 50 results / ~200 ms, streams nothing fancy (returns the capped vec).
- Pinned sessions float to top in the sidebar.
- **Tests**: Rust unit tests for delete (files removed) and search (matches title
  and body, respects cap).

## K5 — Paste & region capture
- Composer `onPaste`: image items in `clipboardData` → base64 → existing
  `save_attachment` (Prompt 9 D4) → attachment chip.
- "Capture region" button: hide windows → interactive region screenshot
  (macOS `screencapture -i -x`; Linux `gnome-screenshot -a` / `slurp+grim`
  best-effort; Windows: disabled with tooltip) → attach.
- **Test**: end-to-end with the fake provider — an attachment reaches the
  provider payload (attachments → message content).

## K10 — Export run as Markdown
- Pure builder `buildRunMarkdown(run)` in `desktop/src/export/runMarkdown.ts`:
  user/assistant messages, collapsed activity as a bullet list, files touched,
  duration/cost. Action on the run pill + session ⋯ menu → clipboard + optional
  save dialog.
- **Test**: Vitest snapshot of the builder.

## Notes
- Delete is destructive → always confirm in UI; Rust command does the unlink.
- Search cap keeps the sidebar responsive on large histories.
