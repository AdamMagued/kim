# Team B — Providers (Python) — Wave 1 findings

Territory: `orchestrator/providers/` (base.py, claude.py, openai_provider.py, gemini.py,
deepseek.py, ollama.py, browser_provider.py, browser/ package). Baseline: `integration/audit-fixes` @ HEAD.
Read-only hunt. Inherited findings (`inherited.md`) not re-reported; F-INH-1/2/3/4 remain valid as filed.

Status: IN PROGRESS — findings banked incrementally (session-limit resilience). Browser package deep-dive follows.

---

## F-B-1: Every Gemini OAuth HTTP failure is misclassified as non-retryable "auth" — the error label itself trips the auth regex
- **File:** orchestrator/providers/gemini.py:236,275 (label "Gemini OAuth API" → RuntimeError) + orchestrator/providers/base.py:183-189 (`_AUTH_WORD_RE`)
- **Severity:** High
- **Class:** bug (retry/classification)
- **Evidence:** `_complete_oauth` passes `error_label="Gemini OAuth API"` to `_post_rest`, which wraps every
  `urllib.error.HTTPError` as `RuntimeError(f"{error_label} error: HTTP {exc.code}: …")` (line 275). The agent's
  `_call_with_retry` classifies via `classify_provider_error`, whose **auth check runs first** and matches
  `\b(auth|oauth)\b` — and the message always contains the standalone word "oauth" ("gemini oauth api error: …").
  Result: HTTP 429 (shared Kim quota exhausted — the common failure for the default Google-sign-in flow), 500,
  502, 503, 529 in OAuth mode are ALL classified `ProviderError("auth", retryable=False)`. No retry/backoff ever
  happens, and the user sees an authentication failure for a transient overload. The `oauth_user_project` 429 is
  pre-classified (M10 fix, line 263) but plain `oauth` mode — Kim's primary Google flow — is not, and 5xx is not
  covered in either OAuth mode. API-key mode is unaffected (label "Gemini API" has no `\boauth\b` hit).
- **Fix sketch:** classify by HTTP status before wrapping (raise pre-classified `ProviderError` from `_post_rest`
  for 429/5xx like the M10 branch), or change the label to "GeminiOAuth" / check status codes before auth words in
  `classify_provider_error`.
- **Cross-territory?** no — Team B both files.

