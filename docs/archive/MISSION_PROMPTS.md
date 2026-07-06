# MISSION_PROMPTS — run these in order, one per agent session

Usage: tell your agent **"Open MISSION_PROMPTS.md and execute Prompt N exactly."**
Each prompt is self-contained for a zero-context agent.
**Run order: 13 → 1 → 2 → 3 → 4 → 5 → 6 → 9 → 10 → 11 → 12 → 7 → 8**
(13 is a SECURITY batch — do it first; 1 and 2 are independent and may run in parallel
worktrees; 10–12 are UX feature batches from PRODUCTION_ROADMAP Part II-K; 7 is the big
CLI milestone; 8 is release prep, always last. 11 and 12 are independent of each other.)
After each prompt finishes, a human (or the reviewing agent) verifies before starting
the next.

---

## GLOBAL RULES (apply to every prompt below — read first, always)

- Repo root is `kim-pro/`. Never touch files outside it (not in git, unrecoverable).
- Read root `CLAUDE.md` (standing constraints) and `HOW_TO.md` before editing.
- Bug IDs (A1, B5, …) refer to `DEEP_DIVE_AUDIT.md` at the repo root — read the full
  entry for every ID in your prompt before writing any code.
- Work on a fresh branch off `main` named `fix/<prompt-slug>`. One commit per bug ID
  (prefix the message with the ID). Do NOT merge to main — push the branch, report, stop.
- Every fix ships WITH a test that fails before / passes after. Never weaken or delete
  an existing test to get green.
- Before every commit: run the affected suite. Before push: all four suites —
  `python -m pytest tests/ -q` (venv) · `cd desktop && npx tsc --noEmit && npm run test`
  · `cd desktop/src-tauri && cargo test` · `cd cli && cargo test`.
- **After every push: confirm the remote CI run is green**
  (`gh run list --branch <branch> --limit 1`). Local green is not done.
- Final message: per-ID status (FIXED/BLOCKED + commit hash), test counts for all four
  suites, CI run conclusion, anything a reviewer should double-check. Claims without
  evidence count as not done.

---

## Prompt 1 — CLI P0 batch: persistence, swallowed errors, panic, resume hint

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **A1, A2, A3, A5, A7**
> in `cli/src/`. Specifics:
> **A1+A2** (`main.rs stream_repl_turn`): delete the vestigial `is_local_agent`
> reload-from-file branch entirely. Unconditionally: push the user message → save session
> → stream → if assistant text is non-empty, push it to `app.messages` and save again.
> The existing else-branch is the correct logic; make it the only path. Add a no-network
> regression test: drive `stream_repl_turn` with a stubbed event channel (refactor the
> mpsc receiver into an injectable function if needed) and assert (a) the session file
> exists in a temp `~/.kim/sessions` override, (b) `app.messages` contains the assistant
> reply, (c) a resumed session + new turn preserves both old and new messages.
> **A3** (`provider.rs`): in `process_openai_sse_line`, handle `{"error": ...}` payloads
> (object with `message`, or bare string) → send `AppEvent::Err`. Same for Anthropic
> `type:"error"` SSE events in `process_anthropic_sse_line`. Unit tests with literal
> error lines from ollama and Anthropic.
> **A5** (`provider.rs` ×2): replace `&trimmed[..300]` with a char-boundary-safe
> truncation helper. Test with a string whose byte 300 is mid-emoji.
> **A7** (`main.rs` main(), Repl arm): only print the `kim --resume <id>` hint when
> `~/.kim/sessions/<id>.jsonl` actually exists.
> Verification beyond tests: build the binary and run a scripted 2-turn REPL via stdin
> against local ollama if available; show that a session file is created and turn 2's
> context includes the assistant reply. If ollama is unavailable, say so explicitly.

