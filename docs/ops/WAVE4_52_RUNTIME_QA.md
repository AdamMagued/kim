# Wave 4 issue #52 — Wave-2 runtime QA checklist

**Overall status:** PENDING — not executed. Static tests and source inspection cannot close issue #52.

This checklist verifies Wave-2 behavior across the live React → Tauri → Python → MCP/browser process seams. Run it against a clean build with `cd desktop; npm run tauri dev`. Any Rust change made after launch requires stopping and restarting `tauri dev`; hot reload does not reload Rust. A human operator or an E2E harness must fill every **Actual**, **Result**, and **Artifact** field. Do not infer PASS from unit tests, console output copied from another commit, or this template's expected results.

## Non-negotiable safety rules

- Never paste, print, screenshot, or attach access tokens, bridge tokens, cookies, authorization headers, `.env` contents, browser profiles, or OAuth callback query strings. Redact before saving evidence.
- Use a disposable test account/profile and non-sensitive prompts. Do not browse to real internal services to test SSRF.
- The Code tab must use only Ollama Cloud or a browser provider; never select OpenAI authentication or an OpenAI model for a Code-tab case.
- Do not weaken the shell/sandbox deny-list, secret-file protections, HITL gate, or network allow/deny policy for testing.
- Killing a process, occupying a port, injecting malformed stdout, delaying a response, or inspecting a scoped token requires the repo owner's approval and a written rollback plan. Use an existing test/fault-injection harness where possible; do not edit production source to manufacture a result.
- Stop immediately on unexpected secret exposure, access to an internal/metadata target, a second accepted task, a bypassed HITL prompt, a lost sentinel/protocol marker, or a process that cannot be identified safely. Preserve redacted evidence, restore the test environment, and record a handoff under **Failure handoff**.

## Run record

| Field | Value |
|---|---|
| Operator | |
| Git commit (`git rev-parse HEAD`) | |
| Branch (`git branch --show-current`) | |
| Dirty tree (`git status --short`) | |
| Started / finished (ISO-8601 with timezone) | |
| Windows edition/build (`Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsBuildNumber`) | |
| CPU / RAM | |
| Node / npm / Rust / Python versions | |
| Tauri dev command and terminal artifact | |
| `ipc_protocol` value | |
| Chat provider/model (non-secret) | |
| Code provider/model (Ollama Cloud or browser only) | |
| Browser + version / disposable profile | |
| Bridge port / CDP port (numbers only) | |
| Owner approvals for controlled failures | |
| Redactions performed | |
| Overall result (PASS/FAIL/BLOCKED) | PENDING |

## Safe prerequisites and evidence capture

1. Confirm the branch and commit, then confirm no unrelated dev server is running. Record the output without environment variables:

   ```powershell
   git status --short --branch
   git rev-parse HEAD
   Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'kim|python|node|cargo|chrome' } | Select-Object ProcessId, ParentProcessId, Name
   Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 18991..19010 -or $_.LocalPort -eq 9222 } | Select-Object LocalAddress, LocalPort, OwningProcess
   ```

   Do not capture `CommandLine`: it may contain credentials.

2. Use a clean disposable browser profile and a provider test account. Back up only non-secret test data needed for rollback. Confirm the test prompts cannot mutate valuable files or accounts.
3. Start the app from a new terminal and keep the complete terminal transcript:

   ```powershell
   Set-Location desktop
   npm run tauri dev
   ```

4. Create an evidence directory outside tracked source, or use the issue's approved artifact store. Recommended names are `52-<case>-<commit8>-<UTC timestamp>.<ext>`, for example `52-lifecycle-a1b2c3d4-20260713T120000Z.png`. Each artifact pointer below must name the file plus the relevant timestamp/line range. Redact first.
5. Capture logs through Settings → Feedback → Reveal logs. Runtime logs are normally `logs/kim-YYYY-MM-DD.jsonl`, falling back to `~/.kim/logs`. Copy only the relevant redacted lines. For process/port snapshots use:

   ```powershell
   Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'kim|python|node|cargo|chrome' } | Select-Object ProcessId, ParentProcessId, Name
   Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 18991..19010 -or $_.LocalPort -eq 9222 } | Select-Object LocalAddress, LocalPort, OwningProcess
   ```