## F-B-2: claude.py sends the message list verbatim — leading-assistant history (trimmed resume) 400s on Anthropic (provider-side guard for F-A-2)
- **File:** orchestrator/providers/claude.py:37-46, 69-93 (`_to_claude_messages`)
- **Severity:** High
- **Class:** bug (Anthropic API contract)
- **Evidence:** Confirms the Team A handoff (F-A-2). `_to_claude_messages` transforms roles/content 1:1 with no
  normalization: no guard that `messages[0]` has role `user`, no merging of consecutive same-role turns. When
  `memory._enforce_limits`/`_fix_tool_boundary` walks the trim boundary back to include an assistant tool_call
  turn, the provider submits `[assistant, user, …]` and Anthropic rejects with 400 ("first message must use the
  user role"), which `classify_provider_error` marks `invalid_request`/non-retryable — the whole resumed session
  is bricked until compaction changes the window. Root cause is Team A's (memory.py); the provider owns the
  belt-and-suspenders: every other provider tolerates this shape, so claude.py is the only one that turns a
  memory-trim artifact into a hard failure.
- **Fix sketch:** in `_to_claude_messages`, if the first canonical message is assistant, prepend a synthetic
  `{"role": "user", "content": "[conversation resumed mid-exchange]"}` (or drop the orphan assistant turn);
  optionally also merge consecutive same-role messages, which Anthropic likewise rejects.
- **Cross-territory?** yes — root fix Team A (memory.py); this guard Team B.

## F-B-3: Ollama ignores `done_reason` — truncated/length-stopped answers presented as complete (honesty fix 3.1 never applied here)
- **File:** orchestrator/providers/ollama.py:246-250 (`complete` text return), 504-516 (`_stream_chat` final object)
- **Severity:** Medium
- **Class:** bug (contract conformance)
- **Evidence:** Recovered lead from lost run — CONFIRMED. Ollama's final stream chunk carries
  `done_reason: "stop" | "length" | "load"`. `_stream_chat` keeps `final_obj` but nothing ever reads
  `done_reason`; `complete()` returns `{"type": "text", "content": content, "usage": …}` with **no
  `stop_reason` key**, and ollama.py is the only API provider that never imports/calls
  `finalize_text_content` (claude.py:166, openai_provider.py:216, gemini.py:525 all do). A
  `num_ctx`-clipped or output-limit-clipped reply therefore reaches the agent as a complete answer — exactly the
  masquerade the cross-provider "finding 3.1" fix eliminated elsewhere. This matters most on Ollama since small
  local contexts make truncation the norm, not the edge case. Also breaks the response-shape matrix (see V-3
  matrix below): text responses from Ollama lack `stop_reason`; tool_call responses lack it too.
- **Fix sketch:** map `done_reason == "length"` → `stop_reason="length"` and pass content through
  `finalize_text_content(content, stop_reason)`; include `stop_reason` in both return shapes.
- **Cross-territory?** no — Team B.

## F-B-4: Ollama tool-result pairing skips any tool result that contains an image — pending tool_call left unanswered, strict-server 400s
- **File:** orchestrator/providers/ollama.py:389-417 (`_to_ollama_messages` list-content branch)
- **Severity:** Medium
- **Class:** bug
- **Evidence:** Recovered lead — CONFIRMED, sharper form. In the list-content branch, the tool-result
  conversion (`_match_pending` + `_tool_result_message`) only runs in the `else:` arm of `if images:` (line
  411). A tool result carrying a screenshot (`screen_capture`, `web_screenshot` — canonical
  `[{"type":"text","text":"[Tool result: screen_capture]…"},{"type":"image",…}]`) is emitted as a plain
  `role:"user"` message with `images`, and its assistant `tool_calls` entry stays in `pending_calls` forever.
  Consequences: (a) OpenAI-compatible strict servers (ollama cloud models proxying such semantics) can 400 on an
  assistant tool_call with no following `role:"tool"` message; (b) the stale pending entry is popped by the NEXT
  result of the same tool name, so a later screenshot's result is paired with the EARLIER call's id — off-by-one
  id cascade for every repeated vision tool. Related false positive in the same machinery: `_TOOL_RESULT_RE`
  matches any **user-typed** message starting with `[Tool result: x]` (e.g. a user pasting a previous
  transcript), silently converting real user input into a `role:"tool"` message with a fabricated
  `tool_call_id` that has no matching call.
- **Fix sketch:** run the tool-result match before/independent of the `images` check and attach images to the
  `role:"tool"` message (Ollama supports `images` on any message); only treat text as a tool result when a
  pending call of that name exists (fixes the user-typed false positive too).
- **Cross-territory?** no — Team B.

## F-B-5: httpx timeouts (Ollama's transport) classify as "unknown"/non-retryable — `"timed out"` doesn't contain `"timeout"`
- **File:** orchestrator/providers/ollama.py:443-516 (no try/except around stream), orchestrator/providers/base.py:215-218
- **Severity:** Medium
- **Class:** bug (retry/classification)
- **Evidence:** ollama.py never catches httpx transport errors in `_stream_chat`/`_fetch_tags` connect/read
  paths beyond the `/api/version` liveness probe. A mid-generation `httpx.ReadTimeout`/`ConnectTimeout`
  propagates raw to `classify_provider_error`, where every branch misses it: it is **not** a builtin
  `TimeoutError` (httpx exceptions derive from `httpx.HTTPError` → `Exception`), not a `ConnectionError`/`OSError`,
  and its `str()` is typically empty or "timed out" — and the substring check at base.py:215 looks for
  `"timeout"`, which `"timed out"` does not contain. Result: `ProviderError("unknown", retryable=False)` for the
  single most transient failure class a local daemon has (model cold-load can exceed connect windows; 600 s read
  ceiling). Claude/OpenAI providers explicitly re-wrap their SDK timeout as builtin `TimeoutError` for exactly
  this reason (claude.py:55-58, openai_provider.py:93-96); Ollama skipped the same treatment.
- **Fix sketch:** wrap the httpx calls and re-raise `httpx.TimeoutException` as builtin `TimeoutError` (mirroring
  claude.py), or add `"timed out"` + `isinstance(error, httpx.TimeoutException)`-by-name to the classifier.
- **Cross-territory?** no — Team B.

## F-B-6: Context-limit probe runs local `ollama ps` even when base_url points at a remote daemon
- **File:** orchestrator/providers/ollama.py:585-598 (`_context_limit_from_ps_sync`), 132-134 (base_url config)
- **Severity:** Low
- **Class:** bug
- **Evidence:** Recovered lead — CONFIRMED. `KIM_OLLAMA_BASE_URL`/`ollama.base_url` lets all HTTP traffic target
  a remote daemon, but `_context_limit_from_ps_sync` shells out to the **local** `ollama` CLI with no
  `OLLAMA_HOST` derived from `self._base_url`. The CLI answers for localhost (or errors), so the reported
  `context_limit`/`context_limit_source="ollama_ps"` can describe a different daemon's loaded model — the
  context meter then budgets against the wrong window. Same subprocess also fails silently when the CLI binary
  isn't on PATH while a remote daemon is fully healthy. Adjacent cosmetic wart: `_ensure_daemon_running`'s
  message "Ollama is installed but not running" is wrong for remote base_urls (network down ≠ not running) and
  for machines where Ollama isn't installed at all.
- **Fix sketch:** pass `env={**os.environ, "OLLAMA_HOST": self._base_url}` to the subprocess, or skip the `ps`
  path entirely when base_url != localhost and rely on `/api/show`.
- **Cross-territory?** no — Team B.
