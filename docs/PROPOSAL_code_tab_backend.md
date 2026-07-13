# Proposal: Code Tab Backend Options

> **Status:** superseded (by `PROPOSAL_codex_appserver_parity.md`) — 2026-07-13
> Its recommendation (Option C: replace the Codex CLI with the Kim agent) was never
> adopted; the code tab still runs the Codex CLI, and ROADMAP.md Phase 3 adopts the
> app-server parity proposal "as-is" (implemented code-complete 2026-07-06), which
> keeps Codex. Retained for the option analysis and constraints.

**Date:** 2026-06-11  
**Branch:** `production-roadmap`

---

## Background

The Code tab routes tasks through a different execution path than the chat tab.
When the user opens an external project directory (`is_codex = true` in
`subprocess.rs`), Tauri spawns `python -m orchestrator.run_codex_bridge` which
starts an aiohttp proxy, writes a temp Codex config, and launches the Codex CLI
binary (`codex exec --json`).  The Codex CLI thinks it is talking to an OpenAI
endpoint; the proxy transparently rewrites requests through either a
`BrowserProvider` (browser-tab LLM) or an Ollama cloud model.

**Hard constraint (non-negotiable):** The Code tab must NEVER use OpenAI auth or
gpt-5.5.  Only Ollama cloud (`ollama` provider with `cloud` mode) or a browser
provider (`browser:<site>`) may serve Code tab requests.  This constraint is
enforced in `tests/test_invariants.py::TestCodeTabConstraint`.

---

## Current Architecture (baseline)

```
User picks project dir
       │
       ▼
Tauri send_task  (subprocess.rs:find_code_backend)
       │  is_codex=true
       ▼
python -m orchestrator.run_codex_bridge
       │
       ▼
orchestrator/codex_bridge_service.py :: _run_async()  (spawns codex itself)
       │  starts aiohttp proxy on ephemeral port
       ▼
codex exec --json --dangerously-bypass-approvals-and-sandbox
       │  OPENAI_BASE_URL=http://127.0.0.1:{port}/v1
       ▼
_CodexProxy intercepts /v1/responses
       │
       ▼
BrowserProvider.complete()  OR  OllamaProvider.complete()
```

**What gets deleted if we move away:** `orchestrator/run_codex_bridge.py`,
`mcp_server/tools/codex_bridge.py`, and the `find_code_backend()` logic plus
all `is_codex` branches in `subprocess.rs`.  The bundled `codex` and/or `claw`
sidecar binaries also become dead weight.

---

## Option A — Keep Codex CLI as-is

### Architecture sketch

No changes to the current path.  Continue to ship a bundled `codex` binary
(and optionally `claw` as fallback), proxy LLM calls through the aiohttp bridge.

### Migration steps

None.

### What gets deleted

Nothing.

### Risks

- **External binary dependency.** Codex CLI is a third-party binary that must be
  vendored, notarized, and updated independently.  Its JSONL output format can
  change between releases, breaking `ChatView`'s `item.completed` parser.
- **Brittle proxy.** The aiohttp proxy rewrites OpenAI-format `/v1/responses`
  calls to whatever the backing provider supports.  Any mismatch in request/
  response shape silently fails or produces garbled output.
- **Bypass flags.** Codex is invoked with
  `--dangerously-bypass-approvals-and-sandbox`, which is fine for a sandboxed
  desktop app but is a maintenance hazard: if Codex removes or renames that flag,
  the Code tab silently stops working.
- **No MCP tooling.** Codex CLI manages its own tool loop; none of Kim's 31
  MCP tools (browser, UI observe, OCR, etc.) are available to the Code agent.

### Effort estimate

0 — status quo.

---

## Option B — Bundled claw only (drop Codex CLI)

### Architecture sketch

Keep the aiohttp proxy and `run_codex_bridge.py` / `codex_bridge.py`, but
replace the `codex` binary with `claw` exclusively.  `find_code_backend()` still
resolves: CODEX_BIN env → bundled `claw` → system `claw`.  The frontend and
proxy are unchanged.

### Migration steps

1. Remove the bundled `codex` binary from the release build.
2. Update `find_code_backend()` to skip the `codex` search paths; keep only
   `claw`.
3. Update README / install docs.
4. Verify `claw` output is compatible with the existing `item.completed` JSONL
   parser — run the golden-transcript tests with a real `claw` binary.

### What gets deleted

Bundled `codex` binary + its update machinery.  `find_code_backend()` shrinks
by ~15 lines.

### Risks

- `claw` is also an external binary with the same notarization, versioning, and
  flag-stability concerns as Codex CLI — just one instead of two.
- `claw` may not support identical JSONL output format; the `item.completed`
  parser may need adjustment.
- **Provider constraint:** same as Option A — the constraint is enforced in
  `subprocess.rs`, not in `claw`, so there is no new risk here.

### Effort estimate

S (half-day): binary removal + one-line change in `find_code_backend()` + test
sweep with real claw binary.

---

## Option C — Replace Codex CLI with Kim agent (code-only toolset)

### Architecture sketch

Remove the Codex CLI path entirely.  When `is_codex = true`, Tauri spawns the
standard `python -m orchestrator.agent` subprocess — exactly as in the chat tab
— but configures it with a restricted, code-focused MCP toolset:
`code.py`, `git.py`, `files.py`, `search.py` (and optionally `shell.py`).
Browser-tab and OS-control tools are not loaded.

```
User picks project dir
       │
       ▼
Tauri send_task  (is_codex=true → spawn orchestrator.agent with code profile)
       │
       ▼
python -m orchestrator.agent  (provider: ollama-cloud or browser:*)
       │
       ▼
KimAgent loop  →  MCP server  →  code.py / git.py / files.py / search.py
```

