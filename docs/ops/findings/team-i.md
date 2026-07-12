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

## F-I-3: (placeholder — filesystem/permissions sweep, see below; will be filled after temp-home + session-perm audit)
