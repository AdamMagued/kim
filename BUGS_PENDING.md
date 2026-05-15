# Pending Bug Context — handoff for a future Claude session

Two issues the user hit on 2026-05-15 while testing Kim with Ollama Cloud
(gpt-oss:120b). Filed here so the next session can act cold without
re-exploring. Read this top-to-bottom before touching the codebase.

User's verbatim report:

> when sending the prompt whats on my screen via ollama cloud gpt-oss 120b
> i got "macOS Accessibility permission is required for observe_ui. Please
> enable Accessibility access for the Kim process in System Settings →
> Privacy & Security → Accessibility." he's throwing me this error for no
> reason even tho it's already done. and i got an error saying hume api key
> not sent and some bs which then caused the prompt to fail. i believe it
> was that fault so i turned off voice completely.
>
> additionally when i did take screenshot it took a screenshot but it didn't
> go back to kim — there was a stop button tho. this IS intended flow, the
> only issue is i don't know why it took so long/failed.

Working hypothesis after a code audit: **the two reports are linked**. The
spurious `observe_ui` error and the screenshot hang are both downstream of
the Hume voice provider blocking on an unreachable endpoint for ~15s per
call. The accessibility error is a separate but smaller issue.

---

## Bug 1 — Hume voice blocks the agent loop (THE BIG ONE)

### Symptoms

- "Hume api key not sent" surfaces during a normal chat prompt.
- After `take_screenshot`, the agent appears to freeze for many seconds and
  the next tool call never fires (stop button shows, eventually fails).
- Turning voice off makes everything work.

### Where it lives

- **Primary**: `tray/voice.py`, lines ~585–725 (`HumeVoiceProvider` class)
- **Speak path**: `tray/voice.py` line ~681 — `urllib.request.urlopen(req, timeout=15)`
- **Init path**: `tray/voice.py` lines ~615–632 — reads `HUME_API_KEY` from
  env once at construction; if missing, sets `_available = False` but the
  fallback chain still has Hume listed as primary.
- **Called from**: `orchestrator/agent.py` — `_voice_speak()` helper, invoked
  on TASK_COMPLETE, NEED_HELP, stuck-detection, and after some tool calls
  (search agent for `_voice_speak` to see all call sites).

### Root cause

Two compounding problems:

1. The Hume provider's `speak_sync` issues an HTTP POST with a **15-second**
   timeout via `urllib.request.urlopen`. When `HUME_API_KEY` is missing or
   invalid, this either fails fast with a 401 or — more often — hangs the
   full 15s waiting on a TCP-level timeout. Because the agent's tool loop
   awaits the speak call (or runs it on a thread that the next iteration
   joins), the loop appears frozen for the duration.

2. The Hume failure surfaces as a generic exception that bubbles up as
   "Hume api key not sent" inside the chat error path. The user sees an
   error that *looks* like it's about chat auth but is actually about TTS.