## Prompt 2 — Frontend P0/P1 batch: queue, error UX, cost meter, state hygiene

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **B1, B2, B3, B4, B5,
> B6, B7** in `desktop/src/`. Specifics:
> **B1** (`hooks/useTaskRunner.ts`): make the queue real — when a run completes
> (`kim-agent-done` / isRunning→false) and `queuedTasks` is non-empty, dequeue the head
> and `runPendingTask` it. Decide and implement ONE of: drain-on-done (preferred) or
> remove the queue UI + toast entirely. Delete the dead `interruptTask` branches and the
> hardcoded `queueEnabled` consts (also in `ChatComposer.tsx`). Vitest: submit-while-running
> → run completes → queued task starts.
> **B2** (`StreamRenderer.tsx` + `ChatView.tsx`): pass the real `liveHistory` index
> through `collapseMessages` (extend its return to carry the source index), fix the
> in-place mutation in `handleEditLiveMessage` (clone the message object), and make edit
> functional: editing a user message truncates liveHistory after it and resends the
> edited text as a new task (Claude-style). If resend is out of scope, remove the edit
> affordance instead — no cosmetic-only edit.
> **B3** (`StreamRenderer.tsx` session branch): special-case `taskError === 'agent-error'`
> with the same friendly message used in the new-chat branch.
> **B4**: render `runFailure` and `rateLimitedState` in the session branch too; when
> `runFailure` is set, suppress the redundant `taskError` banner for the same run.
> **B5** (`components/chat/utils.ts`): `estimateCostUsd` — normalize provider (strip
> `browser:*` → `browser`); store the provider used per run alongside runHistory and
> price with THAT, not the currently selected provider. NOTE: runHistory is persisted
> via `save_run_history` — adding a provider field changes the saved shape; loading
> runs saved without it must not break (default to null → hide the cost chip).
> Vitest for `browser:claude` → $0.
> **B6** (`ChatView.tsx` newChatMode effect): also clear liveHistory, runFailure,
> rateLimitedState, hitlApprovalStatus, lastFailedTask.
> **B7** (`hooks/useChatStream.ts` kim-agent-done handler): clear `hitlApprovalStatus`.

## Prompt 3 — CLI UX batch: cancel, harmony, doctor, quoting

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **A4, A6, A8, A10,
> A11, A16** in `cli/src/`. Specifics:
> **A6** (`main.rs stream_repl_turn`): make Ctrl-C cancel the current generation instead
> of killing the CLI — `tokio::select!` over `rx.recv()` and `tokio::signal::ctrl_c()`;
> on signal, abort the spawned task's JoinHandle (kill_on_drop reaps subprocesses), print
> a dim "(cancelled)" note, save whatever assistant text already streamed, return to the
> prompt. TEST INTERACTIVELY (spawn the binary, send SIGINT mid-stream against ollama)
> and verify rustyline's own double-Ctrl-C-to-exit still works afterwards — if the two
> SIGINT handlers conflict, document the conflict and gate the feature off rather than
> shipping a broken exit path.
> **A4** (`provider.rs ThinkParser`): treat the literal token `assistantfinal` as a
> channel boundary — held-back text before it flushes as ThoughtChunk, the marker is
> swallowed, text after streams as the answer. Raise the Normal-state tail-hold window
> from 6 to 14 chars so the marker can't be split across flushes. Unit tests: marker
> mid-chunk, marker split across two feeds, no marker (unchanged behavior).
> **A8** (`commands.rs doctor`): when provider is ollama, fetch `/api/tags` and warn if
> `config.model` is not in the list AND not a known cloud model. For key providers, note
> whether the configured model appears in `model_options()`.
> **A10** (`commands.rs run_project_command`): use `split_shellish_tokens` (already in
> main.rs — move it to a shared module) so `/git commit -m "two words"` works.
> **A11**: replace the magic-string sentinels (`"Conversation cleared."`,
> `"__KIM_REFRESH_SESSIONS__"`, `"__KIM_RESUME_SESSION__:"`) with real `CommandOutcome`
> variants (`ClearConversation`, `OpenSessionPicker`, `ResumeSession(String)`); update
> `handle_repl_message` accordingly.
> **A16**: consolidate the two divergent `trim_base_url` implementations into one shared
> function with tests covering trailing-slash and `/v1/` inputs.

