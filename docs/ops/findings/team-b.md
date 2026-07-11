# Team B — Providers (Python) — Wave 1 findings

Territory: `orchestrator/providers/` (base.py, claude.py, openai_provider.py, gemini.py,
deepseek.py, ollama.py, browser_provider.py, browser/ package). Baseline: `integration/audit-fixes` @ HEAD.
Read-only hunt. Inherited findings (`inherited.md`) not re-reported; F-INH-1/2/3/4 remain valid as filed.

Severity counts: High 3 · Medium 4 · Low 7. Includes the V-3 provider conformance matrix (end of file).
Recovered leads from the lost prior run: all three verified and filed (F-B-3, F-B-4, F-B-6).
Team A handoff (F-A-2 provider-side guard): filed as F-B-2.

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

## F-B-7: Sentinel echo terminates the generation wait instantly — the prompt itself contains the literal completion hash, and Claude/Grok response selectors match user turns
- **File:** orchestrator/providers/browser/provider.py:1602-1604 (`_wait_for_generation_complete` hash check), 1487-1488 (`new_element_index`); orchestrator/providers/browser/prompt_builder.py:200-205,412 (literal hash in every prompt); orchestrator/providers/browser/site_configs.py:127-130,220-224 (selectors)
- **Severity:** High
- **Class:** bug (sentinel protocol / selector drift)
- **Evidence:** Charter question "what if the model echoes the sentinel early" — answered: it terminates the
  wait, and worse, the *user's own message* does it too. (1) `transport_marker_instruction` embeds the literal
  `[END_OF_RESPONSE_<id>]` in every injected prompt ("Always append the exact string …"). (2) Claude's primary
  response selector `[data-testid^="conversation-turn"]` (and Grok's `article`) matches USER turns as well as
  assistant turns. After submit, the user bubble appears first: `_wait_for_new_response` returns on the count
  increase, `new_element_index` points at the user bubble, and the first poll of
  `_wait_for_generation_complete` scrapes it — the echoed prompt CONTAINS the hash, so
  `norm_hash in _normalize_for_marker(current_text)` returns True immediately (this check bypasses
  `min_generation_time`). The scraped "response" is the user's own prompt: `parse_response` then matches the
  echoed "2. TASK_COMPLETE: <one-line summary>" from the [INSTRUCTIONS] block (first turn) or, when the last
  message was a tool result whose payload contains registered-tool-shaped JSON (reading Kim's own tests/docs),
  dispatches a spurious tool call — the known_tools guard does not help because the name IS registered.
  (3) Assistant-side echo is also terminal: a model that says "I'll end with `[END_OF_RESPONSE_x]`" before
  answering trips the same any-occurrence check mid-generation; `_normalize_for_marker` strips
  backticks/italics so styled echoes match MORE easily, and `strip_transport_markers` rsplits on the LAST hash
  occurrence — the early echo — discarding the whole in-flight answer. Applies to the Playwright/CDP path
  (bare CLI, headless, and the unified-Playwright chat path); the in-app bridge waits in bridge.js instead
  (same class of check worth auditing — Team D/E).
- **Fix sketch:** accept the hash only at the (normalized) TAIL of the scraped text; never accept it before
  `min_generation_time`; give Claude/Grok assistant-only primary selectors (`.font-claude-message` first) or
  filter candidate elements that contain the marker-instruction sentence verbatim.
- **Cross-territory?** partially — same-audit handoff to Team D/E for bridge.js's completion detection.

## F-B-8: Browser send is retried non-idempotently — a timeout after a delivered send re-injects the whole prompt into the same chat
- **File:** orchestrator/providers/browser/provider.py:658-663 (H6 re-raise), 1479-1485 (`TimeoutError` after submit); orchestrator/agent.py:1496-1621 (`_call_with_retry`)
- **Severity:** Medium
- **Class:** bug (retry-on-non-idempotent)
- **Evidence:** The H6 fix deliberately re-raises `TimeoutError` from the browser flow so
  `classify_provider_error` marks it retryable. But the timeout at provider.py:1483 ("No new response appeared
  after 60s") fires AFTER the prompt was successfully injected and submitted — the message is already in the
  site thread. `_call_with_retry` then calls `complete()` again (up to `max_retries`=5), which re-formats and
  re-sends the SAME content with a NEW completion hash into the SAME conversation. The site may still be
  answering the first copy: duplicate prompts pile up, responses interleave, and the count-based
  `_wait_for_new_response` baseline from the retry can latch onto the FIRST send's late answer — which carries
  the OLD hash, so the new-hash wait never resolves. Same pattern for the agent's outer
  `asyncio.wait_for(1260s)` cancellation. The bridge path has the same shape in miniature: bridge_client.py:199
  acknowledges "prompt may already be injected" on send timeout but returns NEED_HELP (safe, non-retried) — the
  CDP path instead retries automatically.
- **Fix sketch:** make post-send timeouts non-retryable for the browser provider (return a NEED_HELP that names
  the thread state), or track "delivered" state so a retry only re-polls/scrapes instead of re-sending.
- **Cross-territory?** no — Team B (agent wrapper unchanged).

## F-B-9: Auth-wall detection: title heuristics are dead code, and no re-check after the clear_chat navigation
- **File:** orchestrator/providers/browser/site_configs.py:59-74 (`detect_auth_wall(url, title="")`); orchestrator/providers/browser/provider.py:684-687 (sole call site), 695-702 (clear_chat goto)
- **Severity:** Low
- **Class:** bug / dead-code
- **Evidence:** `detect_auth_wall` accepts a `title` parameter with four `_AUTH_WALL_TITLE_MARKERS`
  ("just a moment", "attention required", …) but the only production call passes the URL alone — every
  title-based Cloudflare/interstitial detection branch is unreachable. And the check runs once at flow start:
  when `clear_chat` then navigates to the site root and the (signed-out) site redirects to its login page, the
  walled state is not re-detected; the flow proceeds to `_find_selector` and fails with the generic
  "Could not locate chat input box" RuntimeError instead of the actionable AUTH_REQUIRED message. The bridge
  path has no python-side wall check at all (relies on the Rust 409).
- **Fix sketch:** re-run `detect_auth_wall(page.url, await page.title())` after any goto and before injection.
- **Cross-territory?** no — Team B.

## F-B-10: CDP path uploads only the LAST image and clobbers the user's system clipboard every turn
- **File:** orchestrator/providers/browser/provider.py:711-716 (`image_attachments[-1]`), 1242-1252 + 1274-1281 (clipboard writes)
- **Severity:** Low
- **Class:** bug
- **Evidence:** (1) `_run_chat_flow` pastes only `image_attachments[-1]`; earlier screenshots in the same turn
  are silently dropped while the prompt text still says "[Screenshot attached]" for each — the bridge path
  supports 8 attachments, the CDP path 1, an undocumented behavioral fork of the same provider. (2) Both text
  injection and image injection write through `navigator.clipboard` on the user's REAL Chrome (CDP attach) —
  every agent turn silently overwrites whatever the user had on their system clipboard (potentially something
  they were about to paste elsewhere). No save/restore attempted.
- **Fix sketch:** loop over image attachments; note the clipboard side-effect in docs or restore the prior
  clipboard text afterwards (image restore is not feasible; a doc note may be the honest fix).
- **Cross-territory?** no — Team B.

## F-B-11: Bridge result long-poll is one-shot — a transient GET failure abandons a delivered request's answer
- **File:** orchestrator/providers/browser/bridge_client.py:213-233
- **Severity:** Low
- **Class:** bug
- **Evidence:** After a successful `/v1/send` (message delivered to the site), the `/v1/result/{req_id}` GET is
  attempted exactly once with a 720 s client timeout. Any transient failure — connection reset, bridge busy
  blip — returns a terminal NEED_HELP even though the Rust side still holds/completes the result for that
  `req_id`. The user's resend then duplicates the message into the provider thread (same non-idempotency class
  as F-B-8, but user-driven).
- **Fix sketch:** retry the GET a few times on transport errors (the send is NOT re-issued; re-polling an
  existing req_id is idempotent) before giving up.
- **Cross-territory?** no — Team B (protocol seam doc → Team H).

## F-B-12: OpenAI-compatible endpoints with a missing API key silently get "placeholder" — cryptic 401 instead of the actionable EnvironmentError
- **File:** orchestrator/providers/openai_provider.py:48-57
- **Severity:** Low
- **Class:** bug (error quality)
- **Evidence:** The missing-key guard raises only when `base_url is None`. A user who configures
  `openai_base_url` (Cerebras/Groq/Together per the class docstring) but forgets the key env gets
  `api_key="placeholder"` and a first-call HTTP 401 whose SDK message may not name the env var — versus the
  official-OpenAI path's precise "`{key_env}` is not set. Set it in .env …". Auth classification still lands
  non-retryable, but the actionable fix is hidden. (Deliberate for token-less LOCAL proxies, but that intent is
  undocumented and unconditional for remote hosts too.)
- **Fix sketch:** warn loudly at init when base_url is remote (non-localhost) and no key was found; keep the
  placeholder path for localhost proxies.
- **Cross-territory?** no — Team B.

## F-B-13: Dead config knobs: `browser_provider.max_history_messages` is read but never used; the `relay:` section is read by nothing at all
- **File:** orchestrator/providers/browser/provider.py:146; config.yaml (`browser_provider.max_history_messages`, `relay:` with `pc_api_key`/`poll_interval`/`url`)
- **Severity:** Low
- **Class:** dead-code
- **Evidence:** `self._max_history_messages` is assigned at init and referenced nowhere else in the repo —
  `format_prompt`/`build_history_recap` bound the recap by characters (`max_recap`), not message count, so a
  user tuning `max_history_messages: 6` changes nothing. Separately, the shipped config.yaml carries a `relay:`
  section (`pc_api_key`, `poll_interval`, `url`); repo-wide grep finds no reader in orchestrator/, mcp_server/,
  desktop, or cli — an empty-string "api key" field in the default config that feeds nothing (invites
  confusion about where a secret would go).
- **Fix sketch:** delete both (or wire `max_history_messages` into `build_history_recap` as a message cap).
- **Cross-territory?** relay section: Team A/W2 config cleanup; max_history_messages: Team B.

## F-B-14: base.py's typed contract (`ToolCallResponse`/`TextResponse`) omits half the fields every provider actually returns
- **File:** orchestrator/providers/base.py:76-92
- **Severity:** Low
- **Class:** contract / docs
- **Evidence:** `ToolCallResponse` declares only `type/tool/args`, but all six providers attach `content`
  (narration — the agent's PLAN/STEP stream depends on it per the H2 fixes), and API providers attach
  `stop_reason` and `usage`. `TextResponse` omits `stop_reason`. Ollama returns `usage` keys (`provider`,
  `forbid_fallback`, `billing`, optional `input`/`output`) that no other provider has and the TypedDict doesn't
  mention. The declared contract is therefore not what the agent consumes (it reads `response["content"]` on
  tool calls) — the V-3 conformance matrix below is the ground truth; the TypedDicts should be updated to match
  and become the doc of record (feeds Team H's CONTRACTS.md).
- **Fix sketch:** add `content`, `stop_reason`, `usage` as optional fields on both TypedDicts; document the
  ollama usage extension keys.
- **Cross-territory?** yes — pairs with Team H (CONTRACTS.md).

---

# V-3 — Provider response-shape conformance matrix (ground truth, this audit)

Legend: ✓ conforms to the de-facto richest shape · — field absent · n/a not applicable.
De-facto canonical shapes (what agent.py consumes):
`{"type":"text","content",stop_reason?,usage?}` ·
`{"type":"tool_call","tool","args","content"(narration),stop_reason?,usage?}` ·
multi-call → `{"tool":"batch","args":{"calls":[{tool,args},…]}}`.

| Aspect | claude | openai / deepseek | gemini | ollama | browser | fake |
|---|---|---|---|---|---|---|
| text: `stop_reason` | ✓ | ✓ (`finish_reason`) | ✓ (missing on the no-candidates path, gemini.py:470) | — (F-B-3) | — (no signal from DOM) | — |
| text: truncation honesty (`finalize_text_content`) | ✓ | ✓ | ✓ | ✗ never called (F-B-3) | n/a | n/a |
| tool_call: narration `content` | ✓ | ✓ | ✓ | ✓ | ✓ (prose around JSON) | — |
| tool_call: `stop_reason` | ✓ | ✓ | — (gemini.py:488-505) | — | — | — |
| tool_call: `usage` | ✓ | ✓ | ✓ | ✓ (divergent keys) | ✓ (estimated) | — |
| multi tool_call → `batch` | ✓ (claude.py:150) | ✓ (:190) | ✓ (:496) | ✓ (:225) | ✗ — protocol says "EXACTLY ONE"; extra JSON objects in one reply become prose `content` (first balanced match wins, response_parser.py:101) | n/a |
| malformed tool args | n/a (native dict) | `{}` + warn (F-INH-3) | n/a (native dict) | `{}` + warn (F-INH-3) | non-dict coerced `{}` (L3); unparseable → falls through to text | n/a |
| empty response | `""` / block note | `""` / block note | NEED_HELP w/ blockReason (M2) or `""` | `""` | NEED_HELP text | n/a |
| errors | raise SDK exc (timeout re-wrapped ✓) | raise SDK exc (timeout re-wrapped ✓) | raise RuntimeError — OAuth label poisons classification (F-B-1) | raise mixed; raw httpx timeouts misclassified (F-B-5) | **returns** NEED_HELP text for connect/IO failures; raises only TimeoutError (retry non-idempotent, F-B-8) | scripted |
| usage key set | input/output/cache_creation/cache_read (additive cache) | same (cache_read subset; DeepSeek cache always 0 — known) | same (cache_read subset) | provider/source/model/mode/usage_available/forbid_fallback/billing/input?/output?/durations | input/estimated/source/output | — |
| auth failure surface | raise 401 → "auth" | raise 401 → "auth" | EnvironmentError w/ actionable text ✓ (but see F-INH-1) | PermissionError "Sign in to Ollama…" ✓ | NEED_HELP AUTH_REQUIRED (Rb3) / bridge 409 | n/a |

**Cross-cutting contract facts:** (1) The browser provider is the only one whose *errors are in-band text*
(`NEED_HELP:` strings) rather than exceptions — the agent must string-match; this is load-bearing and
undocumented (→ Team H). (2) `usage` consumers all use `.get()` so the ollama/browser divergence is tolerated,
not validated. (3) `stop_reason` has NO consumer in agent.py — it is only meaningful via
`finalize_text_content` inside providers, which is exactly why ollama skipping it (F-B-3) is invisible today.
(4) Model-ID drift-watch (unverifiable offline): default `gemini: gemini-2.0-flash` is the oldest configured
default and the likeliest to be retired; claude/openai/deepseek defaults look current.

# Checked and clean (no finding)

- base.py `classify_provider_error` ordering (auth-before-OSError, digit-bounded 400/429/5xx, JSONDecodeError
  → retryable) — sound except the F-B-1 label collision; agent-side asyncio.TimeoutError normalization present.
- `create_provider` factory: KIM_FAKE gate, `browser:<site>[:tier]` parsing, unknown-name error — clean.
- Agent `_call_with_retry`: bounded (5), exponential with jitter, no sleep after final attempt, honest
  rate-limit vs other-retry status lines — clean (except the F-B-8 idempotency interaction).
- ollama #38 (delta accumulator slots) and #40 (FIFO pending tool-call pairing) — correct for same-name
  interleaving; only the image-branch bypass (F-B-4) breaks pairing.
- response_parser known_tools prompt-injection guard (#38) applied to fenced AND bare JSON — clean;
  `strip_transport_markers` last-occurrence anchoring (L6) correct given F-B-7 is fixed upstream.
- prompt_builder trim ladder (4.2): marker instruction and task survive pathological budgets — clean.
- markdown_scraper fence reconstruction — covered by DOM-fixture contract tests.
- bridge_client attachment-honesty gating (`attachments_uploaded`) and oversize/8-cap limits — clean.
- Secrets: no provider logs key material; gemini truncates/extracts Google error bodies; bridge token only in
  headers — clean.
