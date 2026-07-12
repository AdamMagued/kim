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