## Prompt 4 — Installer batch: prebuilt-first, Python provisioning, root collision

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **A14, A15, C1, D1**
> plus A9's documentation. Specifics:
> **D1** (`desktop/src-tauri/src/subprocess.rs find_python_interpreter`): `~/.kim_root`
> is a FILE containing the checkout path, not a directory — delete the impossible
> `~/.kim_root/venv/...` candidates and instead READ the file, then probe
> `<contents>/venv/bin/python` (+ `.venv`, + Windows `Scripts\python.exe`). Unit-test
> with a temp home: file → venv resolution; file pointing at a dir without venv → falls
> through; no file → unchanged behavior. This is what makes A15's provisioned venv
> actually get used by the desktop app.
> **A14** (`cli/install.sh`): before falling back to `cargo build`, try downloading the
> prebuilt `kim` binary from the latest GitHub release
> (`https://github.com/AdamMagued/kim/releases` — assets are named
> `kim-cli-<version>-<suffix>` per release.yml line ~157, suffixes: macos-aarch64,
> macos-x86_64, linux-x86_64, windows-x86_64.exe). Detect
> OS/arch via `uname`, verify the download runs (`kim --version`), fall back to
> source-build when no asset matches or download fails. Keep all env overrides working.
> Test both paths locally (force-fallback with a bogus repo URL for the build path).
> **A15**: after install, if `python3` exists, offer (prompt, or `KIM_SETUP_PYTHON=1`
> non-interactive) to create `~/.kim/source/venv` and `pip install -r requirements.txt`
> so `kim code` browser-bridge works out of the box; print clear skip-instructions
> otherwise. Document in the script header what works without Python (chat) vs what
> needs it (code mode browser bridge).
> **C1**: make both installers (`install.sh`, `cli/install.sh`) print a loud warning when
> overwriting an existing `~/.kim_root` that points somewhere else, showing old → new.
> **A9 doc**: `kim --help` and the installer's post-install text must stop implying
> one-shot results are resumable until Prompt 1 is merged (if Prompt 1 is already merged,
> verify `kim chat "hi"` creates a session file and update help text to say so).
> Shell scripts: `bash -n` + shellcheck if available; manual run of each mode.