## Runtime cases

For every case, replace the blank fields. Allowed results are **PASS**, **FAIL**, or **BLOCKED**; unexecuted cases remain **PENDING**. A blocked case must state the missing prerequisite.

### 52-A — typed terminal lifecycle: completion, cancel, and forced error

**Findings covered:** F-H-1, F-H-2, F-F-5.

Steps:

1. In Chat, run a harmless prompt that completes. Repeat in Code using Ollama Cloud or a browser provider.
2. For each run, observe the spinner, terminal message/banner, input controls, and ability to start one subsequent task.
3. Start another harmless long-running task in each tab and cancel it from the UI. Confirm cancellation once; then start a fresh task.
4. With owner approval, run the existing controlled-error fixture/harness so Chat and Code each terminate with a known failure. If no approved fixture exists, mark this subcase BLOCKED; do not edit production code or invent malformed output.

Expected:

- Success, cancellation, and failure each produce exactly one terminal lifecycle outcome for the owning run. The spinner clears promptly, controls re-enable, and the next task starts.
- Cancellation is not rendered as success. Forced failure has a visible actionable error and is not silently swallowed or left spinning.
- Typed events and preserved legacy output do not create duplicate answers/statuses. `[STATUS]`, `[PLAN]`, `[STEP]`, `[DONE]`, `[CONTEXT]`, `[UI]`, and `[END_OF_RESPONSE_{id}]` behavior remains intact when those markers occur.

Actual:
Result: PENDING
Artifacts (success/cancel/error in both tabs):

### 52-B — session switch has no event or terminal-state bleed

**Findings covered:** F-H-1, F-H-8, F-F-2.

Steps:

1. Start a run in session A that emits several progress events and remains active long enough to switch sessions.
2. Switch to session B before A completes. Record B before and after A terminates.
3. Return to A, verify its progress and terminal result, then repeat with A cancelled while B is visible.
4. Repeat for a Code browser-provider run.

Expected: B never receives A's answer, status, plan, activity, error, done, cancel, spinner, or history. A retains its own activity and terminal outcome when revisited. Missing envelopes never default to the currently mounted view.

Actual:
Result: PENDING
Artifacts:

### 52-C — abnormal child death recovers the UI

**Findings covered:** F-F-5 plus lifecycle cleanup.

Steps:

1. Obtain owner approval for process termination and record the exact disposable run/PID relationship first.
2. Start a harmless long-running Chat task. Identify only its child Python PID using the process snapshot and parent PID; have a second operator verify the PID.
3. Terminate that exact child with `Stop-Process -Id <approved-child-pid>` (add `-Force` only if explicitly approved). Never stop by process name.
4. Observe the UI, logs, child/process cleanup, and ability to submit another task. Repeat for Code only if separately approved.

Expected: abnormal death becomes a visible failure, spinner clears without waiting indefinitely, the runner slot is released, owned descendants are reaped, and a subsequent task works. No unrelated process is stopped.

Actual:
Result: PENDING
Approval / PID verification / rollback:
Artifacts:

### 52-D — undecodable stdout remains visible as a protocol error

**Findings covered:** F-H-3.

Steps:

1. Use only an existing approved E2E/fake-sidecar fixture that emits one non-secret, intentionally undecodable line and then a valid terminal event in typed mode.
2. Run it once through the Chat spawn path and, if supported, Code. Capture the UI and redacted Rust terminal/log lines.
3. If no harness can select the fake child without a source/config change, mark BLOCKED and file the missing-harness handoff.

Expected: the malformed line is not silently dropped; the UI or logs expose a bounded/rate-limited protocol error or raw diagnostic, the following valid event is still processed, and the run terminates normally. No malformed content is interpreted as a control marker.

Actual:
Result: PENDING
Fixture/version:
Artifacts:

### 52-E — HITL approve and deny do not stall

