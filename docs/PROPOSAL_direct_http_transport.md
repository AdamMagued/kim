# PROPOSAL: Direct-HTTP / Private-API Transport for the Browser Provider

**Status:** Feasibility investigation (read-only). No code changed.
**Branch:** `integration/audit-fixes`
**Author:** Claude Fable 5 (research agent)
**Date:** 2026-07-12

---

## 0. The question

Today Kim's "browser provider" mode opens a **real Chrome window** and drives the
Claude / ChatGPT / Gemini / Grok **web UIs** via CDP/Playwright (or the in-app
Tauri webview bridge): it injects the prompt into the page's editor, waits for
the page to finish rendering, scrapes the rendered DOM, and parses the text back
into Kim's canonical response format.

The owner asks: **instead of opening a browser, can Kim make direct HTTP requests
to those services — treating the logged-in web session like an API — so that no
browser window has to open at all?**

This document answers that decisively, per provider, with an honest treatment of
the middle-ground options (headless vs pure-HTTP vs hybrid), the maintenance/ToS
risk, and a concrete architecture sketch if any path is worth building.

**Bottom line up front (BLUF):**

- **Pure HTTP against the private web endpoints is NOT viable as a general
  replacement.** Every one of the four target services sits behind at least one
  anti-automation layer that a raw `httpx` client cannot satisfy without
  executing browser-origin JavaScript (Cloudflare Turnstile / `__cf` clearance,
  ChatGPT's Arkose + Sentinel proof-of-work `requirements` token, Google's
  `at`/`SNlM0e` per-page tokens + `batchexecute` envelope, x.com's client
  transaction-id header + guest/CSRF tokens). These tokens are *minted by the
  page*, not by a login cookie, so a cookie jar alone is insufficient.
- **The owner's *real* goal — "no window pops up" — is fully achievable today
  without defeating any anti-bot, by running the existing Playwright path
  headless.** That is the pragmatic 80/20 and it also closes two live security
  findings (F-J-3 orphan CDP Chrome, F-I-4 unauthenticated CDP :9222).
- **A narrow hybrid is defensible:** one provider (Claude.ai is the best
  candidate) *might* sustain a cookie-jar HTTP transport as an optional
  fast-path, with automatic fallback to the headless browser. This is a spike,
  not a commitment.

The recommendation (Section 6) is: **build headless-first; optionally spike a
Claude.ai HTTP fast-path behind a flag; do not attempt a pure-HTTP replacement
for ChatGPT / Gemini / Grok.**

---

## 1. Current architecture — what a replacement transport must satisfy

### 1.1 The provider interface (the drop-in contract)

`orchestrator/providers/base.py` defines the contract every transport must meet:

```python
class BaseProvider(ABC):
    native_tool_calling: bool = False
    lean_system_prompt: bool = False

    @abstractmethod
    async def complete(self, messages, tools, system) -> dict: ...
```

`complete()` returns one canonical dict:

- `{"type": "tool_call", "tool": str, "args": dict}`  — the model asked to run a tool
- `{"type": "text", "content": str, "usage"?: dict}`   — a text completion

Providers are constructed by `create_provider(name, config)` (base.py:246). The
browser provider is reached via `"browser"` or `"browser:<site>"` (which sets
`browser_provider.preferred_site`). A new transport plugs in **exactly here**.

Key point: **the interface is not streaming and not tool-native.** The agent
loop calls `complete()` and gets one dict back per turn. So a replacement
transport does **not** have to expose SSE to the agent — it only has to *consume*
whatever streaming the remote uses internally and return the final parsed dict.
That substantially lowers the bar for an HTTP transport (no need to re-plumb
streaming up into the loop).

### 1.2 What the browser provider actually does (the parts a replacement must replicate)

`BrowserProvider.complete()` (`orchestrator/providers/browser/provider.py`) is
**not** a thin HTTP wrapper — it is a text-in / text-out adapter around a chat
*UI*. It layers a large amount of protocol on top of a dumb "paste text, scrape
text" channel. A replacement transport inherits every one of these
responsibilities:

1. **Prompt flattening (`prompt_builder.format_prompt`).** The entire canonical
   conversation (system + history + tools + the current turn) is flattened into
   **one big text message**, because the web UI only accepts one text box. This
   includes:
   - The system prompt (sent once per thread; tracked by `_sent_system_prompt`).
   - An `[AVAILABLE TOOLS]` JSON block — the web model has no native tool-calling,
     so tools are described in-band and the model is asked to *emit JSON* to call
     one.
   - A response contract: the model must answer as a raw JSON tool call, or
     `TASK_COMPLETE: …`, or `NEED_HELP: …`.
   - A **completion sentinel**: `transport_marker_instruction()` tells the model
     to append the literal string `[END_OF_RESPONSE_<8hex>]` at the very end.

2. **The completion-hash / sentinel protocol.** Because there is no HTTP
   "response complete" signal when scraping a live DOM, the provider needs to
   know when the model has *finished* generating. It uses three overlapping
   heuristics (`_wait_for_generation_complete`): (a) the sentinel appears in the
   scraped text (definitive), (b) the site's Stop button appeared then cleared
   (UI signal), (c) the text length settled for N idle polls (fallback).
   This whole machine exists **only because scraping has no end-of-stream
   marker.** A real HTTP transport with SSE gets end-of-stream for free and can
   *drop the sentinel entirely* (see F-B-7 below — the sentinel is also a live
   bug source).

3. **Response parsing (`response_parser.parse_response`).** Scraped markdown →
   canonical dict. Parses fenced ```json blocks and bare `{"tool": …}` first
   (with a `known_tools` allow-list as a prompt-injection guard, #38), then
   `TASK_COMPLETE:` / `NEED_HELP:` prefixes, else plain text. `strip_transport_markers`
   removes the sentinel(s) and anchors on the current turn's hash.

4. **Session / thread statefulness.** The provider is stateful across turns:
   `_sent_system_prompt`, `_last_chat_page_url`, `_last_chat_site`,
   `reset_session()`, `mark_thread_continuation()`, `start_fresh_chat()`
   (compaction rollover), and `clear_chat`. A conversation lives in a *remote
   thread* (a claude.ai conversation UUID, a chatgpt.com/c/<id>, …); Kim appends
   delta turns to it rather than resending full history. **Any replacement must
   preserve this remote-thread model** — including "don't resend the system
   prompt if the thread already has it" and "on compaction, roll to a fresh
   remote thread."

5. **Auth-wall detection (`site_configs.detect_auth_wall`).** If the tab is on a
   sign-in / Cloudflare-challenge URL, fail fast with an actionable
   `NEED_HELP: AUTH_REQUIRED` instead of hanging 600s.

6. **Multimodal upload.** Screenshots are pasted via clipboard (`_inject_image_clipboard`),
   with honesty guards if the paste fails.

7. **Usage estimation.** `_estimate_prompt_usage` / `_attach_usage` — token
   counts are *estimated* (there is no billing API), tagged `"estimated": True`.

### 1.3 Two existing transports already implement this contract

There are already **two** back-ends behind `BrowserProvider`, selected in
`complete()`:

- **In-app webview bridge (`bridge_client.complete_via_webview_bridge`)** — the
  primary desktop path. Talks to a **loopback HTTP bridge** (`KIM_WEBVIEW_BRIDGE_URL`
  + `KIM_WEBVIEW_BRIDGE_TOKEN`) that the Tauri shell hosts; the Rust side drives
  the app's own webview (`bridge.js` / `PERSISTENT_BRIDGE_JS`) to inject + wait +
  scrape. Split `POST /v1/send` → long-poll `GET /v1/result/{req_id}`, with a
  legacy `POST /v1/complete` fallback.
- **Playwright/CDP (`_run_chat_flow`)** — connects to an external Chrome on
  `:9222` (or auto-launches one, or launches headless Chromium), finds the chat
  tab, and does the inject/wait/scrape directly.

**This is the crucial precedent:** the provider is *already* structured as
"pick a transport, get raw response text back, parse it." A third transport —
direct HTTP to the private API — would slot in as a sibling to these two. The
seam already exists.

### 1.4 How the codex bridge consumes it

`orchestrator/codex_bridge_service.py` builds the provider with
`create_provider(args.provider, config)` where `args.provider` is e.g.
`browser:gemini`, and uses it as an OpenAI-compatible backend for Codex runs
(the "Code tab" — constrained by CLAUDE.md to *only* ollama-cloud or the browser
provider, never OpenAI auth). So any new transport is automatically reachable
from the codex path too, as long as it is registered in `create_provider` under
a `browser*`-style name and returns the same canonical dict.

### 1.5 The exact drop-in checklist

A direct-HTTP transport is a true drop-in **iff** it provides:

| Responsibility | Browser provider mechanism | HTTP transport must… |
|---|---|---|
| `complete()` returns canonical dict | `parse_response` | Same — parse the model's text (from SSE) into `{type: tool_call/text}` |
| Tool calling | In-band `[AVAILABLE TOOLS]` + JSON contract | **Same in-band trick** — these are consumer web endpoints with no tool API |
| Streaming/end-of-stream | Sentinel + stop-button + idle heuristics | **Free** — SSE `data:` frames + terminal event; sentinel can be dropped |
| Remote-thread statefulness | conversation UUID / URL tracking | Track the provider's conversation id from the create/response JSON |
| System-prompt-once | `_sent_system_prompt` | Same flag; send system as first message in the remote thread |
| Compaction rollover | `start_fresh_chat()` | Create a new remote conversation id |
| Auth | logged-in browser cookies | **The hard part** — obtain + persist session cookies AND page-minted tokens |
| Auth-wall / expiry | `detect_auth_wall` | Detect 401/403/challenge JSON and return `NEED_HELP: AUTH_REQUIRED` |
| Multimodal | clipboard paste | Provider file-upload endpoint (per-provider, more work) |
| Usage | estimated | Same estimate (no billing API on web endpoints) |

The single load-bearing difference is **Auth** (and the anti-automation tokens
bound up with it). Everything else is equal or *easier* over HTTP. Section 2
therefore concentrates on auth + anti-bot per provider.

---

## 2. Per-provider investigation — direct HTTP / private-API replay

**Method + honesty caveat:** I have **no network access** to these services from
this environment, so the endpoint shapes, token names, and anti-bot mechanisms
below are reconstructed from (a) Kim's own code, which already names several of
these (e.g. the Gemini `authuser` routing, the Cloudflare/`__cf_chl` markers in
`detect_auth_wall`), and (b) my training knowledge of these web apps as of early
2026. **Every endpoint path and token name below is a hypothesis that must be
verified empirically** (open DevTools → Network on a live logged-in session and
read the actual request). I flag confidence explicitly. These are *private,
unversioned* endpoints; they change without notice, so even a verified shape has
a short shelf life.

For each provider: the send/stream endpoint, the auth material, the anti-automation
defenses, and a verdict.

---

### 2.1 Claude.ai (claude.ai)

**Endpoints (medium-high confidence on shape, exact paths need verifying):**
- List/create conversation: `POST /api/organizations/{org_uuid}/chat_conversations`
- Send + stream a completion:
  `POST /api/organizations/{org_uuid}/chat_conversations/{conv_uuid}/completion`
  → **SSE stream** (`text/event-stream`), `data:` frames carrying
  `completion` deltas and a terminal event. This is the cleanest of the four:
  a well-formed SSE completion stream maps directly onto Kim's `complete()`
  (accumulate deltas, return the final text — no sentinel needed).
- The `{org_uuid}` is discoverable from `GET /api/organizations` (or
  `/api/bootstrap`) once authenticated.

**Auth material:**
- Session cookie `sessionKey` (an `httpOnly` cookie set at login on `claude.ai`).
- The organization UUID (fetched once, then cached).
- Standard headers: `anthropic-client-*` / `anthropic-client-version`-style
  headers the web app sends, plus `Referer`/`Origin` = `https://claude.ai`.

