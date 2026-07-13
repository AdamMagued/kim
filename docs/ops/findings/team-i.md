# Team I — Wave 1 findings (Security & Trust, cross-cutting)

Territory: whole-repo security sweep + authoritative threat model.
Baseline: `integration/audit-fixes`. Read-only hunt. Format mirrors §3. Most severe first.

NOTE: Team C (F-C-1..7), Team D (F-D-1..5), Team G (F-G-4, F-G-6) already filed the
git/awk/tar/gh allowlist escapes, the bridge SSRF + token-injection, the dead
`blocked_commands` config, and the Pillow CVEs. Those are CONSOLIDATED into
`docs/THREAT_MODEL.md`, not re-filed here. Below are NEW findings Team I found on top.

---

## F-I-1: CLI installers verify only a same-origin SHA-256 sidecar and NEVER the published cosign signature — supply-chain authenticity gap (curl | sh)
- **File:** scripts/install-kim.sh:48-88; scripts/install-kim.ps1:62-86; .github/workflows/release.yml (cosign sign-blob step produces `.sig`/`.pem` that nothing consumes)
- **Severity:** Medium (High if release/CDN is ever compromised)
- **Class:** security / supply-chain
- **Evidence:** The `curl … | sh` install path downloads `kim-<triple>.tar.gz` and its
  `kim-<triple>.sha256` sidecar **from the same GitHub release URL** and compares hashes.
  Because the checksum is served from the exact same origin as the binary, it only proves
  transport integrity — an attacker who can alter the release asset (compromised repo
  token, malicious maintainer, CDN/MITM on a non-pinned mirror) simply replaces the
  sidecar too and the check passes. The release DOES publish real authenticity material —
  Sigstore keyless `cosign sign-blob` output (`<binary>.sig` + `<binary>.pem`, release.yml
  "Sign KimCLI binary" step) — but **neither installer runs `cosign verify-blob`**. The
  README even tells the user the manual verify command, so the signatures exist but the
  advertised one-liner install silently skips them. Net: the strongest signal (keyless
  transparency-log signature tied to the repo's OIDC identity) is published and then
  ignored by the actual install flow.
- **Fix sketch:** In install-kim.sh/.ps1, when `cosign` is available, download `.sig`+`.pem`
  and run `cosign verify-blob --certificate-identity-regexp <repo> --certificate-oidc-issuer
  https://token.actions.githubusercontent.com --signature …` before install; fail closed on
  mismatch, warn (not silently pass) when cosign is absent. Keep the sha256 as a fast
  integrity pre-check only.
- **Cross-territory?** yes — Team G owns scripts/ + release.yml.

## F-I-2: `KIM_CODEX_BYPASS_SANDBOX=1` runs codex-exec with `--dangerously-bypass-approvals-and-sandbox` on a model/browser-derived task string — prompt-injection → unsandboxed RCE, no per-command HITL
- **File:** orchestrator/codex_bridge_service.py:788-800; desktop/src-tauri/src/task_spec.rs:357,567; cli/src/provider/codex_stream.rs:459-461
- **Severity:** High (by design; the danger is the trust path feeding it, not the flag gating)
- **Class:** security / injection
- **Evidence:** All three spawn paths correctly gate the bypass flag behind explicit opt-in
  (`KIM_CODEX_BYPASS_SANDBOX=1` / `p.bypass_sandbox`). That gating is sound. The residual
  risk is architectural and undocumented: once opted in, `codex exec` runs with **no OS
  sandbox and no per-tool approval** in `args.cwd`, and the `task`/`prompt` string handed
  to it is agent-generated — in Kim's browser-provider mode it can be derived from scraped
  LLM output or (worse) from page content read by `web_text`/`web_open`. Codex's own
  approval layer — the ONLY thing standing between a tool call and the shell in this mode —
  is explicitly disabled. So a prompt-injection payload that reaches the codex task string
  becomes arbitrary local code execution with zero human in the loop, bypassing every
  `mcp_server/policy.py` gate (those guard `run_command`, not the codex child). This is the
  single largest blast-radius path in the app and is not called out in any THREAT/SECURITY
  doc as "only enable in a disposable/containerized workspace."
- **Fix sketch:** (1) Document loudly that bypass mode = full RCE and must only run in a
  throwaway sandbox; surface a one-time in-app confirmation, not just an env var. (2) Refuse
  to combine bypass mode with browser-provider tasks whose task string originated from
  scraped web/LLM content. (3) Consider requiring the workspace to be an ephemeral temp root
  (see F-I-4) when bypass is on.
- **Cross-territory?** yes — Team A (orchestrator), Team D (task_spec.rs), CLI owner.

## F-I-3: Session JSONL transcripts (and their dirs) are written world-readable and un-scrubbed — full conversation + any secret-bearing tool output is exposed to other local users
- **File:** orchestrator/session_store.py:101,136-137 (`session_dir.mkdir(parents=True)` no mode; `open(self.session_file, "a")` no chmod); contrast mcp_server/logger.py:133-137 (deliberate `0o600`), desktop/src-tauri/src/http_bridge/mod.rs:171 (bridge token `0o600`), cli/src/config.rs:116 (API-key config `0o600`)
- **Severity:** Medium
- **Class:** security / info-disclosure
- **Evidence:** Every message (user prompts, model replies, tool inputs, tool RESULTS)
  is appended verbatim to `kim_sessions/<date>/<id>.jsonl`. The directory is created with
  `mkdir(parents=True)` (mode `0o777 & ~umask` → typically `0o755`) and the file opened with
  a bare `open(..., "a")` (→ typically `0o644`). No `os.chmod`, and — unlike `mcp_server.logger`,
  which routes every line through a secret scrubber that redacts `Authorization`, `sk-…`,
  `AKIA…`, and PEM blocks (verified in tests/test_logger.py) — **session_store performs NO
  scrubbing**. So on any multi-user / shared machine (or any process running as a *different*
  local user with world-read), another account can read the complete transcript. If the user
  pastes a credential, or a tool result echoes one (a config file read by `read_file`, a
  `git remote -v` with an embedded token, etc.), it lands in this world-readable file in clear
  text. The project deliberately hardened the log, the bridge token, and the CLI key store to
  `0o600`/`0o700` but left the richest data sink — the transcript — at the process umask.
  (Note: the MCP `run_command` sandbox env DOES strip provider API keys, so a plain `env` dump
  won't leak *provider* keys; this finding is about the many other secret paths + the perm gap.)
- **Fix sketch:** `os.chmod(self.session_file, 0o600)` on create and create `kim_sessions/`
  (and date dirs) with mode `0o700`; apply the same on `summary_file`/`context_file` atomic
  writes (session_store.py:362-363,385-389). Optionally run tool-result text through the
  existing logger scrubber before persisting. Mirror on Windows via an ACL or accept POSIX-only.
- **Cross-territory?** yes — Team A owns session_store.py.

## F-I-4: The browser provider drives a Chrome instance over an unauthenticated CDP port (9222) holding live provider logins — any local process can read cookies / puppeteer the authenticated sessions
- **File:** orchestrator/providers/browser/site_configs.py:15-19 (`CDP_URL = http://localhost:9222`); orchestrator/providers/browser/provider.py:43-50,184-186 (`--remote-debugging-port=9222 --user-data-dir=<project>/sessions/chrome_data`)
- **Severity:** Medium
- **Class:** security / local-attack-surface
- **Evidence:** Kim's key-free "browser mode" connects Playwright to a user-launched Chrome
  started with `--remote-debugging-port=9222`. Chrome's DevTools Protocol endpoint has **no
  authentication** — it binds to localhost, but every process running as the user (and, if the
  port is ever bound to `0.0.0.0` or forwarded, remote hosts) can `GET http://localhost:9222/json`,
  attach to the browser, exfiltrate cookies/localStorage for claude.ai / chatgpt.com /
  gemini.google.com / grok.com / deepseek, and issue authenticated requests as the user. The
  logged-in profile persists at `<project>/sessions/chrome_data` for cookie reuse across runs,
  so the credential material is long-lived on disk. This is a documented design trade-off of
  CDP mode but is nowhere flagged as a security boundary; it materially widens the local attack
  surface (a single malicious local script = full provider-account takeover, no password needed).
  Complements Team C F-C-4 / Team D F-D-1 (SSRF) and F-D-4 (bridge-token theft): the browser is a
  high-value, low-auth local target from three independent angles.
- **Fix sketch:** Prefer binding the debugging port to a random loopback port + `--remote-debugging-address=127.0.0.1`
  and, where supported, a per-launch token; document CDP mode as "any local process can drive
  your logged-in browser" in SECURITY. Ensure `sessions/chrome_data` is `0o700` and gitignored
  (verify). At minimum, warn the user when 9222 is reachable by processes outside Kim.
- **Cross-territory?** partial — Team B owns the browser provider; hardening the port launch may
  touch desktop launch code (Team D).

## Clean / strength notes (verified, NOT findings)
- **Secret logging is CLEAN.** `mcp_server/logger.py` scrubs `Authorization`/`sk-`/`AKIA`/PEM
  from every log line (tests/test_logger.py). Full-repo `logger.*(… token|api_key|secret …)`
  grep found no plaintext-secret log sites in orchestrator/mcp_server/cli/desktop.
- **No committed secrets.** `git grep` for `sk-…`/`AKIA…`/`ghp_…`/PEM across the sampled
  history → only `tests/test_logger.py` fixtures. No tracked `.env`/`.pem`/`id_rsa`/`credentials`.
  gitleaks/trufflehog were NOT available in this environment — a CI gitleaks gate is still advised.
- **CLI API-key store + bridge token are `0o600`; codex temp dir `0o700`.** Perm hygiene is
  correct everywhere EXCEPT the session transcript (F-I-3).
- **Dependencies:** `pip-audit -r requirements.txt` → 7 vulns in Pillow(5)+pytest(1), i.e.
  exactly Team G F-G-6, no additional packages. `cargo audit` / `npm audit` were not run here
  (cargo-audit not installed); recommend wiring all three into CI (see hardening list).