**Findings covered:** HITL hard-block and single stdin transport.

Steps:

1. Set permission mode to an ask mode. Submit a disposable action known to require approval; do not use a secret file, destructive command, or valuable target.
2. Approve once and time from click to continued activity/terminal outcome.
3. Repeat and deny once. Time from click to denial/terminal outcome.
4. Repeat on the Code path if its selected transport supports native approvals.

Expected: the action never executes before approval. Approve continues once; deny hard-blocks execution and finishes/reports cleanly. Neither choice stalls for approximately 120 seconds, consumes the next response, duplicates a decision, or leaves the spinner running.

Actual:
Result: PENDING
Approve latency / deny latency:
Artifacts:

### 52-F — one runner across GUI, `/v1/task`, and `kimctl`; cancellation releases it

**Findings covered:** shared `TaskRuntime` / single-runner contract.

Steps:

1. Start a harmless long GUI run and note its run/session identity.
2. While it runs, use the paired automation CLI to submit a second harmless task: `python -m kimctl send "runner collision probe" --timeout 10`; record output and `$LASTEXITCODE`.
3. With an approved harness or token-redacting client, concurrently POST the equivalent harmless request to `/v1/task`. Never print the `X-Kim-Token` header. If no safe client exists, mark only this subcase BLOCKED.
4. Confirm both competing submissions are rejected as already running and do not spawn children.
5. Cancel the first run using `python -m kimctl cancel`; record `$LASTEXITCODE`. Confirm the GUI reflects cancellation, all owned children exit, and one subsequent GUI task is accepted.
6. Repeat with `kimctl` as the first accepted runner and GUI as the collision attempt.

Expected: exactly one run owns the shared slot. Every competitor receives a prompt, stable rejection rather than queuing/spawning; cancellation targets the current owner and releases the slot once. The rejected prompt never appears later.

Actual:
Result: PENDING
Artifacts (including `$LASTEXITCODE`):

### 52-G — CDP loopback bind and owned-Chrome reap

**Findings covered:** F-I-4, F-J-3.

Steps:

1. Before browser-provider launch, record listeners and Chrome PIDs with the safe commands above.
2. Start a browser-provider task that causes Kim to launch its disposable Chrome. Record the new Chrome PID and CDP listener owner.
3. Confirm the CDP listener address. Complete/cancel the run, then exit Kim normally and wait a short documented grace period.
4. Record listeners/PIDs again. Repeat launch and app exit once to detect accumulation. Do not kill a Chrome Kim did not launch.

Expected: CDP listens only on `127.0.0.1` (never `0.0.0.0`, `::`, or a LAN address). Kim records/reaps only the Chrome PID it owns; normal app exit leaves no owned Chrome, child, zombie, or port 9222 listener. Pre-existing user Chrome remains untouched and repeated launches do not accumulate processes.

Actual:
Result: PENDING
Pre/post PID and port map:
Artifacts:

### 52-H — SSRF guards reject controlled internal targets without contacting real services

**Findings covered:** F-D-1, F-I-4 defense in depth.

Safety preflight (required for both subcases):

1. Run only in an owner-approved disposable VM, network namespace, or equivalent firewall-contained E2E harness. The harness must own every synthetic target address and listener and deny all network egress except an explicitly approved public-provider flow. Never run these probes on a normal workstation, corporate/VPN network, cloud host, or network that could route to an actual router, internal service, or instance-metadata endpoint.
2. Allocate fresh synthetic loopback, private, link-local, and IPv6 target addresses to harness-controlled fake listeners. Do **not** use common gateway or real metadata addresses such as `192.168.0.1` or `169.254.169.254`. Record the harness manifest and firewall/namespace rules, with secrets redacted.
3. Before launching Kim, prove non-harness egress is denied and every synthetic target maps only to its controlled canary. If containment cannot be proved, mark both subcases BLOCKED and send no probe.
4. Stop immediately if a target is not demonstrably harness-owned, an unexpected destination is attempted, any request escapes containment, or a blocked-target canary receives a connection. Preserve redacted evidence, file a security handoff, and do not continue this case.

