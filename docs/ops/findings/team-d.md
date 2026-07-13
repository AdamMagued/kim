# Team D — Wave 1 findings (Desktop Rust Backend)

Territory: `desktop/src-tauri/src/` — subprocess/spawn lifecycle, HTTP bridge, Tauri
command surface, google_oauth/provider_auth, tauri.conf.json + capabilities, build.rs.

Baseline: `integration/audit-fixes`. Read-only hunt. Most severe first.

General note: this layer is heavily audited already (M-PROC-*, L-BRIDGE-*, #24/#25 pid
guards, M-BRIDGE-3 body cap, constant-time token compare). The kills below are the
residue after those passes.

---

## F-D-1: `/v1/open` (and `open_browser_signin_window_inner`) navigate the app webview to ANY http/https URL — link-local/RFC-1918/cloud-metadata SSRF
- **File:** desktop/src-tauri/src/http_bridge/provider_send.rs:56 → browser_bridge.rs:60-64,80-82,106
- **Severity:** High
- **Class:** security
- **Evidence:** `open()` passes `parsed.url` (an arbitrary JSON string from the request
  body) straight to `open_browser_signin_window_impl`. The only validation is the scheme
  gate `matches!(parsed.scheme(), "https" | "http")`. No host allowlist, no private-range
  block. A local process holding the bridge token (the token file is `~/.kim/bridge_token`,
  0600 but readable by any process running as the user — the same threat model as Team C's
  F-C-4) can `POST /v1/open {"url":"http://169.254.169.254/latest/meta-data/iam/security-credentials/"}`
  and the app's own webview navigates there, then runs `PERSISTENT_BRIDGE_JS` and the
  title-pull payload channel over the fetched content. This is the top-level-navigation
  sibling of F-C-4 (which covered subresource/XHR/fetch). Unlike the normal provider flow
  (`/v1/complete`, `/v1/provider`) which resolves through the `provider_url.rs` allowlist,
  `/v1/open` bypasses it entirely.
- **Fix sketch:** before navigating, resolve the host and reject link-local (169.254/16,
  fe80::/10), loopback, and RFC-1918/ULA ranges (and `metadata.google.internal`); or restrict
  `/v1/open` to the same provider allowlist the rest of the bridge uses.
- **Cross-territory?** partial — the webview network-policy fix lives in Team D (browser_bridge.rs),
  but it shares root cause with Team C's F-C-4. Coordinate one guard covering both nav + subresource.

## F-D-2: `KIM_PROJECT_ROOT` env override is documented as "wins" but is silently overridden by the compile-time baked root and `~/.kim_root`
- **File:** desktop/src-tauri/src/paths.rs:24-57 (`default_project_root`)
- **Severity:** Medium
- **Class:** bug | contract
- **Evidence:** resolution order is 0a `KIM_COMPILE_TIME_ROOT` (baked by build.rs) → 0b
  `~/.kim_root` → 1 `KIM_PROJECT_ROOT` env → 2 exe walk → 3 `~/.kim`. Step 1's own comment says
  "Environment override wins (explicit user intent)", but it does NOT win: if the baked dev-tree
  path still exists on the machine (0a checks only `p.exists() && orchestrator/agent.py exists`)
  or `~/.kim_root` resolves, `KIM_PROJECT_ROOT` is never consulted. Concrete failure: a developer
  who built the `.app` then sets `KIM_PROJECT_ROOT` to point at a second checkout still gets the
  original baked tree — the orchestrator, config.yaml, and sessions all load from the wrong root
  with no error. Same hazard if a distributed build's baked path happens to exist on a target
  machine. The baked root taking precedence over an explicit env override is a genuine
  least-surprise violation, not just a stale comment.
- **Fix sketch:** move the `KIM_PROJECT_ROOT` check above 0a/0b (explicit env should win), or
  fix the comment and document the true precedence. Verifying `agent.py` under the env root
  before accepting it keeps it safe.
