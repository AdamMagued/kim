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

## F-D-2: `/v1/browser/restore` navigates the webview to a URL read from an on-disk session-meta file — second SSRF/redirect entry point
- **File:** desktop/src-tauri/src/http_bridge/session_meta.rs (restore) → provider_url.rs:89 `browser_url_allowed_for_restore`
- **Severity:** Medium (pending confirm of the restore allowlist strictness)
- **Class:** security
- **Evidence:** restore path pulls a previously-committed URL from session metadata (a JSON
  file under the sessions dir, writable by any user-level process) and re-navigates the
  webview. `browser_url_allowed_for_restore` is described as "stricter than same-host"; needs
  a concrete check that it cannot be coerced to a private-IP/metadata host. If the allowlist
  is host-prefix based it may be bypassable via `https://gemini.google.com.evil.example`.
- **Fix sketch:** confirm restore uses exact-origin equality against the provider allowlist,
  not `starts_with`/substring; add the same private-range block as F-D-1.
- **Cross-territory?** no (Team D owns provider_url.rs + session_meta.rs).

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
