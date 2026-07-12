# Kim — Threat Model & Security Posture

**Status:** Authored by Operation Google-Level, Wave 1, Team I (Security & Trust).
**Baseline:** `integration/audit-fixes` (== `origin/main`).
**Scope:** whole repo — Python orchestrator, MCP server + 50 OS-control tools, Tauri/Rust
desktop shell, KimCLI, browser provider, codex bridge, CI/release.
**Companion finding docs:** `docs/ops/findings/` — Team C (MCP tool gates), Team D (Rust
bridge), Team G (satellites/deps), Team I (this doc's findings F-I-1..4).

This document is descriptive of the system **as built**, not as aspirationally documented.
Where the two differ, the code wins and the gap is called out.

---

## 0. TL;DR for a reviewer

Kim is, by design, **a local agent that controls the user's computer** — browser, shell,
files, apps — under LLM direction. The security question is therefore not "can it run
code" (that is the product) but **"what bounds the code it runs, and who/what can steer
it."** Two structural weaknesses dominate:

1. **The shell allowlist admits general-purpose command-runners** (`git -c`, `awk`, `tar`,
   `gh`) while the human-approval risk arm is **off by default** → un-approved arbitrary
   RCE and secret disclosure in the shipped config (Team C **F-C-1/2/3**, the headline
   risks).
2. **The steering input is partly untrusted.** In browser-provider mode the "LLM" is a
   scraped web page; `web_text`/`web_open` read attacker-controllable content; and in
   codex bypass mode the shell runs **unsandboxed with no approvals** (**F-I-2**). Prompt
   injection is therefore a first-class RCE vector, not a curiosity.

Everything else (path sandbox, log scrubbing, token/file perms, process-group reaping) is
largely sound and was hardened by prior campaigns. The residue is enumerated below.

---

## 1. Trust boundaries — what we trust vs what we do NOT

| Entity | Trusted? | Why / caveat |
|---|---|---|
| **The local user** | Trusted | Kim acts as them, with their privileges. Blast radius of any bug is "the user's account." |
| **The orchestrator process** | Trusted | Holds provider API keys in memory; is the policy enforcer. |
| **`mcp_server.policy.enforce`** | Trust as chokepoint | Runs before EVERY tool dispatch, fails **closed**. Sound as a funnel; the *rules inside it* are incomplete (F-C-1/2/3). |
| **The LLM / model output** | **NOT trusted** | Tool-call arguments are model-authored. In API mode the model is a reputable provider; in **browser-provider mode the "model" is a scraped web page** and can be steered by page content. |
| **Web content read by tools** (`web_text`, `web_open`, page DOM) | **NOT trusted** | Classic prompt-injection surface. A malicious page can instruct the agent to call tools (SSRF, exfil, RCE). |
| **The codex/claw child process** | Conditionally trusted | Normally has its own approval layer. In **bypass mode it has NONE** (F-I-2). |
| **Provider webviews** (claude.ai, chatgpt.com, gemini, grok, deepseek) | **NOT fully trusted** | The loopback bridge token is injected into them (F-D-4); a compromised/lookalike page can steal it and drive `/v1/task` + `/v1/open`. |
| **Other local processes running as the user** | **NOT trusted (but implicitly trusted today)** | The bridge (127.0.0.1), CDP 9222, and world-readable session files all assume same-user = same-trust. False on shared machines and against any local malware (F-I-3, F-I-4, F-D-3/4). |
| **The filesystem outside `ALLOWED_PATHS`** | Guarded | `validate_path` resolves symlinks/`..` then checks containment — genuinely closed for *declared path args*; bypassed by binaries that reach files without a path arg (F-C-1/2/3). |
| **The release pipeline / distribution channel** | Partially trusted | CLI is cosign-signed + macOS notarized, but the install script verifies only a **same-origin sha256**, never the signature (F-I-1). |
| **Third-party dependencies** | Pinned, partly stale | Pillow ships 5 known CVEs, pin caps below the fix (F-G-6). |

**The load-bearing assumption that does NOT hold:** *"an allowlisted binary only does its
nominal job."* `git`, `awk`, `tar`, `sed`, `make`, `gh` are allowlisted yet are themselves
command-runners / secret-readers. A positive allowlist of program *names* cannot bound
*behaviour* when the allowed programs can exec other programs. (Team C, F-C-1/2/3.)

---

## 2. The enforcement stack (as built)

Every tool call flows through `server.py:call_tool` → `policy.enforce(name, args)` →
dispatch. `enforce` never raises (internal error ⇒ deny). Three decisions: **deny**,
**approve** (blocks on human; default-deny on timeout), **allow**.

| Layer | Mechanism | Strength | Gap |
|---|---|---|---|
| **S3 path sandbox** | `validate_path` on declared path args — resolves symlinks + `..`, checks `ALLOWED_PATHS`, denies secret-file globs & sensitive dirs case-insensitively | **Strong** for path args | Blind to files reached without a path arg (git alias, awk `getline`, gh's own store) — F-C-1/2/3 |
| **S2 shell allowlist** | `run_command` segmented on operators, recurses into `sh -c`, denies `_DENY_COMMANDS` on realpath, denies sensitive path *tokens*, escalates inline-interpreter exec/sudo/untrusted-location | **Weakest real boundary** | Admits general-purpose command-runners; token-shape scan can't see payloads inside argument grammar — F-C-1/2/3 |
| **L3 mandatory approval** | `run_python`/`run_node`/`run_powershell` escalate to human **unconditionally**, payload-scoped cache | **Strong** — the one place the design admits it can't parse and asks a human | Only covers these 3 tools. The SAME "runs arbitrary code" power hidden inside an allowlisted binary via `run_command` gets NONE of it — F-C-1/2/3 |
| **Risk-threshold arm** | `hitl_risk_threshold` gates high-risk calls | **INERT by default** (unset) | A `run_command` rated `risk=high` with no attached escalation **dispatches with no human** in the shipped config |
| **Code-exec sandbox** | minimal env (strips API keys), optional `sandbox-exec`/`bwrap` (network-denied) | Good env isolation | OS sandbox is **fail-open if the binary is absent**; blocklist not applied to `file=` execution (DiD only, mitigated by L3 human) |
| **Browser SSRF guard** | `_is_ssrf_target` on top-level navs; `file:`/`chrome:` refused | Covers navigation | **Not** on subresource/XHR/fetch (F-C-4); `/v1/open` bypasses the provider allowlist entirely (F-D-1) |
| **Log scrubber** | `logger.py` redacts Authorization/`sk-`/`AKIA`/PEM | **Strong** (tested) | Not applied to session transcripts (F-I-3) |

**Net:** denies (path deny-list, binary deny-list, redirect/find rules) and the
unconditional code-exec/powershell approvals are strong. The soft underbelly is the
**positive shell allowlist + default-off risk arm**, and the **untrusted steering inputs**.

---

## 3. Per-tool blast radius

| Tool / surface | Can do | Worst case today | Gated by |
|---|---|---|---|
| `run_command` | Run allowlisted binaries | **Arbitrary RCE + any-file read** via `git -c`/`awk`/`tar`; token exfil via `gh auth token` — on `allow`, no human (F-C-1/2/3) | S2 allowlist (incomplete) |
| `run_python` / `run_node` | Arbitrary code | Full RCE — but **always human-approved** (payload-scoped) | L3 unconditional approval |
| `run_powershell` | Arbitrary code | Full RCE — always approved | L3 |
| `read_file` / file tools | Read/write within `ALLOWED_PATHS` | Bounded; secret globs denied at any depth | S3 path sandbox (strong) |
| `web_open` / `web_text` | Fetch + read pages | **Prompt-injection intake** + SSRF to cloud-metadata/RFC-1918 via page JS (F-C-4); no approval by default | SSRF guard (nav-only) |
| Codex bridge (normal) | Delegate a coding task to codex | Bounded by codex's own approvals | codex approval layer |
| **Codex bridge (bypass)** | Delegate with `--dangerously-bypass-approvals-and-sandbox` | **Unsandboxed RCE, no per-command HITL**, task string may derive from web/LLM content (F-I-2) | Only the `KIM_CODEX_BYPASS_SANDBOX=1` opt-in |
| HTTP bridge `/v1/task` | Spawn an agent run | Local RCE-equivalent if token stolen (F-D-4) | 127.0.0.1 + token (constant-time compare) |
| HTTP bridge `/v1/open` | Navigate app webview to any URL | SSRF incl. cloud-metadata (F-D-1) | token; NO host allowlist |
| HTTP bridge `/v1/health` | Liveness | Recon: fingerprints bridge port (F-D-3) | **unauthenticated** |
| CDP :9222 | Drive the logged-in Chrome | **Provider-account takeover** — any local process reads cookies / acts as user (F-I-4) | none (localhost only) |
| Session transcript | Persist conversation | Info-disclosure — world-readable, un-scrubbed (F-I-3) | file perms (umask) |

---

## 4. The codex-bridge unsandboxed-shell risk (deep dive — F-I-2)

The single largest blast-radius path in the app.

- Three spawn sites — `orchestrator/codex_bridge_service.py:788-800`,
  `desktop/src-tauri/src/task_spec.rs:357/567`, `cli/src/provider/codex_stream.rs:459` —
  append `--dangerously-bypass-approvals-and-sandbox` to `codex exec`.
- **Gating is correct:** the flag appears **only** when `KIM_CODEX_BYPASS_SANDBOX=1`
  (Python) / `p.bypass_sandbox` (Rust). This is a genuine opt-in, not an accident.
- **The residual danger is the trust path, not the gate.** Once enabled, `codex exec` runs
  in `args.cwd` with **no OS sandbox and no per-tool approval** — codex's own approval layer,
  the *only* thing between a model tool-call and the shell in this mode, is switched off.
  None of Kim's `policy.py` gates apply to the codex child (they guard `run_command`, a
  different process).
- **Who supplies the task string?** The agent. In browser-provider mode that can trace back
  to scraped LLM output or page content read by `web_text`/`web_open`. So a prompt-injection
  payload that reaches the codex task = **arbitrary local code execution, zero human in the
  loop.**
- **Why it matters even though it's opt-in:** nothing in SECURITY/README frames bypass mode
  as "full RCE — use only in a disposable/containerized workspace," there is no in-app
  confirmation (just an env var a power user copies from a forum), and it can be combined
  with browser-provider tasks whose text is attacker-influenced.

**Mitigations (see hardening list P0/P1):** one-time in-app confirmation; refuse bypass +
web-derived task strings; force an ephemeral temp workspace when bypass is on; loud docs.

---

## 5. Prompt-injection kill-chain (worst realistic path)

1. User runs Kim in **browser-provider mode** (key-free) and asks it to "research X."
2. Agent `web_open`s an attacker-controlled page; `web_text` ingests its content.
3. Page content contains injected instructions ("now run …" / "open 169.254.169.254 …").
4a. **SSRF branch:** page JS (or agent) hits `169.254.169.254` / RFC-1918 — subresource
    SSRF (F-C-4) or `/v1/open` (F-D-1) — exfiltrates cloud/admin creds via a normal request.
4b. **RCE branch (no approval prompt):** agent emits `run_command` with a `git -c
    alias.x=!<cmd>` / `awk 'BEGIN{system(...)}'` payload → **allow**, executes (F-C-1/2/3).
4c. **RCE branch (bypass mode):** if `KIM_CODEX_BYPASS_SANDBOX=1`, agent routes the task to
    codex-exec → unsandboxed shell, no approval (F-I-2).
5. **Token-theft amplifier:** a compromised provider webview steals the bridge token
    (F-D-4) and drives `/v1/task` to spawn further runs.

Each link is an existing finding. The chain is what the threat model exists to surface:
**untrusted-input intake + incomplete shell gate + default-off approval = injection→RCE.**

---

## 6. Consolidated top-5 security risks (across ALL teams)

| # | Risk | ID(s) | Sev | One-line |
|---|---|---|---|---|
| 1 | Allowlisted `git -c` = un-approved RCE + absolute-path secret read | **F-C-1** | Critical | Defeats S2 allowlist AND S3 sandbox in default config |
| 2 | Allowlisted `awk`/`tar`/`sed`/`make` exec arbitrary commands | **F-C-2** | Critical | Same escape, different root binaries |
| 3 | Codex bypass mode = unsandboxed shell, no approvals, web-steerable | **F-I-2** | High | Largest blast radius when opted in |
| 4 | Secret disclosure + SSRF cluster (gh token, IMDS via subresource/webview) | **F-C-3 · F-C-4 · F-D-1** | High | Token exfil + cloud-metadata reachable |
| 5 | `shell.blocked_commands` config is read by nothing — false security | **F-G-4** | High | Operators believe they blocked commands; they didn't |

**Runners-up:** F-D-4 (bridge-token theft from provider webview → `/v1/task` RCE),
F-G-6 (Pillow CVEs on the image path), F-I-4 (unauth CDP → provider-account takeover),
F-I-3 (world-readable transcripts), F-I-1 (install-time authenticity gap).

---

## 7. Prioritized hardening list

### P0 — do first (un-approved RCE / secret disclosure in default config)

- **H0.1 — Close the allowlist-escape family.** Add argv escalation/deny for exec-capable
  allowlisted binaries: `git -c`/`--config*`/dangerous config keys (`alias.*=!…`,
  `core.fsmonitor/pager/editor/sshCommand`, `gpg.program`, `credential.helper`, …); `awk`
  `system`/`|getline`; `tar --checkpoint-action`/`--to-command`/`--use-compress-program`;
  `sed` `e`/`s///e`; `find -fprintf`/`-exec`; `gh auth token`/`--show-token`. Route
  legitimate needs through the structured tools. **Refs: F-C-1, F-C-2, F-C-3.**
- **H0.2 — Turn on a default approval floor.** Make `hitl_risk_threshold` default to
  something non-inert (or require approval for any `run_command` rated `risk=high` that
  carries no other escalation), so the git/awk/tar residue can't dispatch silently.
  **Refs: F-C-1/2/3 (root enabler is the default-off arm).**
- **H0.3 — Gate codex bypass mode behind an in-app confirmation** (not just an env var),
  refuse to pair it with web/LLM-derived task strings, and force an ephemeral temp
  workspace when it is on. Document "bypass = full RCE." **Ref: F-I-2.**

### P1 — high value, contained

- **H1.1 — SSRF: guard ALL requests, not just top-level navs.** Run `_is_ssrf_target` on
  subresource/XHR/fetch (cache per host); restrict `/v1/open` to the provider allowlist and
  block loopback/link-local/RFC-1918/`metadata.google.internal`. **Refs: F-C-4, F-D-1.**
- **H1.2 — Isolate the bridge token from provider pages.** Inject bridge JS in an isolated
  content world; scope tokens per-capability (read-only auth-probe token that can't reach
  `/v1/task`/`/v1/open`). **Ref: F-D-4.**
- **H1.3 — Wire or delete `shell.blocked_commands`.** Either additively feed it into
  `shell.py`'s deny-set (never removing hard-coded entries) or remove the key and document
  the deny-list as code-owned. **Ref: F-G-4.**
- **H1.4 — Bump Pillow to `>=12.1.1` (drop the `~=10.0` cap), pytest to `>=9.0.3`;** re-run
  all four suites. **Ref: F-G-6.**
- **H1.5 — Harden session transcripts:** `0o700` dirs + `0o600` files for `kim_sessions/`;
  optionally scrub tool-result text through the existing logger scrubber before persist.
  **Ref: F-I-3.**
- **H1.6 — Verify signatures at install time.** Have `install-kim.sh`/`.ps1` run
  `cosign verify-blob` against the published `.sig`/`.pem` (fail closed; warn loudly if
  cosign absent), keeping sha256 as a fast pre-check only. **Ref: F-I-1.**

### P2 — defense-in-depth / hygiene

- **H2.1 — Restrict CDP exposure:** random loopback port + `--remote-debugging-address=127.0.0.1`,
  per-launch token where supported; `0o700` on `sessions/chrome_data`; warn when 9222 is
  reachable outside Kim. **Ref: F-I-4.**
- **H2.2 — Clamp model-supplied timeouts** in `code.py`/web-wait tools (mirror shell's
  `MAX_SHELL_TIMEOUT_S`); spawn code-exec with `start_new_session` + process-group kill.
  **Refs: F-C-5, F-C-6.**
- **H2.3 — Cap the agent stdout forwarder** (per-line size limit + back-pressure). **Ref: F-D-5.**
- **H2.4 — Drop or token-gate `/v1/health`.** **Ref: F-D-3.**
- **H2.5 — Fix `KIM_PROJECT_ROOT` precedence** (explicit env should win). **Ref: F-D-2.**
- **H2.6 — Remove dead connector stubs** (`guc_cms`/`guc_mail`). **Ref: F-C-7.**
- **H2.7 — CI supply-chain gates:** add `gitleaks` (was unavailable in this audit),
  `pip-audit`, `cargo audit`, and `npm audit` as CI checks so dep-CVEs and committed
  secrets are caught continuously. **Refs: F-G-6, F-I-1.**

---

## 8. Verified-clean / strengths (so reviewers don't re-chase ghosts)

- **Secret logging:** clean — `logger.py` scrubber redacts keys/tokens/PEM (tested).
- **Committed secrets:** none found (sampled full history; only test fixtures). Recommend a
  CI gitleaks gate since the tool wasn't available here.
- **Path sandbox:** symlink/`..`-resolving containment + secret-file globs — genuinely closed
  for declared path args.
- **Perm hygiene:** bridge token, CLI key store `0o600`; codex temp home `0o700`;
  approval-broker socket in a `0o700` `TemporaryDirectory`. (Only session transcripts miss it.)
- **Process reaping:** codex/shell spawned as process-group leaders; timeouts kill the group.
- **Bridge auth:** 127.0.0.1-bound, constant-time token compare, 32 MB body cap, token file `0o600`.
- **Shell subprocess env** strips provider API keys (a plain `env` dump won't leak them).
- **`git.py` MCP tools** (distinct from `run_command`) are safe: fixed subcommands via
  `create_subprocess_exec`, `--` pathspec separators, checkout target rejects `..`/absolute/`-`.

---

*Finding detail and reproduction steps live in `docs/ops/findings/team-{c,d,g,i}.md`.*