#### 52-H1 — Tauri `/v1/open` rejects a directly supplied internal host

Surface under test: the Tauri provider webview/open path. Its Rust guard classifies only the host in the URL initially supplied to `/v1/open`, before creating or navigating the webview.

Steps:

1. With an owner-approved token-redacting client, submit separate `POST /v1/open` requests containing harness-owned synthetic numeric hosts: loopback, RFC-1918/private, link-local (not a real metadata address), IPv6 loopback/link-local, and one browser-accepted numeric encoding of a harness-owned blocked address. Never print or attach the `X-Kim-Token` value.
2. For each request, record the returned safe error, webview/window state, and matching controlled-canary connection count.
3. Submit one supported public HTTPS provider sign-in URL through the same surface and complete the disposable provider flow, only under the harness's explicit egress allowlist.

Expected: every directly supplied synthetic internal literal is rejected before webview creation/navigation, and its controlled canary records zero connections. The supported public HTTPS provider flow opens and its documented OAuth callback continues to work. DNS resolution/rebinding and redirects, frames, or subresources after the initially supplied Tauri URL are not PASS criteria: the current host-based Rust classifier neither resolves DNS nor installs per-request interception. Record desired coverage as an out-of-scope hardening request, not a PASS.

Actual:
Result: PENDING
Artifacts (redacted request/result matrix and zero-connection canary evidence):

#### 52-H2 — MCP Playwright intercepts redirects, frames, and subresources

Surface under test: the MCP Playwright browser, whose page-level route handler evaluates each outgoing request separately from the Tauri `/v1/open` guard.

Steps:

1. In the isolated harness, open an approved controlled fake origin through MCP `web_open`; it must not require general network egress.
2. Have that origin perform separate probes to harness-owned blocked numeric targets: an HTTP redirect, iframe/frame navigation, and representative subresources (for example image plus XHR/fetch). Use a fresh canary counter for every probe.
3. Capture the MCP tool result, redacted browser/server logs, and all controlled-canary connection counts. Never capture a blocked target's response body.
4. If separately permitted by the harness egress allowlist, exercise the supported public provider sign-in flow to confirm ordinary public HTTPS navigation remains usable.

Expected: the Playwright route aborts each redirect hop, frame navigation, XHR/fetch, and other subresource request to a harness-owned blocked numeric address before its controlled canary receives a connection. Public HTTPS navigation remains usable when explicitly allowed. DNS resolution and rebinding are out of scope for `_is_ssrf_target`, which allows plain DNS names; do not report PASS merely because a hostname resolves internally. If resolution-aware blocking is required, record an explicit expected defect/hardening handoff.

Actual:
Result: PENDING
Artifacts (redacted probe matrix and zero-connection canary evidence):

### 52-I — provider-page token is callback-only and never logged

**Findings covered:** F-D-4.

Steps:

1. Use a disposable provider page and owner-approved inspection. Verify by redacted presence/absence only that the page receives a scoped capability distinct from the full bridge credential; never copy its value into evidence.
2. Through an approved test helper that suppresses headers, attempt with the scoped capability: `POST /v1/callback`, then a non-callback route such as `POST /v1/task` and `POST /v1/open`, plus the wrong method on `/v1/callback`.
3. Search the captured terminal, browser console, and structured log artifacts locally for the known value, then record only `0 matches` or `LEAK DETECTED`; do not attach the search output/value.

Expected: the scoped capability authorizes only `POST /v1/callback`. All other method/path pairs return unauthorized and cause no task/navigation. The full bridge credential is never injected into provider content. Neither token value appears in console, terminal, structured logs, screenshots, or errors.

Actual:
Result: PENDING
Authorization matrix / leak-search outcome (no values):
Artifacts:

### 52-J — `kim` and `kimctl` exit-code contracts

**Findings covered:** F-E-1, F-E-4, F-E-7.

Run each command in a fresh PowerShell statement and immediately record `$LASTEXITCODE`; do not use a pipeline between the command and the check.

Steps:

