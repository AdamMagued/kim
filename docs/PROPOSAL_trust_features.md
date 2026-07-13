# Proposal: Trust & control features (Prompt 10 — K1, K3, K6, K9)

> **Status:** done, with the K1 revert UI path since removed — implemented in commit 673a692.
> Live today: K1 pre-image *capture* (`mcp_server/tools/files.py:37,123` →
> `checkpoints.backup_pre_image`), K3 steering inbox (`orchestrator/agent.py:211`), K9 privacy
> pause (`session_commands.rs` `set_privacy_pause`/`get_privacy_pause`, registered in `lib.rs`).
> Removed: the Rust `revert_run`/`has_checkpoint` Tauri commands were deleted in commit 36a67d9
> (2026-06-29), so Python `checkpoints.revert_run()` has NO caller path from the UI — revert is
> currently unreachable (only vestigial `lastRunId` state remains, `useChatStream.ts:155`)
> — 2026-07-13

Scope: run revert, mid-run steering, approval previews, privacy pause.

## K1 — Run checkpoints + revert
- **Capture**: `mcp_server/tools/files.py` `write_file`/`edit` back up the *pre-image*
  of each path it touches to `~/.kim/checkpoints/<run-id>/` before writing. New files
  are recorded as a tombstone (so revert deletes them). Per-run cap **50 MB**; once
  exceeded, stop backing up and record a `truncated` marker.
- **Run id**: the env var `KIM_RUN_ID` exported by the Rust spawn (`subprocess.rs`),
  falling back to a timestamp when run standalone.
- **Restore**: new Tauri command `revert_run(run_id)` in `session_commands.rs` →
  shells a small Python helper (`session_store`/new `checkpoints.py`) that, for each
  recorded path, first writes the *current* state to `<path>.kim-revert.bak` (revert is
  itself undoable), then restores the pre-image or deletes tombstoned new files.
- **UI**: "Revert changes" action on the run pill when `~/.kim/checkpoints/<run-id>`
  exists.
- **Tests**: Python round-trip — edit→backup→restore; new-file→tombstone→delete; cap.

## K3 — Mid-run steering
- Typing while a run is active offers **Steer** (default) vs **Queue** (existing B1).
- Steer writes `{"type":"user_steer","text":...}` to the agent stdin — extends the
  existing HITL stdin JSON channel (`subprocess.rs` `hitl_stdin` + Python stdin reader).
- The agent injects the text as a user message **before its next LLM call** and emits
  `[STATUS] steering noted`.
- **Test**: a steer line on stdin → the text lands in the next request payload.

## K6 — Approval previews
- Extend the Python `kim:hitl-approval-request` emit with a `preview` string:
  `run_command` → the command; `write_file`/`edit` → unified diff ≤40 lines; web →
  URL + element label.
- Schema: add `preview` to `events.schema.json`, regenerate TS types
  (`npm run gen:events`). Render monospace in the approval card.

## K9 — Privacy pause
- Global flag: tray item + composer eye icon → `set_privacy_pause(on)` Tauri command →
  writes a flag the MCP server reads (env-file `~/.kim/privacy_pause` sentinel).
- While paused, `take_screenshot` / `screen` / `web_screenshot` return a typed error
  `{"error":"privacy_pause","message":"Privacy pause is on"}`; the agent is told to
  inform the user instead of looping.
- **Test**: pause on → screenshot tool returns the typed error.

## Risks
- Checkpoint disk growth → 50 MB/run cap + revert sweeps old `.bak`.
- Steering races the LLM call → injected only at the loop's message-assembly point.
- Privacy flag is a local sentinel file, not a security boundary — documented as such.
