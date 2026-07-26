# Kim Engine — End-to-End Audit Report

**Scope:** `codex_engine/`, `orchestrator/providers/browser/`, `chrome_extension/`
**Branch:** `audit/kim-engine-hardening` (from `a48fe67`)
**Interpreter:** repo `venv/` — Python 3.12.12. *(The system `python3` is 3.9.6 and
cannot even import this codebase: `X | None` annotations fail at runtime. Any
`pytest` run must use `./venv/bin/python`.)*

---

## 1. Headline

`dd3fa9e` — the last commit before the eight-commit bridge series — had a fully
green suite. The series that followed broke 63 tests, including the proxy's own
bearer-token security contract.

| Ref | Passed | Failed | Wall clock |
|---|---|---|---|
| `dd3fa9e` (pre-series baseline) | 2643 | **0** | 104 s |
| `a48fe67` (current `origin/main`) | 2584 | **63** | 451 s |
| `audit/kim-engine-hardening` | **2656** | **15** | **117 s** |

48 of the 63 regressions are fixed. **All 15 remaining failures were verified,
per test, to fail identically at `a48fe67` before any change of mine** — none is
new breakage (§5).

The 451 s → 117 s suite time is not a test artifact. At `a48fe67` every browser
completion spent 15 s inside `wait_for_connection` waiting for a Chrome
extension that would never connect, then failed with `NEED_HELP`. That was a
**15 s-per-turn production cost on every non-extension deployment**, not just in
tests.

---

## 2. Regressions restored

Each was a deletion or rewrite with no replacement, pinned by a test that passed
at `dd3fa9e`.

### 2.1 `_check_auth` — the per-run bearer token was a no-op (security)

```python
if not auth or auth == "Bearer ":
    return True
return hmac.compare_digest(auth, expected) or auth.startswith("Bearer ")
```

An absent `Authorization` header passed, and so did *any* string beginning with
`"Bearer "`. The proxy drives an authenticated ChatGPT Web session, so this let
any local process send prompts through the user's account. `dd3fa9e` had a plain
`hmac.compare_digest`; `tests/test_standalone_proxy.py::test_bearer_auth_enforced`
has been failing since. **Restored.** Verified live: no header, `Bearer `, and a
wrong token all return 401; the real token returns 200.

`CODEX_BRIDGE_SETUP.md` told users to configure `api_key = "dummy"`, which the
restored check rejects — the doc now explains the ready-line token. **See §7:
this requires action on your live setup.**

### 2.2 `_normalize_tool_calls` — half the function was unreachable

The line that populated the lookup table was deleted:

```python
name = tool.get("name") or fn.get("name")
#  ← by_name[str(name)] = tool   (deleted)
```

`by_name` was therefore permanently `{}`, which made everything gated on it dead
code — including the schema-driven argument coercion and the **F-H-7
`jsonschema.validate` call**. Malformed tool inputs reached codex unchecked, and
a request whose exec tool is named anything other than `exec_command` never got
its arguments mapped. **Restored**, with the `if not by_name: return tool_calls`
passthrough guard.

### 2.3 `_extract_shell_blocks` — prose was executed as shell (safety)

The conservative fragment fallback was replaced with:

```python
m = re.search(r"\b(printf|cat|echo|ls|...|open)\b.*$", line_str, re.IGNORECASE)
```

run against **every line of prose**. So a narration line — the terminal system
prompt explicitly asks for one — like *"you can open the file in your editor"*
was lifted out and executed as `open the file in your editor`. That
`_SAFE_BARE_CMD_RE` (line 1524) survived with no remaining consumer confirms
this was a drive-by, not a design change.