**How Kim could obtain the credential WITHOUT a visible window:**
- Extract `sessionKey` from the persisted Chrome profile Kim already maintains
  (`sessions/chrome_data`) — Kim *already* keeps a logged-in profile there for
  the headless path. Cookies live in that profile's `Cookies` SQLite DB (though
  on macOS/Windows they may be encrypted with an OS-keychain key — extraction is
  non-trivial and is exactly the sort of thing CLAUDE.md's secret-file sandbox is
  designed to keep Kim *away* from).
- Cleaner: a **one-time headless CDP session** reads `document.cookie` /
  `context.cookies()` via Playwright, persists `sessionKey` into a Kim-owned
  cookie jar, and refreshes it opportunistically. This is the realistic path and
  it means "no *visible* window," not "no browser engine ever."

**Anti-automation defenses:**
- **Cloudflare** fronts claude.ai. Normal API/XHR calls from the logged-in origin
  usually carry a `cf_clearance` cookie already minted by the browser; a raw
  client that has a valid `cf_clearance` + `sessionKey` can often pass. But
  `cf_clearance` is **bound to the client's IP + User-Agent + (increasingly)
  TLS/JA3 fingerprint** — replaying it from Python `httpx` whose JA3 differs from
  Chrome's can trip a re-challenge (`__cf_chl…`, which Kim's `detect_auth_wall`
  already recognizes as a wall).