1. Run `kim doctor` with one required dependency deliberately unavailable only via the approved test environment; record `$LASTEXITCODE`. Run `kim doctor --strict` with an optional/provider check unavailable.
2. Run successful one-shot `kim chat "<harmless prompt>"`; record `$LASTEXITCODE`.
3. With an approved fixture/provider, run one-shot agent-declared failure, Ctrl-C cancellation, and no-response cases; record each `$LASTEXITCODE`.
4. Complete one `kimctl send` in session S, then send a second distinguishable prompt with `python -m kimctl send "<second prompt>" --session <S> --timeout 60`; record output, elapsed time, and `$LASTEXITCODE`.
5. Exercise `kimctl` success, need-help, timeout, transport failure, and failed cancel/browser operations; record output and `$LASTEXITCODE`.

Expected: required doctor failure is non-zero; strict gates optional failure. Successful one-shot is 0; declared failure, cancellation, and no response are non-zero. The resumed `kimctl send` waits for and reports only the second task, never stale `TASK_COMPLETE`/`NEED_HELP`. `kimctl` returns 0 success, 1 need-help, 2 timeout, and 3 transport/failed bridge operation, with no raw traceback.

Actual:
Result: PENDING
Command/output/`$LASTEXITCODE` table:
Artifacts:

### 52-K — retry collapse and long-history render performance

**Findings covered:** F-F-11 and retry-collapse correctness.

Steps:

1. Load an approved synthetic/non-sensitive session with at least 500 messages, including consecutive duplicate structured retry/tool-call messages and nearby distinct messages. Record fixture size/hash.
2. Open the session and record time to usable UI, scrolling responsiveness, process memory, and React render/profile evidence.
3. Trigger frequent token/stat updates during a run. Confirm saved history does not fully rerender on every tick.
4. Edit/retry the intended visible message after duplicate retries have collapsed. Confirm the right raw source message is used and the retry count is correct.
5. Complete enough disposable runs to exceed the in-memory run-history cap, reopen the view, and check that only the bounded recent set persists/renders.

Expected: consecutive equivalent structured retries collapse with an accurate count; distinct/plain messages are not wrongly collapsed. Edit/retry targets the visible message's correct source index. Token updates remain responsive and do not repeatedly traverse/render all saved messages. Run history remains bounded; no whole-history O(n²) growth, UI freeze, or unbounded memory trend is observed.

Actual:
Result: PENDING
Measurements / profiler comparison:
Artifacts:

## Result roll-up

| Case | Result | Artifact pointer | Defect / blocker |
|---|---|---|---|
| 52-A typed lifecycle | PENDING | | |
| 52-B session isolation | PENDING | | |
| 52-C abnormal death | PENDING | | |
| 52-D undecodable stdout | PENDING | | |
| 52-E HITL | PENDING | | |
| 52-F single runner | PENDING | | |
| 52-G CDP/reap | PENDING | | |
| 52-H SSRF/OAuth | PENDING | | |
| 52-I scoped token | PENDING | | |
| 52-J CLI exit codes | PENDING | | |
| 52-K retry/performance | PENDING | | |

Issue #52 may be marked PASS only when every required case is PASS on the recorded commit. Any required FAIL makes the issue FAIL. BLOCKED is acceptable only as an honest run status and does not close the issue.

## Failure handoff

On FAIL or BLOCKED, stop the affected case and record:

- case ID, exact UTC time, commit, provider/transport, and last safe action;
- expected versus observed result, minimal redacted reproduction, and whether it reproduces after one clean Rust restart;
- relevant redacted log line ranges, screenshots/video, process tree, listener map, and `$LASTEXITCODE` values;
- cleanup performed and any process/port still present;
- invariant or security impact, especially secret exposure, HITL bypass, wrong-session mutation, duplicate runner, internal navigation, or lost terminal state;
- the narrowest suspected owning files, without making a code fix in this QA task.

Do not continue security cases after a control fails. File the defect/handoff and leave the overall status FAIL or BLOCKED. Static suites may support diagnosis but cannot replace this live checklist or close #52.