The `provider` restriction (no OpenAI auth / gpt-5.5) is enforced at the same
point as today: `subprocess.rs:configure_codex_direct_provider()` is called
before spawning, and the test invariants remain unchanged.

**IPC protocol:** The Kim agent already emits `kim:*` typed events for every
phase (status, context, plan, tool, run_done, run_failed).  `ChatView` already
renders these for the chat tab.  No new parsing is required.

### Migration steps

1. Add a `code_tools_only` config flag (or a `--profile code` CLI arg) to the
   MCP server that restricts which tools are registered at startup.
2. Update `send_task` in `subprocess.rs`: when `is_codex = true`, spawn
   `orchestrator.agent` (not `run_codex_bridge`) with the new profile flag.
3. Remove `find_code_backend()`, all `is_codex` binary-resolution paths, and the
   `--dangerously-bypass-approvals-and-sandbox` flag usage.
4. Delete `orchestrator/run_codex_bridge.py` and
   `mcp_server/tools/codex_bridge.py` (aiohttp proxy).
5. Remove the bundled `codex` and `claw` sidecar binaries and their
   notarization machinery.
6. Update `ChatView`'s Code tab rendering: since the agent emits typed events
   (not Codex JSONL), the `codex_agent_message` / `codex_shell_call` parse path
   becomes unused.  The chat-tab rendering already covers all agent events.
7. Update golden-transcript tests: add a Code-tab profile variant.
8. Update `tests/test_invariants.py::TestCodeTabConstraint` to verify that
   `is_codex = true` still never resolves to OpenAI.

### What gets deleted

| File / component | Lines |
|---|---|
| `orchestrator/run_codex_bridge.py` | ~200 |
| `mcp_server/tools/codex_bridge.py` | ~350 |
| `subprocess.rs`: `find_code_backend()`, all `is_codex` binary branches | ~250 |
| Bundled `codex` + `claw` binaries | N/A |
| `codexEvents.ts` parsing (newly extracted in V-4a) | ~50 |
| Codex-branch parsers in `parsers.ts` (`codex_agent_message`, etc.) | ~25 |

Total Python/Rust/TS deleted: ~875 lines.  Both sidecar binaries go away.

### Risks

- **Behavior regression.** Codex CLI manages a sophisticated multi-step tool
  loop with its own re-try, plan, and summarise logic.  Kim's agent loop does
  the same, but users familiar with Codex CLI output will see a different
  experience (different status messages, different formatting).
- **Feature parity gap.** If users relied on Codex-specific capabilities (e.g.
  its diff-apply semantics, its patch-mode output), those would not be available.
  Needs a UX review before shipping.
- **MCP tool completeness for coding.** The four tools (`code.py`, `git.py`,
  `files.py`, `search.py`) cover the core coding loop, but Codex CLI can also
  run arbitrary shell commands.  Adding `shell.py` to the code profile restores
  this — but re-introduces the risk of accidental OS-control commands.  A
  `shell.py` subset (read-only file ops, `grep`, `find`, safe `git` subcommands)
  might be the right middle ground.
- **Longer migration.** Steps 1–8 touch four layers (Python, Rust, TypeScript,
  tests).  Each layer needs its own test sweep.

### Effort estimate

M–L (2–4 days for a single developer): MCP profile flag (S), `subprocess.rs`
re-routing (S), deletion pass (S), ChatView Code-tab render update (S), test
updates (M), end-to-end smoke test on a real project (M).

---

## Comparison summary

| Criterion | A (keep Codex) | B (claw only) | C (Kim agent) |
|---|---|---|---|
| External binary dependency | 2 (codex + claw) | 1 (claw) | 0 |
| Proxy / bridge code to maintain | Yes | Yes | No |
| Lines deleted | 0 | ~15 | ~875 |
| MCP tools available to Code agent | No | No | Yes (4+) |
| IPC unification | No | No | Yes (same protocol) |
| Risk of binary format breakage | High | Medium | None |
| Migration effort | 0 | S | M–L |
| Provider constraint preserved | Yes | Yes | Yes |

---

## Recommendation: Option C

**Rationale:**

1. **IPC unification.** After V-1 (kill dual-emit) and V-3 (golden-transcript),
   the Kim-agent path emits a clean, tested, typed event stream.  The Codex CLI
   path still uses a parallel JSONL format that requires its own parser and test
   coverage.  Unifying on a single path eliminates a class of future bugs.

2. **No external binary.** Removing the sidecar binary dependency eliminates the
   notarization, update, and format-stability risks in Option A/B.  This is the
   largest operational risk on the current path.

3. **Full MCP toolset.** Using the Kim agent for coding gives it access to
   `observe_ui`, `web_open`, `take_screenshot`, and all other MCP tools —
   capabilities the Codex CLI path can never have.

4. **Deletion pays for the migration.** ~875 lines deleted vs. ~100 lines added
   (profile flag + subprocess re-route).  The code surface shrinks.

**Prerequisite before shipping Option C:** A side-by-side UX comparison of the
Kim agent vs. Codex CLI on 5–10 representative coding tasks.  If the Kim agent
produces clearly inferior results on multi-file refactors, the migration should
be deferred until the agent loop is stronger in that domain.

**Provider constraint:** All three options preserve the constraint (no OpenAI
auth / gpt-5.5 in the Code tab), and the existing invariant tests cover it
regardless of which path is chosen.  Option C makes it simpler to enforce because
there is no longer a separate code path that needs its own constraint check.

---

*TODO(human): Review and decide before merging to main.  Option C requires a
UX sign-off meeting.*