- No known Arkose/proof-of-work on the *completion* call itself (unlike ChatGPT).
- Server-side abuse heuristics on cadence/volume still apply.

**Verdict: VIABLE-BUT-FRAGILE.** Claude.ai is the *best* candidate for a
cookie-jar HTTP transport: clean SSE, a single session cookie, no per-message
proof-of-work. The fragility is Cloudflare fingerprinting — mitigable by
matching Chrome's User-Agent and, if needed, using a TLS-fingerprint-spoofing
client (`curl_cffi` / a JA3-matching wrapper) and reusing the browser-minted
`cf_clearance`. Worth a spike; **not** worth betting the whole feature on.

---

### 2.2 ChatGPT (chatgpt.com)

**Endpoints (high confidence on names, from the well-documented web app):**
- Send + stream: `POST /backend-api/conversation` → **SSE** (`data:` frames with
  `message` deltas, terminal `data: [DONE]`).
- Pre-flight: `POST /backend-api/sentinel/chat-requirements` (formerly
  `/backend-api/conversation/requirements`) returns a **`requirements` token**
  plus a **proof-of-work seed** that the *next* `POST /conversation` must echo in
  the `Openai-Sentinel-Chat-Requirements-Token` header (and a computed PoW
  answer). Session bootstrap: `GET /api/auth/session` yields the `accessToken`.

**Auth material:**
- `__Secure-next-auth.session-token` cookie (login session), **plus** a
  short-lived **Bearer `accessToken`** fetched from `/api/auth/session`.
- Cloudflare `cf_clearance` cookie.
- The `requirements` token + solved **proof-of-work** (Sentinel), and,
  historically/again under load, an **Arkose Labs token** (`arkose.func` / the
  `openai-…` enforcement) for the conversation call.

**Anti-automation defenses — the heaviest of the four:**
- **Sentinel proof-of-work.** The `chat-requirements` response hands the client a
  seed; the browser runs JS to compute a hashcash-style PoW and returns it. This
  is *designed* to require executing OpenAI's obfuscated in-page JavaScript.
  Re-implementing it in Python is possible but it is a **moving target that
  OpenAI rotates deliberately** — a permanent arms race.
- **Arkose / FunCaptcha** can be required for the conversation endpoint,
  especially for automation-looking traffic.
- **Cloudflare** + UA/JA3 fingerprinting on top.

**Obtaining creds without a visible window:** the cookie + accessToken are
extractable from a headless session, but the **PoW/Arkose tokens are minted
per-request by page JS** — a cookie jar cannot pre-compute them. You would have
to run the page's JS (i.e., a browser engine) *per message*, which defeats the
entire point of "no browser."

**Verdict: NOT-VIABLE without a browser engine.** ChatGPT is explicitly hardened
against exactly this (Sentinel PoW + Arkose exist to stop cookie-replay bots).
A pure-HTTP transport would be a perpetual reverse-engineering treadmill and
would break on OpenAI's schedule, not Kim's. Do not build it. (This is *the*
provider where headless-browser is the only sane answer.)

---

### 2.3 Gemini (gemini.google.com)