- **Cross-territory?** no.
- **Note (checked, NOT a finding):** `/v1/browser/restore` (session_meta.rs:321-337) was audited
  as a possible second SSRF vector but is safe — it gates the stored URL through
  `browser_url_allowed_for_restore` → `browser_url_is_bad_for_commit`, which requires exact
  equality to a hardcoded provider origin (provider_url.rs:76-86); a spoofed
  `gemini.google.com.evil` host fails the exact match and falls back to `fresh_site_url`.

## F-D-3: `/v1/health` is unauthenticated and confirms the bridge port + liveness to any local process
- **File:** desktop/src-tauri/src/http_bridge/mod.rs:74,103-105
- **Severity:** Low
- **Class:** security
- **Evidence:** the token gate explicitly exempts `GET /v1/health`, which returns
  `{"ok":true}`. Any local process can sweep 18991-19010 and fingerprint that Kim's bridge is
  running (and on which port) before attempting token-gated calls. Low impact (no data), but
  it is free reconnaissance and the stated reason ("Railway prober, uptime checks") does not
  apply to a 127.0.0.1-only server.
- **Fix sketch:** either drop the health route or keep it but document that it is intentional
  local-only liveness; consider binding the probe behind the token too.
- **Cross-territory?** no.

## F-D-4: the loopback bridge token is injected into third-party provider webviews — a compromised/monkeypatched provider page can steal it and drive the local bridge
- **File:** desktop/src-tauri/src/provider_auth.rs:61-89 (`build_auth_probe_js`); browser_bridge.rs (PERSISTENT_BRIDGE_JS injection); http_bridge/mod.rs token gate
- **Severity:** Medium
- **Class:** security
- **Evidence:** auth-probe/send bridge JS embeds the live bridge token (`const token = {token_js}`)
  and injects it into the provider page (claude.ai, chatgpt.com, gemini.google.com, grok.com,
  chat.deepseek.com — see capabilities/browser-bridge.json). The token is closure-scoped, but the
  injected code calls the page's global `window.fetch`; a provider page that has monkeypatched
  `fetch` (or any main-world script running before injection) captures the token and the
  `baseUrl`. With the token, that script can then call every authenticated bridge route from
  inside the webview — most dangerously `/v1/task` (spawn an arbitrary agent run on the user's
  machine) and `/v1/open` (F-D-1 SSRF). This turns any provider-page compromise/XSS, or a user
  being navigated onto a lookalike page in the webview, into local bridge takeover. The bridge
  auth model implicitly trusts every loaded provider origin with a machine-local capability token.
- **Fix sketch:** run bridge JS in an isolated content world (WKWebView `WKContentWorld`/Tauri
  isolation) so page scripts can't observe injected globals or hook the `fetch` it uses; and/or
  scope tokens per-capability (a read-only auth-probe token that cannot reach `/v1/task`/`/v1/open`).
- **Cross-territory?** partial — token minting/routing is Team D; the isolation-world plumbing may
  touch the webview init that Team C/frontend also handles.

## F-D-5: agent stdout forwarder has no per-line size cap or event back-pressure — a runaway/oversized-line child can balloon memory
- **File:** desktop/src-tauri/src/spawn_supervisor.rs:124-132 (stdout pump) → subprocess.rs:27-42 (`forward_agent_stdout_line`, `app.emit`)
- **Severity:** Low
- **Class:** leak
- **Evidence:** the pump uses `BufReader::lines().next_line()`, which accumulates an entire line
  (no newline ⇒ unbounded String) before yielding, and then `app.emit`s each line to the webview
  with no size cap or rate limit. A child that emits a single multi-GB line, or floods stdout
  faster than the webview consumes events, grows Rust-side memory unboundedly (the request-body
  path already has M-BRIDGE-3's 32 MB cap; the stdout path has no equivalent). Trusted-input today
  (our own orchestrator), so robustness rather than security — but a malformed provider dump or a
  buggy tool can trigger it.
- **Fix sketch:** cap `next_line` accumulation (read fixed-size chunks, truncate over-long lines
  with a marker) and/or coalesce/drop emits under back-pressure.
- **Cross-territory?** no.
