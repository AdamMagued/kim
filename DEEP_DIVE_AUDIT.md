# Deep-Dive Bug Audit — main @ `5d837dc` (post-merge)

**Date:** 2026-06-12 · **Method:** full read of `cli/` (all 5 files), post-split frontend
(useChatStream, ChatView, StreamRenderer, useTaskRunner, useSessionLoader, parsers,
codexEvents, ChatComposer, useOsNotifications, utils cost code), spot-reads of
subprocess.rs emit path, session_store retention, stuck_detection. **Plus a live run of
the built CLI binary against local ollama** — findings marked 🔴 LIVE were reproduced on
this machine, not just read in code.

Severity: **P0** broken core flow · **P1** real bug, hurts soon · **P2** correctness/UX debt · **P3** smell/polish.

---

## A. kim CLI (`cli/src/`)

### A1 · P0 🔴 LIVE — Chat sessions are never saved; assistant replies vanish from context
`main.rs stream_repl_turn`: `is_local_agent = provider != "desktop" && mode != Code` is
true for **every normal chat** (ollama, claude, gemini, deepseek, browser:*). That branch
skips the pre-turn save and, after streaming, tries to *reload* `~/.kim/sessions/{id}.jsonl`
— a file **nothing ever writes** (the direct-API path streams HTTP; no agent writes that
file). Net effect, reproduced live:
- No session file is created (`~/.kim/sessions/` unchanged after a 2-turn chat).
- The streamed assistant text is **never pushed to `app.messages`** → `chat_history()`
  sends *user messages only* → in turn 2 the model literally saw two user messages and no
  assistant turn (visible in its leaked reasoning).
- `/compact`, `/sessions`, `--resume` are all broken for these chats as a consequence.
The reload branch is vestigial (built for an old "local Python agent writes the file"
design that no longer exists in `provider.rs`). The non-`is_local_agent` else-branch is
the correct logic and should simply always run.

### A2 · P0 — Resuming a session then chatting *destroys* the new exchange
Same reload branch: for a **resumed** session the file *does* exist (from the old save
format), so after the turn `app.messages = load_session_messages(file)` — the old file
content — wiping the just-typed user prompt and the new reply from memory. Conversation
state actively regresses every turn.

### A3 · P0 🔴 LIVE — In-stream provider errors silently swallowed → "Kim: (no response)"
`process_openai_sse_line` only reads `choices`; an in-stream `{"error": ...}` object
(which ollama emits, e.g. model-not-found for `gpt-oss:20b-cloud` while only 120b is
pulled) is dropped on the floor. Live: turn 1 returned "(no response)" in 0s with zero
diagnostics. Anthropic path has the same gap (`type:"error"` SSE events unhandled).

### A4 · P1 🔴 LIVE — gpt-oss "harmony" reasoning leaks as answer text
Ollama cloud gpt-oss streams chain-of-thought inside `delta.content` ending with the fused
token `assistantfinal`. The CLI's `ThinkParser` only knows `<think>` tags, so the user
sees the raw CoT printed as the answer, concatenated like `…so answer: ZEBRA42.assistantfinalZEBRA42`.
Needs `assistantfinal` boundary handling (and/or requesting reasoning-separated output).

### A5 · P1 — Panic risk: byte-slicing UTF-8 at fixed offset
`provider.rs` ×2 (`function_call_output` handlers): `&trimmed[..300]` panics if byte 300
is mid-multibyte-char. Tool output containing emoji/non-ASCII near the boundary **crashes
the whole CLI**. Needs char-boundary-safe truncation (a safe `truncate()` already exists
in sessions.rs).

### A6 · P1 — No way to cancel a generation
During streaming there is no Ctrl-C/Esc handling — SIGINT kills the entire CLI (default
handler), losing the REPL. Claude Code parity requires turn-cancel (select on
`tokio::signal::ctrl_c`, abort the spawn handle; careful with rustyline's own SIGINT
handling).

### A7 · P1 — Broken `--resume` hint printed for sessions that don't exist
On REPL exit the hint `Resume this Kim session with: kim --resume <id>` prints
unconditionally — live-confirmed pointing at a file that was never written (consequence
of A1, but also fires for empty REPLs where save is skipped by design).