`_chatgpt_terminal_system_prompt()` requires a real ` ```bash ` fence, which
`_SHELL_FENCE_RE` already handles, so the fallback is only ever needed for a
fence fragment. **Restored to the anchored, single-line form.**

### 2.4 `_use_webview_bridge = True` — broke every CDP deployment

Hard-wiring this sent *every* `BrowserProvider` down the HTTP webview path.
Deployments that use CDP/playwright have no `KIM_WEBVIEW_BRIDGE_URL`, so they
POSTed to `http:///v1/send` and every completion came back as
`NEED_HELP: Bridge /v1/send failed — Request URL is missing an 'http://' ...`.
This alone accounted for 20 of the 63 failures.

Now requires a transport that actually exists: a configured desktop bridge, or
`KIM_EXTENSION_BRIDGE=1` (set by `scripts/start_codex_proxy.sh`).

### 2.5 `request.content_type` read unconditionally

`request` is duck-typed here; a hard attribute read turned every lightweight
caller into an `AttributeError` 500. Now `getattr(..., "application/json")`,
keeping the hardening.

### 2.6 Title interception returned the wrong protocol shape

Found by driving a live proxy, not by reading. The background-title branch was
the only exit from `_handle_responses` returning the raw provider dict
`{"type": "text", "content": ...}` instead of a Responses payload — codex reads
`output[]` and found nothing there. Now goes through `_make_responses_text_reply`;
answers in **~15 ms with zero provider calls**.

`_generate_stateless_title` also only handled `content` as a bare string. Codex
Desktop sends a block list, so `"User prompt:" in text` matched nothing and
**every title came out as "Coding Task"**. Now reads block lists.

---

## 3. New hardening (no prior coverage)

### 3.1 Thread continuity — compaction hijacked the live chat

The strongest new finding. `compaction._summarize_messages` calls the provider
with `clear_chat=True` to keep the summary out of the user's chat. On the
extension bridge that clears `_current_conversation_id`/`_current_message_id`
**and then stores the throwaway summarizer thread's ids in their place**. Every
turn after a compaction silently continued inside the summarizer's chat — and it
fires precisely when a session is long enough for it to matter.

Fixed with `extension_bridge.preserved_thread_state()`, an async context manager
that snapshots and restores the pointers around any such side-call.

### 3.2 `session_id` state leakage between codex sessions

```python
if session_id and self._current_codex_session_id == session_id:
    is_reset = False
```

A *matching* id suppressed the reset heuristic (correct — Codex Desktop rewrites
environmental context between turns). A *differing* id did nothing, so a second
codex session with a similar item count inherited the first session's relay
cursor, cached reply, and loop guard. A session change now forces a reset.

### 3.3 `_handle_responses` had no concurrency guard

The relay bookkeeping (`_last_sent_count`, `_relay_count`, `_last_proxy_response`)
is a read-modify-write across several awaits. Two overlapping requests
interleaved those updates and corrupted the sent-cursor — duplicated or skipped
items in the next prompt. Now serialized on a per-proxy `asyncio.Lock`.

### 3.4 Extension bridge resilience

| Defect | Effect | Fix |
|---|---|---|
| Pending futures never failed on disconnect | Every caller blocked the full **180 s** timeout after a tab close / extension reload | `_fail_pending()` on disconnect and on takeover by a new socket |
| Register-before-send with no unwind | One orphaned future + callback leaked **per failed send**, forever | `try/except` unwinds the registration |
| Delta callback called inside `async for` | One raising consumer escaped the read loop and **disconnected the extension** for every other in-flight request | Dispatch moved to `_handle_response_frame`, exceptions contained |
| Cancel sent only on `CancelledError` | A timed-out turn kept generating in the tab and burned the next `parent_message_id` | Cancel on `TimeoutError` too |
| `wait_for_connection` polled every 0.5 s | Up to 500 ms added to every turn racing a reconnect | `asyncio.Event` |
| `get_extension_bridge()` unguarded | Two concurrent first-callers raced to bind port 10533 | `asyncio.Lock` |
| `stop()` left `_runner` set | `start()` became a permanent no-op afterwards | Cleared |
| Extension answered **any** site | A `browser:gemini` request was answered by whatever ChatGPT tab was open — a silent provider swap | `_extension_bridge_serves()` gate |
| Attachments silently dropped | Model answered "describe this screenshot" without one | Explicit not-delivered note + `attachments_uploaded = 0` |
| No empty-response guard | Empty text parsed as a blank final answer; codex ended the turn having done nothing | `NEED_HELP` with a diagnosis |

