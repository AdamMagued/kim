> **Archived** — historical document retained for provenance; not maintained. For current plans and repo structure see ROADMAP.md and the living docs at the repo root.

# Second Patch Notes — Browser Meta, Restore UX, Races

## Scope delivered in this patch

This is the second patch on top of `kim_browser_reliability_patched.zip`. It focuses only on browser session metadata access, safer restore behavior, session-switch race handling, and pure protocol tests.

Provider handoff, `/compact`, and the context/token bar are intentionally left as separate future logical commits so the browser-continuity fix stays reviewable.

## Files touched and why

- `desktop/src-tauri/src/lib.rs`
  - Added bridge HTTP routes for browser metadata:
    - `GET /v1/browser/current-url`
    - `GET /v1/browser/meta?session_id=...`
    - `POST /v1/browser/meta`
    - `POST /v1/browser/commit-url`
    - `POST /v1/browser/restore`
  - Tightened restore URL validation so stored URLs must pass the provider allowlist and must not be login/auth/home/new-chat URLs.
  - Added provider-browser navigation guard to `navigate_browser_window_if_open` so it refuses non-provider URLs.
  - Added `message` on `BrowserRestoreResult` so the UI/CLI can explain fallback reasons.
  - Changed `.browser.json` writes to temp-file + rename, with a Windows fallback that avoids partially-written JSON even though overwrite-by-rename is not fully atomic on Windows.
  - Added `KIM_BROWSER_RESTORE_STATUS` env propagation for UI-launched and kimctl-launched browser tasks. `BrowserProvider` uses this to send a lighter recap when the provider thread was restored from sidecar metadata.

- `desktop/src/components/ChatView.tsx`
  - Added session restore race protection with a monotonic restore sequence.
  - Ensured session entry paths call restore for concrete sessions instead of only sessions with already-loaded browser metadata.
  - Added restore fallback toasts for invalid saved URLs.
  - Blocked provider switching while a task is running, because switching mid-task can destroy browser LLM context.
  - Invalidated pending restores when the user switches provider.

- `desktop/src/types/index.ts`
  - Added optional `message?: string` to `BrowserRestoreResult`.

- `kimctl/__main__.py`
  - Added kimctl browser metadata commands:
    - `python -m kimctl browser current-url`
    - `python -m kimctl browser meta <session_id> [--site gemini]`
    - `python -m kimctl browser commit-url <session_id> [--site gemini]`
    - `python -m kimctl browser restore <session_id> [--site gemini]`
  - These use the same token-auth bridge request helper as existing kimctl commands.

- `orchestrator/providers/browser_provider.py`
  - Added lighter first-send recap behavior when `KIM_BROWSER_RESTORE_STATUS=stored_thread`.
  - The restored-thread path uses a short refresher block instead of a full prior-conversation replay, because the web provider already has the thread context.

- `tests/test_browser_protocol.py`
  - Added pure logic tests for:
    - formatted Claw prompt having only the dynamic marker instruction
    - lighter recap when restored browser thread status is present
    - full recap header when no restored browser thread status is present

## Human QA checklist

- Start Kim normally and open a browser-backed chat session using Browser: Gemini.
- Send a normal task and verify `<session_id>.browser.json` appears beside the session JSONL.
- Reopen or reselect the same Kim session and verify the browser restores the saved Gemini conversation URL.
- Create/select another Kim session and verify it does not inherit the previous session's saved URL.
- Switch Browser: Gemini → Browser: Claude with no task running.
  - Expected: current Gemini URL is committed if valid.
  - Expected: Claude restores the session-specific Claude URL if present, otherwise opens Claude's start page.
- Try switching providers while a task is running.
  - Expected: UI shows a warning toast and does not navigate the provider browser.
- Manually force a bad sidecar URL such as a login/home page.
  - Expected: restore refuses it, opens a provider start page, and does not overwrite the previous good URL.
- From terminal, verify bridge metadata commands while Kim is running:
  - `python -m kimctl browser current-url`
  - `python -m kimctl browser meta <session_id> --json`
  - `python -m kimctl browser commit-url <session_id> --site gemini --json`
  - `python -m kimctl browser restore <session_id> --site gemini --json`
- Send a follow-up in a restored browser session and verify the prompt does not replay a huge prior transcript into the provider UI.

## Tests added

Human should run:

```bash
PYTHONPATH=. python3 tests/test_browser_protocol.py -v
```

Tests that construct `BrowserProvider` **skip** when `playwright` is not installed (lazy import). Claw-only tests always run.

Do **not** use:

```bash
python -m unittest tests.test_browser_protocol
```

Some environments have a third-party `tests` package that shadows the repo's local `tests/` directory, which can make `python -m unittest tests...` import the wrong package.

## Known risks / not verified

- I did not execute tests or compile commands for this second patch; this pass used self-review by reading the diff only.
- Rust compile should still be verified locally with your normal Tauri flow.
- The bridge HTTP routes are written to match the existing tiny_http route style, but they need a real running app smoke test.
- Atomic `.browser.json` writes are strongest on Unix-like systems. On Windows, the fallback uses remove+rename because `std::fs::rename` does not overwrite an existing file.
- URL validity is intentionally conservative. Some legitimate provider URL formats may fallback to provider home until the allowlist/path filters are tuned with real examples.
- Provider handoff, `/compact`, and context/token bar were not implemented in code in this second patch. Treat them as later commits after browser continuity stabilizes.

## Self-review notes

- Imports resolve on paper: no new heavy dependencies were added; `kimctl` only lazy-imports `urllib.parse.urlencode` inside the command branch that needs it.
- New bridge routes follow the existing `/v1/...` tiny_http match-arm style and keep token auth through the existing global auth gate.
- Restore uses provider host/path validation and refuses arbitrary stored URLs.
- UI restore operations use a sequence guard so stale async restores do not toast/navigate after a newer session/provider selection.
- File writes are documented in-code and use temp-write + rename where possible.