### A8 · P1 — `kim doctor` validates nothing about the selected model
Doctor said "ok" on this machine while the configured model (`gpt-oss:20b-cloud`) was not
in `ollama /api/tags` (only 120b) — the exact condition that then produced A3's silent
no-response. Doctor should check the configured model is actually servable.

### A9 · P2 — One-shot `kim chat "..."` results are unresumable
Same root as A1: one-shot mode saves nothing for non-desktop providers, so the printed
output is the only artifact.

### A10 · P2 — `/git` breaks quoted arguments
`run_project_command` does `args.split_whitespace()` → `/git commit -m "two words"`
becomes 4 tokens. The shell-ish tokenizer that already exists in main.rs
(`split_shellish_tokens`) isn't used here.

### A11 · P2 — Magic-string command protocol
`commands.rs` ↔ `main.rs` communicate via sentinel strings (`"Conversation cleared."`,
`"__KIM_REFRESH_SESSIONS__"`, `"__KIM_RESUME_SESSION__:<id>"`) matched by string equality
on the *message text*. Fragile by construction; should be `CommandOutcome` enum variants
(the enum already exists — these escaped it).

### A12 · P2 — `/init` creates KIM.md that nothing reads
The CLI never loads KIM.md into any prompt (context loading is orchestrator-side only).
The command is currently a no-op feature that implies project context that doesn't exist.

### A13 · P2 — Desktop-bridge chat is non-streaming
`stream_via_bridge` waits for the full `/v1/task` response and emits it as one blob —
fine mechanically, but the UX is a long silent stall then a wall of text.

### A14 · P2 — Installer requires a Rust toolchain; prebuilt binaries unused
`cli/install.sh` (one-line curl mode) clones the repo and `cargo build --release`. On a
machine without cargo/git it dies immediately — while `release.yml` already publishes
prebuilt `kim` binaries for all four targets that the installer never tries. Also
bash-only (no Windows path). "One command install" is only true for Rust developers.

### A15 · P2 — Installer doesn't provision the Python side
Code mode with browser providers needs `python3` + orchestrator deps at the source root;
install.sh writes `~/.kim_root` but never creates a venv or installs requirements →
`kim code` fails on clean machines with a Python import error at first use.

### A16 · P2 — Doctor base-URL edge: `http://host/v1/` probes `…/v1/api/tags`
`commands.rs trim_base_url` strips `/v1` only when it's the literal suffix; a trailing
slash defeats it (provider.rs has a different, also-divergent `trim_base_url` — two
implementations, both with edge cases).

### A17 · P3 — `refresh_sessions` index clamp off-by-one
`selected_session.min(sessions.len())` allows an out-of-range index (should be `len()-1`).
Currently latent — the field is effectively write-only (pickers use a local index) — but
it's a loaded gun for whoever next reads it. `ViewState::SessionMenu` is similarly
vestigial.

### A18 · P3 — Hardcoded/stale model lists
`model_options()` hardcodes claude/openai/gemini lists (e.g. `o1-mini`, no current opus);
`known_ollama_cloud_models()` is a static list that will drift. Fine for v1; needs a
provider `/models` fetch eventually.

### A19 · P3 — File-reference detector over-matches
Any whitespace token in a prompt that happens to exist as a path (`.`, `src`, `Cargo.toml`)
gets canonicalized and appended as "Referenced local files Kim may access" — silent prompt
noise on innocent messages like "what is ." .

### A20 · P3 — History sent to provider is last-24-messages with no token budget
`chat_history()` truncation is message-count-based; a few huge messages can blow the
context window with no compaction/estimation on the direct-API path.

---

## B. Desktop app frontend (`desktop/src/`)

### B1 · P0 — Queued messages are never executed
`useTaskRunner`: submitting while a task runs appends to `queuedTasks` and toasts
"Queued message #N. **Kim will run it automatically next.**" — but no code anywhere
drains the queue when `isRunning` flips false (verified: `setQueuedTasks` is only ever
called to append). The messages sit in the indicator forever and are lost on reload.
The `interruptTask` flow is doubly dead: `queueEnabled` is hardcoded `true` in both
useTaskRunner and ChatComposer, so the interrupt branch is unreachable.