### 3.5 `chrome_extension/injected.js`

- **`streamState.messageId` was never populated.** `cancelChatGPTStream` is
  guarded on `conversationId && messageId`, so **the backend cancel never fired**
  — cancelling stopped Kim reading, not ChatGPT generating. Ids are now published
  mid-stream via an `onIds` callback.
- **`messageId` was captured outside the `isAssistant` guard**, so a user echo or
  trailing system message could become the next turn's `parent_message_id` and
  fork the thread into a branch. Assistant messages only now.
- **Cancel produced a bogus `done`.** `reader.cancel()` makes the next `read()`
  resolve `{done: true}`, walking `parseSSEStream` into `onDone`, which posted a
  normal-looking completion carrying whatever partial text had arrived. Now
  gated on a `cancelled` flag.
- **PoW exhaustion returned a bogus token silently** — the failure surfaced only
  as an unexplained HTTP error later. Now logs the real cause.
- Duplicate `_origFetch`/`originalFetch` captures collapsed to one.

### 3.6 `chrome_extension/content.js`

- **Unbounded outgoing queue.** With the proxy down, every frame was queued
  forever; a long streaming turn produces thousands of deltas. Capped at 200,
  dropping deltas first (a reconnected proxy can still act on `done`/`error`;
  stale deltas belong to a request that no longer exists).
- **`onerror` leaked a live socket** that later fired its own `onclose` and
  queued a *second* reconnect, halving the backoff on every error.

---

## 4. Performance

**PoW base64 encoder — measured, `node`, 200 000 iterations:**

| | ms | µs/call |
|---|---|---|
| before | 486 | 2.43 |
| after | 282 | 1.41 |

**1.73× faster (42 % less time), byte-identical output** (asserted in the
harness). `utf8Base64Encode` is the hot path of `solvePoW`'s 500 000-iteration
loop and was allocating a fresh `TextEncoder` on every call and appending one
character at a time. Now hoisted, with chunked `fromCharCode.apply`.

**Suite wall clock 451 s → 117 s**, from removing the 15 s-per-completion
extension-bridge wait on deployments that never had an extension (§2.4).

Everything else in this path is I/O-bound on the browser round-trip and is not
measurable without a live signed-in Chrome session. No other performance claim
is made.

---

## 5. Remaining 15 failures — all pre-existing at `a48fe67`

Verified by running these exact node ids in an `a48fe67` worktree: **15 failed,
0 passed.** None is caused by this branch.

**A. Deliberate protocol change (13) — needs a product decision, not a fix.**
`_system_prompt_for` now routes ChatGPT to `_chatgpt_terminal_system_prompt`
(bash-per-turn) instead of the shared JSON contract; `_CONTRACT_NUDGE` was
rewritten to ask for bash; a `_SELF_HELP_RE` gate was added before nudging; and
`tools=body.get("tools", [])` is now forwarded to the provider. These are
coherent, commit-message-named, and `start_codex_proxy.sh` +
`CODEX_BRIDGE_SETUP.md` are built on them. The tests below encode the *prior*
one-protocol contract. **I left them failing deliberately** — silently editing
the assertions would launder a design change into a green build.

- `test_browser_protocol.py`: `test_system_prompt_selector_uses_exact_json_contract_for_chatgpt`, `test_codex_prompt_selects_codex_layout_not_chat_layout`, `test_terminal_prompt_selects_codex_layout_not_chat_layout`
- `test_codex_stateful_threads.py`: `TestChatgptContractNudge` ×2, `TestContractNudge` ×3, `TestDoneSkipsNudge::test_done_answer_to_nudge_does_not_burn_thread`, `TestRepeatedCommandLoopGuard` ×2
- `test_browser_auth_wall.py::RepairMetricsTest::test_nudge_counts_into_thread_state`
- `test_stateful_browser_threads.py::test_handoff_block_in_codex_bridge_prompt`