## Prompt 5 — Frontend P2/P3 cleanup: notifications, timers, dead listeners, types

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **B8, B9, B10, B11,
> B12, B13, B14, B15** in `desktop/src/`. Specifics:
> **B8** (`hooks/useOsNotifications.ts` + ChatView): suppress notifications while the
> window is focused (track via `document.visibilityState`/Tauri focus events, wire the
> existing `enabled` option); on failure notify ONCE (ignore `kim:run-done success=false`
> when a `kim:run-failed` arrives within ~500ms — or simply only notify from run-done and
> include the failure reason); request notification permission during onboarding/first
> run instead of at first completion.
> **B9** (`useChatStream.ts`): store the rate-limited clear-timer in a ref; clear it on
> new events, run start, and unmount.
> **B10**: delete the four no-op typed listeners (kim:status/plan/step/done) and rewrite
> the stale dual-emit comments to describe the post-V-1 reality.
> **B11** (`StreamRenderer.tsx` new-chat branch): remove `ref={bottomRef}` from the
> `.kim-messages` container — only the bottom sentinel gets it.
> **B12** (`useSessionLoader.ts`): surface load errors (toast + empty-state message
> "Couldn't read this session file"), and key `prevMsgCountRef` by session id.
> **B13**: remove the `as any` casts (settings provider write, bottomRef ×3,
> browserCommandArgs return) with real types.
> **B14** (`utils.ts formatCostUsd`): collapse the dead branch.
> **F2** (`MessageBubble.tsx renderText`): fix fenced-code handling — extract ```fence
> pairs BEFORE paragraph-splitting so blank lines inside code blocks survive; handle
> fences mid-paragraph. Vitest: code block containing a blank line round-trips into one
> `<pre>`.
> **F3** (`MessageBubble.tsx`): sanitize URL schemes — links only http/https/mailto
> (else render as plain text); images only data:/asset/'self' by default, remote https
> images behind a click-to-load placeholder. Vitest for `javascript:` link and remote
> img.
> **B15** (`ChatView.tsx`): sync `permissionMode` when `settings.permission_mode` changes
> (useEffect), keeping per-session override behavior.

## Prompt 6 — CLI polish batch: KIM.md, context budget, file-ref noise, model lists

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **A12, A13, A17, A18,
> A19, A20, C2, C3** in `cli/src/`. Specifics:
> **A12**: make `/init` real — when KIM.md exists in cwd (or any parent up to repo root),
> prepend its contents to the system prompt for chat turns (cap at ~4KB with a truncation
> note). Test: temp dir with KIM.md → assert it lands in the request system message.
> **A13**: desktop-bridge chat — if `/v1/task` ever streams (check the bridge contract in
> `desktop/src-tauri/src/http_bridge.rs`), consume it; otherwise print a "Kim desktop is
> working…" heartbeat line every ~5s while waiting so the user isn't staring at silence.
> **A17**: fix the clamp to `len()-1` and delete the vestigial `ViewState::SessionMenu` +
> `selected_session` if truly unused (grep first).
> **A18**: fetch live model lists where cheap (`/v1/models` for openai-compatible
> providers; keep static fallback). Refresh the static claude/openai lists to current
> model ids (check `claude-api` docs/skill for current ids — do not guess).
> **A19** (`prompt_file_references`): only treat a token as a file reference if it
> contains `/` or `.` AND is not a bare `.`/`..`, or is quoted; never reference cwd
> itself. Tests for "what is .", "look at src/main.rs", quoted paths.
> **A20** (`chat_history`): add a crude char budget (e.g. 24 msgs AND ≤ ~48k chars,
> dropping oldest first) so giant messages can't blow the context.
> **C2**: document the two session formats in `cli/README.md` and make
> `load_session_messages` label orchestrator-trace resumes ("read-only transcript —
> tool context not resumable").
> **C3**: covered in Prompt 3 (skip if done).

## Prompt 7 — THE MILESTONE: agentic `kim chat` + rendered output (Claude Code parity)

> Open MISSION_PROMPTS.md, read GLOBAL RULES. This is a feature mission, not a bug batch
> — read `DEEP_DIVE_AUDIT.md` "honest answer" section and `ARCHITECTURE.md` first, then
> design before coding (write `docs/PROPOSAL_cli_agentic_chat.md`, ~1 page, commit it,
> then implement).
> Goal: `kim chat` (REPL + one-shot) runs the REAL Kim agent — tool loop in the terminal —
> instead of a bare LLM call, when a Kim source root is available.
> Constraints: stream tool events as dim activity lines (the AppEvent enum already
> models this); reuse `python -m orchestrator.agent`'s typed stdout protocol (see
> `events.schema.json` + `desktop/src-tauri/src/subprocess.rs` for the parsing reference —
> port the minimal KimEvent subset to the CLI, do not invent a new protocol);
> HITL approval prompts render as terminal y/N prompts respecting the risk threshold;
> Ctrl-C cancels the run (Prompt 3's machinery); sessions persist through the
> orchestrator's own session store; graceful fallback to today's plain-chat when no
> source root / python is found, with a one-line note.
> Also in this mission: render assistant markdown in the terminal (headings, bold, code
> blocks with a dim border, inline code) — a tiny hand-rolled renderer or a small crate
> (justify the dependency in the proposal).
> Standing constraint: code mode must never use OpenAI auth or gpt-5.5 — unchanged.
> Definition of done: scripted REPL session shows a tool-using task (e.g. "list the
> files in this folder and summarize the biggest one") executing real tools with visible
> activity lines, a resumable session file, all four suites green, CI green.

## Prompt 9 — Backend plumbing batch: bridge pairing, packaged logs, attachments, scheduler

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **D2, D3, D4, D5, D6**.
> Specifics:
> **D2** (`desktop/src-tauri/src/http_bridge.rs` + `cli/src/provider.rs`): make the CLI
> and desktop pair by default. Desktop: whatever token it ends up using (env, .env, or
> the random fallback) gets written to `~/.kim/bridge_token` with 0600 perms (overwrite
> on every bridge start; document that it's a local-loopback credential). CLI
> `bridge_token()`: env `KIM_API_KEY` first, then read that file. Add a `kim doctor`
> line showing whether a bridge token source was found. Integration check: start the
> desktop bridge locally with no env token and verify a CLI `/v1/task` round-trip
> succeeds (or document precisely why it couldn't be tested).
> **D3** (`orchestrator/cli.py` log setup): if `<repo_root>/logs` is not writable
> (packaged/frozen mode), fall back to `~/.kim/logs`. Update the Settings "Reveal logs"
> path resolution (`PaneInfo.tsx` + its Tauri command) to check both locations. Test:
> monkeypatch an unwritable dir → logs land in the fallback.
> **D4** (`desktop/src-tauri/src/feedback.rs save_attachment`): write each attachment to
> a unique subdir (`kim_attachments/<unix-ms>-<rand>/<original-name>` — keep the original
> filename for model readability), and sweep subdirs older than 7 days on each call.
> Unit-test collision: two saves of `report.pdf` → two distinct paths, both readable.
> **D5** (`desktop/src-tauri/src/session_commands.rs prune_sessions`): stop interpolating
> the repo path into Python source — pass it via env var (`KIM_ROOT`) or argv and read it
> in the snippet. Test with a path containing a quote char.
> **D6** (new, smallest-possible scheduler loop): in Rust (`lib.rs` setup or a small
> `scheduler.rs`), spawn a tokio interval (60s) that calls `schedule_commands::run_due_once`
> when (a) a new `settings.schedules_enabled` flag is true (default true if schedules
> exist), and (b) no agent task is currently running (reuse the existing running-task
> tracking — never let a scheduled run stomp an interactive one; skip the tick instead).
> Log each fired schedule to the activity/status channel. Add overlap protection (a
> static AtomicBool guard). Cargo test for the guard logic; manual verification with a
> 1-minute schedule documented in the report.
> **E1** (`orchestrator/providers/gemini.py` `_parse_response`): collect ALL
> `functionCall` parts; when >1, return the same `{"tool": "batch", "args": {"calls":
> [...]}}` shape claude.py/openai_provider.py use; never discard trailing parts. Add a
> multi-functionCall scenario to the V-3 provider contract suite (all providers must
> pass it — this is the test that would have caught the bug).
> **F1** (`orchestrator/task_queue.py`): dead module, zero importers — delete it (git
> history preserves it), unless Prompt 10's steering work explicitly claims it (check
> with the report of that prompt if already run).

## Prompt 10 — Trust & control: run revert, steering, approval previews, privacy pause

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then implement `PRODUCTION_ROADMAP.md`
> Part II-K items **1, 3, 6, 9**. Prereqs: Prompts 2 and 9 merged. Design first: 1-page
> `docs/PROPOSAL_trust_features.md`, commit, then build.
> **K1 — Run checkpoints + revert**: before each run in code/chat mode, record files the
> agent touches (hook `write_file`/`edit` paths in `mcp_server/tools/files.py` + the
> existing `[DIFF]` emission) and back up pre-images to `~/.kim/checkpoints/<run-id>/`
> (cap 50MB/run, skip larger with a note). New Tauri command `revert_run(run_id)`
> restores pre-images (creating `.kim-revert.bak` of current state first — revert must
> itself be undoable). UI: "Revert changes" action on the run pill when a checkpoint
> exists. Python tests for backup/restore round-trip incl. new-file deletion case.
> **K3 — Mid-run steering**: typing while running offers "Steer" (default) vs "Queue":
> steer writes `{"type":"user_steer","text":...}` to agent stdin (extend the HITL stdin
> JSON channel in `subprocess.rs` + Python's stdin reader) and the agent injects it as a
> user message before its next LLM call, with a `[STATUS] steering noted` ack. Update
> the B1 queue UI accordingly. Python test: steer line arrives → message lands in next
> request payload.
> **K6 — Approval previews**: `kim:hitl-approval-request` already carries tool+risk —
> extend the Python emit to include a `preview` field (run_command → the command string;
> write_file/edit → unified diff ≤40 lines; web actions → URL + element label) and
> render it monospace in the approval card. Schema change → `events.schema.json` +
> `npm run gen:events`.
> **K9 — Privacy pause**: global flag (tray menu item + composer eye icon →
> `set_privacy_pause` Tauri command → env-file or stdin flag the MCP server checks);
> while paused, `take_screenshot`/`screen`/`web_screenshot` tools return a typed error
> ("Privacy pause is on") and the agent is prompted to inform the user rather than loop.
> Test: pause → screenshot tool returns the typed error.

## Prompt 11 — Speed & access: global hotkey overlay, tray, command palette

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then implement Part II-K items **2, 7, 8**.
> **K2 — Quick-ask overlay**: `tauri-plugin-global-shortcut` (default ⌥Space,
> rebindable in Settings → System); shortcut toggles a small frameless always-on-top
> WebviewWindow with a single composer input; submit routes to the active (or newest)
> chat session via the existing send path and focuses the main window only when the
> run needs attention (HITL/need-help). Esc hides. Handle shortcut-registration
> failure gracefully (already-taken hotkey → toast + Settings link).
> **K7 — Tray**: `tauri::tray` menu — status line (idle/“Running: <task 40ch>”),
> Cancel current run, last 3 sessions (open), Quick ask, Privacy pause toggle (wire to
> K9 if merged, else hide), Quit. Update status from the existing isRunning/task state
> in Rust (subprocess tracking), not from the frontend.
> **K8 — Command palette**: ⌘K in-app palette listing: new chat, new code session,
> switch session (fuzzy), switch provider, toggle mode, cancel run, open settings panes.
> Centralize these as an actions registry module so palette/shortcuts/tray share one
> implementation (no copy-pasted invoke calls). Vitest for the registry; manual
> verification checklist in the report for hotkey/tray (CI can't test OS integration —
> say exactly what you verified by hand).

## Prompt 12 — Session & composer UX: manage sessions, paste images, export

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then implement Part II-K items **4, 5, 10**.
> **K4 — Session management**: sidebar context menu (right-click + ⋯ button): Rename
> (writes a `title` field to a session meta sidecar or first-class field — check
> `session_commands.rs` read path and extend), Pin (pinned float to top, persisted),
> Delete (single session: JSONL + summary + meta, with confirm). Full-text search box
> in the sidebar filtering by title + message content (Rust command doing the grep —
> stream results, cap 200ms/50 results). Tests for the Rust search + delete commands.
> **K5 — Paste & region capture**: composer `onPaste` handler for `clipboardData` image
> items → existing `save_attachment` path (after Prompt 9's D4 fix) → attachment chip.
> "Capture region" button: hide windows → interactive region screenshot (macOS
> `screencapture -i -x <file>`; Linux `gnome-screenshot -a`/`slurp+grim` best-effort;
> Windows: skip with tooltip) → attach. Verify the image actually reaches the provider
> payload (attachments → message content) end-to-end with the fake provider.
> **K10 — Export run as Markdown**: action on the run pill + session ⋯ menu: build
> markdown (user/assistant messages, collapsed activity as bullet list, files touched,
> duration/cost) → clipboard + optional save dialog. Vitest snapshot for the builder.

## Prompt 13 — Sandbox hardening: secret-file deny, sensitive dirs (SECURITY)

> Open MISSION_PROMPTS.md, read GLOBAL RULES, then fix audit items **G1, G2, G3** in
> `mcp_server/config.py` (+ files.py for G3). This is a security batch — every fix needs
> a test proving the denial. Specifics:
> **G1** (`validate_path`): enforce the already-defined `_SENSITIVE_GLOBS` against
> `p.name` at ANY depth, not just `$HOME`. Deny `.env`, `.env.*`, and add common secret
> filenames: `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `credentials`, `.npmrc`,
> `.pypirc`. A read of the project-root `.env` must raise PermissionError. Tests:
> project `.env`, nested `sub/dir/.env.local`, `keys/server.pem` all denied; a normal
> `notes.txt` allowed. Update root `CLAUDE.md`'s "Standing constraints" if it overclaims
> `.env` protection.
> **G2** (`_SENSITIVE_PATHS`): add `~/.config/gcloud`, `~/.mozilla`, `~/.password-store`,
> `~/Library/Application Support/Google/Chrome`, `~/Library/Application Support/Firefox`,
> `~/Library/Application Support/Code`. Keep cross-platform (guard the macOS-only ones).
> Test each is denied.
> **G3** (`files.py handle_write_file`): require the data-URI base64 branch to match the
> WHOLE content (`^data:[^,]*;base64,` anchored, no trailing junk) AND add an explicit
> `binary: bool` arg path; a text file whose content merely starts with `data:...;base64,`
> must be written as text. Test both.
> NOTE: do NOT touch the shell blocklist — it is a documented speed-bump, not a boundary
> (the real control is the HITL gate). Adding to `_DENY_COMMANDS` is fine; do not claim
> it's a sandbox.

## Prompt 8 — Release hygiene: version bump, changelog, tag dry-run

> Open MISSION_PROMPTS.md, read GLOBAL RULES. Prereq: Prompts 1–5 merged.
> Update `CHANGELOG.md` (keep-a-changelog format) summarizing everything since the last
> tag (read `git log`, `EXECUTION_REPORT.md`, `DEEP_DIVE_AUDIT.md` fix branches). Bump
> versions consistently: `desktop/src-tauri/tauri.conf.json`, `desktop/package.json`,
> `cli/Cargo.toml` (+ lockfiles). Run the release workflow in dry-run mode
> (`gh workflow run release.yml -f dry_run=true`) and confirm all four matrix builds
> pass; report the artifact list. Do NOT push a tag — the human tags after review.

---

*Generated 2026-06-12 from DEEP_DIVE_AUDIT.md @ ab2722d. If audit items get fixed out of
band, a prompt's agent should detect already-fixed items in Step 0 (read the audit entry,
check the code) and mark them SKIPPED-ALREADY-FIXED rather than re-doing them.*