### B2 · P1 — Editing a live user message edits the wrong message (and does nothing)
`StreamRenderer` passes the **collapsed-array index** `i` to `handleEditLiveMessage`,
which indexes **uncollapsed `liveHistory`** — after any retry-collapse the indices
diverge and the wrong message is modified. The handler also mutates state in place
(`next[idx].content = newText` on a shallow copy — same object identity, memoized children
won't re-render). And functionally the "edit" only rewrites local display state — it
doesn't resend or affect the agent — so the feature is cosmetic at best, misleading at
worst. (Both call sites: new-chat branch and session branch.)

### B3 · P1 — Raw `'agent-error'` sentinel rendered to users in session view
The new-chat branch special-cases `taskError === 'agent-error'` into a friendly message;
the existing-session branch renders `taskError` verbatim through `SignalCard` — users see
the literal string "agent-error".

### B4 · P1 — Double/missing failure surfaces
One failed run can render **both** the structured `runFailure` card and the legacy
`taskError` banner (new-chat branch). Meanwhile the session branch renders **neither**
`runFailure` nor the rate-limited banner (only `taskError`) — failure UX differs by view
for no reason.

### B5 · P1 — Cost meter charges money for free browser providers
`estimateCostUsd` does an exact-key lookup; `browser:claude` / `browser:chatgpt` /
`browser:gemini` (the actual provider strings) miss the `browser` entry and **fall back
to claude pricing** — free browser-session runs display a fake dollar cost. Also the pill
recomputes historical runs with the *currently selected* provider's rates, and uses the
latest `tokenStats` for whichever run is "last" — switch provider after a run and the
numbers change.

### B6 · P1 — Stale state leaks into new chats
The `newChatMode` reset effect clears activity/runHistory/taskError/tokenStats/context/
elapsed but **not** `liveHistory`, `runFailure`, `rateLimitedState`, `hitlApprovalStatus`,
or `lastFailedTask`. Old failure cards / pending-approval cards / live bubbles can render
inside a fresh chat until the next send (which does clear them in `runPendingTask`).

### B7 · P1 — Pending approval card survives run end
`kim-agent-done` doesn't clear `hitlApprovalStatus` (the cancelled handler does). If a
run ends while an approval is pending (timeout/agent exit), the Approve/Deny card lingers
— and clicking Approve invokes `hitl_respond_approval` against a dead run.

### B8 · P2 — OS notifications misbehave
(1) Fire even when the app window is focused — the `enabled` option exists but ChatView
calls `useOsNotifications()` bare, no focus tracking. (2) A failed run notifies **twice**
(`kim:run-done success=false` → "Task ended: X" *and* `kim:run-failed` → "task failed").
(3) The permission prompt pops at first task completion instead of onboarding.

### B9 · P2 — Rate-limited banner timer race
Each `kim:rate-limited` event schedules `setTimeout(clear, delay+1s)` that is never
cancelled on unmount/new run — a stale timer from run N can clear run N+1's banner.

### B10 · P2 — Dead no-op typed listeners + stale dual-emit comments
`kim:status/plan/step/done` listeners are registered as no-ops with comments still
describing the pre-V-1 dual-emit world. Misleading for the next change; either subscribe
meaningfully or drop them.

### B11 · P2 — `bottomRef` bound to two elements in the new-chat branch
Both the `.kim-messages` container (line ~308) and the bottom sentinel div get
`ref={bottomRef}`; last-mounted wins, so the scroll target is mount-order-dependent.
Session branch binds only the sentinel (correct).

### B12 · P2 — Session loader swallows errors and cross-contaminates state
`load_session_messages` failures are silently `catch(() => {})`-ed → user sees "No
messages in this session" with no hint. `prevMsgCountRef` is not keyed by session, so the
"newest message" animation heuristic can fire on the wrong message after switching
sessions.

### B13 · P3 — Type holes
`provider: val as any` written into settings (ChatView), `ref={bottomRef as any}` ×3,
`browserCommandArgs: (...) => any`. ESLint no-new-any rule is being dodged in the exact
places type safety matters (IPC payloads).

### B14 · P3 — `formatCostUsd` dead branch
`usd < 0.01` and the default branch are identical (`toFixed(4)`).

### B15 · P3 — `permissionMode` initialized from settings once
`useState(() => settings.permission_mode)` — changing the default in Settings while a
chat is open never syncs the toggle (per-session divergence is arguably intended; the
silent divergence is not).

---

## C. Cross-cutting

### C1 · P2 — Two `~/.kim_root` writers
Both `install.sh` (repo root, desktop) and `cli/install.sh` write `~/.kim_root` —
last-writer-wins. A user with the desktop installed from one checkout and the CLI
installed via curl (clones into `~/.kim/source`) ends up with both products pointing at
whichever installed last, which silently changes what `kim code` / browser-bridge runs.

### C2 · P2 — CLI and desktop have divergent session formats in one directory
CLI writes `{type:"message", role, content, timestamp_ms}` lines into `~/.kim/sessions/`;
the orchestrator writes its richer trace records into `kim_sessions/`. The CLI's
discovery scans **both** plus cwd dirs, with format-sniffing in `load_session_messages`.
Works, but resume semantics differ silently per source (and orchestrator trace files
resumed in the CLI lose everything but displayable text).

### C3 · P3 — Two `trim_base_url` implementations (commands.rs vs provider.rs) with
different behavior; one appends `/v1`, one strips it. Consolidate.

---

## D. Second-pass findings (Rust backend + plumbing, 2026-06-12)

### D1 · P1 — `find_python_interpreter` treats `~/.kim_root` as a directory; it's a file
`subprocess.rs` step 2 probes `~/.kim_root/venv/bin/python` etc. — but **both installers
write `~/.kim_root` as a plain FILE containing the checkout path** (`echo "$PWD" >
~/.kim_root`). Those candidates are impossible paths (you can't have children under a
file) — the entire step is dead code, and the function **never reads the file** to locate
the install checkout's venv. Consequence: a packaged app (no sidecar) or any run where
`project_root` isn't the checkout itself falls through to system python, which lacks the
orchestrator deps → agent fails to boot with an import error. Fix: read the file, resolve
`<contents>/venv/bin/python`.

### D2 · P1 — CLI ↔ desktop bridge token pairing is broken by default
`/v1/task` (the CLI's desktop/browser-provider entry) is correctly gated on
`X-Kim-Token`. But when `KIM_API_KEY` is missing from env/.env, the desktop **falls back
to a random token** (http_bridge.rs:1709) and deliberately never writes it to disk —
while the CLI only sends the header if *its own* env has `KIM_API_KEY`. Net: on a default
setup, `kim` + `/login browser:claude` → every request 401s. Two products that are
supposed to pair out of the box can't. Fix: desktop writes the active token to a 0600
file (e.g. `~/.kim/bridge_token`), CLI reads env → that file, in that order.

### D3 · P2 — Structured file logs break in packaged mode
Log setup (`orchestrator/cli.py:62`) writes to `<repo_root>/logs` derived from
`Path(__file__).parent.parent` — inside a frozen sidecar/.app bundle that's read-only, so
setup throws and is silently `except`-ed to a debug message. Packaged users get no file
logs and the Settings "Reveal logs" button points at nothing. Fix: fall back to a user
data dir (`~/.kim/logs`) when the repo dir isn't writable.

### D4 · P2 — Attachment storage: collisions, no cleanup
`save_attachment` (feedback.rs) writes every attachment to the shared
`$TMPDIR/kim_attachments/<original-filename>` — two different files named `report.pdf`
(any session, any time) silently overwrite each other, and nothing ever cleans the
directory. Fix: per-save unique subdir (timestamp/uuid) + retention sweep.

### D5 · P3 — `prune_sessions` python snippet breaks on quote-in-path
session_commands.rs interpolates `kim_root.display()` into a python raw string
(`r"{root}"`) — a path containing `"` produces a syntax error. Use an env var or argv
instead of source interpolation.

### D6 · P1 — Scheduled tasks never fire on their own
The whole scheduling subsystem (cron_store, schedule_commands, SchedulePane) is
button-driven only: `run_due_scheduled_task` is invoked solely from the Settings pane
(SchedulePane.tsx:497). There is **no background timer** in Rust or the frontend — a
"scheduled" task runs only if the user opens Settings and clicks. Fix: a Tauri background
interval (e.g. every 60s, gated on a setting) calling `run_due_once`, with overlap
protection.

---

## E. Third-pass findings (2026-06-12)

### E1 · P1 — Gemini provider silently drops parallel tool calls
`orchestrator/providers/gemini.py:363-371` returns on the FIRST `functionCall` part —
any additional functionCall parts and any text parts after it are discarded without a
trace. Claude (`claude.py:132`), OpenAI, and Ollama all wrap multi-calls as a `batch`
tool call; Gemini violates the provider contract. Exactly the class of bug the V-3
parametrized contract suite should cover — add a multi-call scenario to it when fixing.
(DeepSeek is safe: subclasses OpenAIProvider.)

### E2 · INFO — Discord webhook: verified NOT exposed
`feedback.rs` embeds the webhook via `option_env!("KIM_DISCORD_WEBHOOK")` at compile
time — source contains no URL. Verified: no real webhook URL (numeric id) anywhere in
git history; no `sk-ant-`/`AIza`/`ghp_` real-shaped keys in any commit; no `.env` ever
committed; CI/release workflows do NOT set `KIM_DISCORD_WEBHOOK`, so public release
binaries ship with it empty (feedback no-ops). **Standing rule:** never add the webhook
to public CI — any URL compiled into a distributed binary is extractable with `strings`
and spammable/deletable by anyone (Discord webhooks carry full post+delete rights). If
real feedback collection is wanted for public builds, proxy it through a tiny server-side
relay instead. One papercut: with the webhook unset, `send_feedback` returns Ok and the
UI shows success while sending nothing — intentional per the comment, but "Feedback
recorded locally" honesty would be better.

### E3 · CI status — nothing to fix
All recent `main` runs green (`cf729a8`, `5aacf5a`, `ab2722d`, merge `5d837dc`); no red
runs on any live branch. The only historical CI breakage (invalid workflow YAML) was
fixed in `75897ee` and is documented in EXECUTION_REPORT.md.

---

## Ratings (out of 10)

| Area | Score | One-liner |
|---|---|---|
| Architecture (whole repo) | **8** | Post-merge structure is genuinely good: split modules, typed events, seam tests, per-dir docs |
| Code quality (whole repo) | **7.5** | Python/orchestrator strongest; frontend has state-hygiene debt; CLI has a broken core flow |
| Desktop app UI | **7** | Looks and reads well; B1–B7 are real user-facing bugs but all shallow to fix |
| **kim CLI** | **5** | Excellent plumbing (atomic config, 0600 perms, doctor, completions, secure key entry, 90 tests) wrapped around a **broken primary loop** (A1–A3) |
| Production readiness | **6** | Was ~6.5 before this audit found A1/B1 |

## The honest answer to "is the CLI as good as Claude Code?"

**Not yet — and it's not mainly about bugs.** Three structural gaps after A1–A3 are fixed:

1. **Chat mode isn't agentic.** `kim chat` is a plain LLM chat with no tools — no file
   reads, no commands, no screenshots. The Kim agent (the actual product) only powers the
   desktop bridge and code mode. Claude Code's defining trait is the tool loop in the
   terminal; Kim CLI has the slash-command shell *around* such a loop but not the loop.
2. **No rendering layer.** Output is raw text — no markdown, no syntax highlighting, no
   diff view. Claude Code's terminal output is formatted.
3. **No cancel, no streaming via bridge, no token/context meter** in the REPL.

What's *already* at or near Claude Code grade: doctor, slash menu + tab completion,
session picker, atomic config with private perms, secure key entry, one-shot + stdin
piping, the install script's ergonomics (modulo A14).

**Install UX:** the one-liner exists
(`curl -fsSL …/cli/install.sh | bash`) but requires git+cargo → fails for normal users.
Fix path is cheap: try the prebuilt release artifact first (it's already being built!),
fall back to cargo. Claude-Code-grade would be `brew install` / `npm i -g` — that's
packaging work, not code work.

## Forward plan (my call, in order)

1. **CLI P0 batch** — A1+A2 (delete the vestigial reload branch, always push+save),
   A3 (surface stream errors), A5 (safe truncation), A7 (gate the resume hint). One
   focused session, ~a day, with a no-network regression test for the persistence loop.
2. **Frontend P0/P1 batch** — B1 (drain the queue on run end — or remove the queue UI),
   B2, B3, B5, B6, B7. Also one session.
3. **CLI UX batch** — A4 (harmony), A6 (cancel), A8 (doctor model check), A13/A14/A15
   (installer: prebuilt-first + venv provisioning).
4. **Then** the product leap: route `kim chat` through the orchestrator agent (tools in
   the terminal) + markdown rendering. That's the "Claude Code parity" milestone and
   deserves its own planned session.
5. P2/P3 items ride along opportunistically; none block anything.