**Decision needed:** either accept the terminal protocol for ChatGPT and rewrite
these tests to encode it, or revert `_system_prompt_for`. Both are defensible;
it is a product call, not an audit call.

**B. Unfixed regression, outside this audit's path (2).**
`test_appserver_real_binary.py::RealBinaryAppServerSmoke::test_native_approval_{accept,declined}*`
— passes at `dd3fa9e`, fails at `a48fe67`. The real codex binary executes an
escalated command without emitting `command_approval_request`. This is the
app-server approval flow, not the browser bridge; **root cause not identified.**

---

## 6. Negative findings (checked, no defect)

Recording these so the vectors are closed rather than silently skipped.

- **Delta-prompt backtracking (Vector B) — unfounded.** `_extract_delta_prompt`
  contains no regex at all; it is a bounded `str(output)[:6000]` slice per item.
  There is no catastrophic-backtracking exposure on large command outputs.
- **JSON repair (Vector D) — degrades safely.** `_parse_contract` falls back to
  `json_repair` and `_coerce_contract_dict` returns `None` for anything that
  isn't a dict or a list of dicts. Prose containing braces yields a dict with no
  `text`/`tool_calls`, which the converter renders as plain text.
- **Streaming is wired but unconsumed.** `send_completion` has exactly one caller
  (`bridge_client.py:88`) and it passes no `on_delta`. The extension streams,
  `content.js` relays, the bridge dispatches — and nothing on the Python side
  listens. The path is now hardened, but **token-by-token streaming to codex is
  not implemented today.** Codex sees one complete reply per turn.
- **Premature `DONE` (Vector D) — inspected, left alone.** `_DONE_RE` matches a
  standalone `Done.` *anywhere* in a reply, guarded only by `_FILE_WRITE_RE`.
  This looks wrong but is load-bearing tuning against a specific browser-chat
  hang. Changing it needs a concrete failing multi-step case, which I could not
  produce without a live session. Flagged, not touched.
- Dead code removed: `_TERMINAL_NUDGE` (unreferenced since `_CONTRACT_NUDGE` was
  rewritten), a duplicated `stream = bool(...)`, a redundant local `import shlex`
  shadowing the module-level one.

---

## 7. Required action on your live setup

1. **Reload the unpacked extension** at `chrome://extensions`. Unpacked
   extensions do not hot-reload — none of the `injected.js`/`content.js` fixes
   take effect until you do.
2. **Launch via `./scripts/start_codex_proxy.sh`.** It now exports
   `KIM_EXTENSION_BRIDGE=1`; other launch paths will use the CDP path instead of
   the extension bridge.
3. **Stop using `api_key = "dummy"`.** The restored auth check returns 401 for
   it. Copy the `token` from the proxy's first stdout line into
   `OPENAI_API_KEY` / `CODEX_API_KEY`. It is regenerated on every proxy start —
   do not hard-code it in `~/.zshrc`.

---

## 8. Verification performed

- `./venv/bin/python -m pytest tests/` — 2656 passed, 15 failed (all §5), 3 skipped
- 24 new regression tests in `tests/test_extension_bridge_hardening.py`
- `py_compile` over all of `codex_engine/` and `orchestrator/providers/browser/`
- `node --check` over all of `chrome_extension/`
- Both CI flake8 gates clean (strict 120-char on `orchestrator/`; errors-only on
  `codex_engine/`, `tests/`) — including one pre-existing E501 in
  `orchestrator/providers/base.py` that would have failed CI
- Live proxy smoke test: handshake, 401 on three bad-auth shapes, 200 on the
  real token, WS bridge listening on 10533, title interception returning a valid
  Responses payload in ~15 ms with no provider call
- PoW encoder benchmark with an equivalence assertion