**Endpoints (medium confidence — Google's RPC is deliberately opaque):**
- Gemini web does **not** use a clean REST/SSE API. It uses Google's
  **`batchexecute`** RPC transport:
  `POST /_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate`
  (RPC id historically `assistant.lamda.BardFrontendService/StreamGenerate`),
  with a **`f.req`** form-encoded envelope and a chunked, length-prefixed
  **JSON-array** response (not standard SSE — a Google-proprietary framing you
  must hand-parse). Kim's `site_configs` already targets `gemini.google.com` and
  the code already threads a Gemini **`authuser`** index for multi-account.

**Auth material:**
- Google SID/HSID/SSID/`__Secure-1PSID` + `__Secure-1PSIDTS` cookies (the
  `1PSIDTS` one **rotates frequently** and must be refreshed).
- A **per-page `at` token** (a.k.a. `SNlM0e`) scraped from the initial HTML of
  `gemini.google.com` — **required in the `f.req` body of every call**. This is
  the killer: the `at` token is *not a cookie*; it is embedded in the page and
  must be re-scraped when it expires. `bl` (backend build label) and `reqid`
  parameters are also page-derived.
- `authuser` / `X-Goog-*` headers.

**Anti-automation defenses:**
- The **`at`/`SNlM0e` "magic cookie" pattern is itself the anti-automation
  measure** — you cannot call `StreamGenerate` with cookies alone; you must first
  GET the page and extract the token, which means you are already halfway to
  needing a browser. Community libraries (e.g. the various `Bard`/`gemini` API
  reverse-engineering projects) do exactly this with a cookie + a page-scrape,
  and they **break repeatedly** as Google rotates `1PSIDTS` and the RPC ids.
- Google account-security heuristics may challenge non-browser access patterns
  and can flag the account.

**Obtaining creds without a visible window:** the cookies + a one-time page GET
to extract `at` are doable headlessly, but the `1PSIDTS` rotation + `at`
expiry mean you are effectively re-scraping the page routinely — again, a browser
engine in the loop.

**Verdict: NOT-VIABLE via pure HTTP (VIABLE-BUT-VERY-FRAGILE via cookie +
page-scrape libraries).** The `batchexecute`/`f.req` framing and the `SNlM0e`
page token make this a proprietary-RPC reverse-engineering project with a
notoriously short half-life. The existing browser path is more robust. Do not
build a pure-HTTP Gemini transport.

---

### 2.4 Grok (grok.com / x.com)

**Endpoints (lower confidence — least publicly documented, and it moved from
x.com to grok.com):**
- `grok.com` exposes REST-ish endpoints such as
  `POST /rest/app-chat/conversations/new` and `.../conversations/{id}/responses`
  returning a **streamed JSON-lines** body. The x.com-hosted variant used
  `/i/api/graphql/…/Grok…` GraphQL operations.

**Auth material:**
- x.com/grok.com auth cookies (`auth_token`, `ct0` CSRF), and — on the x.com
  GraphQL path — a static **Bearer** plus the `x-csrf-token` (= `ct0` cookie)
  and a **`x-client-transaction-id`** header.

**Anti-automation defenses:**
- **Cloudflare** on grok.com.
- The x.com path requires the **`x-client-transaction-id`** header, which is
  computed by obfuscated client JS (the same mechanism that makes the Twitter/X
  private API painful to automate) plus a **guest-token / CSRF** dance.
- Aggressive account-level rate limiting and bot heuristics (X is hostile to
  scraping by policy and by engineering).

**Verdict: NOT-VIABLE-without-a-browser-engine (VIABLE-BUT-FRAGILE at best).**
The client-transaction-id header alone requires running X's JS; grok.com's
Cloudflare + undocumented, actively-changing endpoints make this the least
stable target after ChatGPT. Do not build a pure-HTTP Grok transport.

---

### 2.5 Per-provider verdict summary

| Provider | Send/stream endpoint (hypothesis) | Auth material | Hard blocker | Verdict |
|---|---|---|---|---|
| **Claude.ai** | `POST …/chat_conversations/{uuid}/completion` (SSE) | `sessionKey` cookie + org UUID | Cloudflare JA3/UA fingerprint on `cf_clearance` | **VIABLE-BUT-FRAGILE** — best candidate |
| **ChatGPT** | `POST /backend-api/conversation` (SSE) | session cookie + Bearer accessToken | **Sentinel proof-of-work + Arkose** (page-JS-minted per request) | **NOT-VIABLE without a browser engine** |
| **Gemini** | `POST …/StreamGenerate` (`batchexecute`/`f.req`) | 1PSID(+TS) cookies + **`SNlM0e`/`at` page token** | page-minted `at` token + `1PSIDTS` rotation + proprietary RPC framing | **NOT-VIABLE via pure HTTP** (fragile cookie+scrape only) |
| **Grok** | `POST /rest/app-chat/conversations/…` (JSON-lines) or x.com GraphQL | x.com cookies + `ct0` CSRF + Bearer | **`x-client-transaction-id`** (page-JS-minted) + Cloudflare | **NOT-VIABLE-without-a-browser-engine** |

**The pattern:** three of four services mint a per-request token *in the page's
JavaScript* (ChatGPT PoW/Arkose, Gemini `SNlM0e`, Grok transaction-id). A cookie
jar cannot manufacture those tokens; only a JS runtime (a browser engine) can.
**Only Claude.ai** authorizes the completion call with a plain session cookie,
and even it is gated by Cloudflare fingerprinting.