The screenshot hang specifically: after `take_screenshot` returns its
data URI, the agent sometimes wants to narrate ("I took a screenshot, let
me look…"). That narration call goes through the voice engine, which calls
Hume, which hangs 15s. The agent appears stuck because it really is —
waiting on a blocking call disguised as a fire-and-forget.

### Fix recipe (do all four)

1. **Validate the key at speak time, not just init time.**
   In `HumeVoiceProvider._speak_sync`, re-read `HUME_API_KEY` once at the
   top. If empty or whitespace, return immediately with `available=False`
   without making the HTTP call. This kills the 15s hang outright.

2. **Hard-cap the network timeout.**
   Drop `urllib.request.urlopen(timeout=15)` to `timeout=3` (Hume normally
   answers in <500ms; 3s is generous). Any longer and we'd rather skip
   voice than block the agent.

3. **Catch URLError / socket.timeout / HTTPError and downgrade silently.**
   Wrap the urlopen in try/except. On failure, log once at WARNING level
   (deduped by error class) and fall through to the next provider in the
   chain (kokoro is the natural fallback). Never let a voice exception
   bubble into the chat path.

4. **Run voice off the agent loop's critical path.**
   Audit `_voice_speak` in `orchestrator/agent.py`. If it's `await`-ed on
   the same task that runs the next tool call, change to fire-and-forget:
   `asyncio.create_task(voice.speak_async(text))` with the result task
   discarded. The agent should never block on TTS.

### Test plan

- Set `HUME_API_KEY=""` in `.env`, leave voice engine = `hume` in
  `config.yaml`. Run a prompt. Expected: prompt completes in normal time
  (no 15s pause), log shows one "Hume disabled (no key)" warning.
- Set `HUME_API_KEY="invalid-key"`. Run a prompt. Expected: same as above
  — one warning, no hang.
- Set a valid key. Run a prompt with screenshot. Expected: speech plays,
  agent loop continues without waiting on speech to finish.
- Run a prompt that calls `take_screenshot` then a second tool. Confirm
  the second tool fires within <1s of the screenshot returning, with
  voice on AND off.

### Files to touch

- `tray/voice.py` (Hume provider class)
- `orchestrator/agent.py` (`_voice_speak` and its call sites — grep first)

---

## Bug 2 — `observe_ui` spuriously claims Accessibility is missing

### Symptoms

User asked "what's on my screen", got:

> macOS Accessibility permission is required for observe_ui. Please enable
> Accessibility access for the Kim process in System Settings → Privacy &
> Security → Accessibility.

— even though Kim is already granted Accessibility in System Settings.

### Where it lives

- **Tool**: `mcp_server/tools/ui_observe.py`, function `handle_observe_ui`
- **Preflight check**: lines ~174–192 (an inline AppleScript that probes
  `front window of frontProc`)
- **Main script**: lines ~195–298 (the actual UI walk)
- **Error mapping**: lines ~187–192 (only matches AppleScript error
  `-1719`, the canonical "user declined access" code)

### Root cause (best guess)

Two failure modes converge on the same user-facing message:

1. **Real TCC denial** — error `-1719`. Handled correctly.
2. **Transient AppleScript failure** that *isn't* a permission denial but
   the main-script branch maps any non-zero exit to a generic ERROR
   string that the LLM then summarises as "permission needed" because
   that's the most common cause of `osascript` failures.

What likely triggered it for the user: the dev rebuild produced a new
binary signature. macOS sometimes treats this as a fresh identity and the
existing TCC grant doesn't transfer cleanly, even though the entry still
shows in System Settings. The grant says "Kim" but the kernel sees a
different code-sign hash. The check correctly detects "no access", the
user correctly sees Kim listed → confusion.

**Caveat**: this hypothesis is unverified. Before writing the fix,
reproduce locally by:
1. Building Kim.
2. Confirming the prompt fails.
3. Toggling Kim off → on in Accessibility settings.
4. Re-running the same prompt.

If step 4 fixes it, the hypothesis is right. If not, the bug is in
`ui_observe.py` itself.

### Fix recipe

1. **Replace the AppleScript preflight with a native check.**
   Use a small Rust/Swift sidecar (or PyObjC) to call
   `AXIsProcessTrustedWithOptions(NULL)`. This returns a clean bool and
   doesn't depend on AppleScript working at all. Cache the result for the
   process lifetime — if it's true once, it stays true.

2. **Stop conflating "main script failed" with "permission denied".**
   In the main-script error branch (line ~300), only return the
   permission-required message if the error code is `-1719`. Any other
   non-zero exit should return the raw error text with a "AppleScript
   error N: <msg>" prefix, so the LLM doesn't claim it's a permission
   problem when it isn't.

3. **Add a "did the rebuild break TCC?" hint.**
   When the permission check genuinely fails AND we detect that Kim was
   recently rebuilt (compare binary mtime vs TCC.db mtime, or just
   detect dev mode), append: "If you recently rebuilt Kim, toggle it off
   and on in System Settings → Accessibility."

### Test plan

- Grant Accessibility, run `observe_ui` — should return UI elements.
- Revoke Accessibility, run `observe_ui` — should return the
  permission-required message with the "toggle off/on" hint.
- Rebuild Kim, run `observe_ui` immediately — if it fails, hint should
  appear. After toggling Accessibility off/on, it should work.

### Files to touch

- `mcp_server/tools/ui_observe.py` (preflight + main-script error
  handling)
- Possibly `desktop/src-tauri/src/lib.rs` if we add a Rust-side native
  check (look for existing `AXIsProcessTrusted` usage first — there may
  already be a helper).

### Workaround for now

Toggle Kim off then back on in **System Settings → Privacy & Security →
Accessibility**. If that fails, remove Kim entirely and re-add it.

---

## Related context the explore agent surfaced

For full details see explore output from the 2026-05-15 session. Key
findings reproduced here so future Claude doesn't have to re-run it:

- `tray/voice.py` `_speak_sync` at line ~1050 holds a lock during the
  fallback-chain iteration. If Hume is the primary and hangs, kokoro
  never gets a turn within that call.
- `mcp_server/tools/screen.py` `handle_take_screenshot` (lines ~13–36)
  returns cleanly. The screenshot itself is not the bug — the post-
  screenshot agent step is.
- Base64 size for a 4K screenshot at scale=1.0 is 10MB+. Not a hang
  cause but worth downscaling to ~1500px max width for transport. Filed
  as a nice-to-have, not part of bug fix.

---

## Order of operations when you tackle this

1. Start with Bug 1 (Hume hang). It's the bigger user-facing impact and
   the diagnosis is more solid. Fix recipes 1–3 are mechanical; 4
   requires reading agent.py carefully.
2. Verify Bug 1 fix by re-running screenshot-then-tool sequences with
   voice on.
3. Move to Bug 2 (observe_ui). Reproduce first to validate the
   rebuild-broke-TCC hypothesis before writing the fix.
4. Don't combine these into one PR — they're independent.

## What's NOT in scope here

- The agent-side stuck-detection logic (works correctly today; would only
  need changes if the Hume fix surfaces a new kind of hang).
- The relay/pairing code added 2026-05-15 — see `relay_server/`,
  `desktop/src/components/PairingModal.tsx`, `orchestrator/relay_worker.py`.
  Those are unrelated to these bugs and verified working.
