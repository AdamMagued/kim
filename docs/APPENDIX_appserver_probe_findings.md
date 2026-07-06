# codex app-server protocol probe — verified against codex-cli 0.134.0 (2026-07-06)

Schema bundle: `./schema/` (generated via `codex app-server generate-json-schema --out schema`).
v1 and v2 subdirs exist; the flat files are the current (v2) protocol.

## Transport
- `codex app-server` (default `--listen stdio://`); newline-delimited JSON-RPC 2.0 on stdin/stdout.
- Also supports `unix://PATH` and `ws://IP:PORT` if we ever want the Tauri app to attach directly.

## Verified live
1. `initialize` → `{"id":1,"result":{"userAgent":...,"codexHome":"/Users/adammaged/.codex",...}}`
   Params: `{clientInfo: {name, title, version}, capabilities?}`.
2. `thread/start` with ALL of the following accepted in one call (no temp CODEX_HOME needed):
   - `modelProvider: "kim-proxy"`, `model: "kim-browser"`
   - inline `config` overrides (dotted keys): `model_providers.kim-proxy.base_url = http://127.0.0.1:<port>/v1`, `wire_api = "responses"`, `name`
   - `approvalPolicy: "on-request"`, `sandbox: "workspace-write"`, `cwd`, `ephemeral: true`
   - Response: `thread.id == sessionId` (uuid-v7), `approvalsReviewer:"user"`,
     `sandbox: {type:"workspaceWrite", networkAccess:false, writableRoots:[...]}`, `reasoningEffort`.
   - Non-ephemeral threads persist → `thread/resume` for cross-restart continuity.

## Client → server methods the bridge needs
- `initialize`, `thread/start`, `thread/resume`, `turn/start`, `turn/interrupt`, `turn/steer`
- `thread/compact/start` (native compaction!), `review/start`, `thread/list`, `thread/read`
- `turn/start` params: `{threadId*, input*: [{type:"text", text:"..."}], model?, cwd?, approvalPolicy?, sandboxPolicy?, outputSchema?, effort?, summary?}`

## Server → client REQUESTS (must answer or codex hangs)
- `item/commandExecution/requestApproval` — params: `{threadId*, turnId*, itemId*, startedAtMs*, approvalId?, command?, commandActions?, cwd?, networkApprovalContext?, proposedExecpolicyAmendment?, proposedNetworkPolicyAmendments?, reason?, riskAssessment?...}`
  Response: `{decision}` where decision ∈ `"accept" | "acceptForSession" | {acceptWithExecpolicyAmendment:{execpolicy_amendment:[...]}} | ...decline variants (see CommandExecutionRequestApprovalResponse.json)`
- `item/fileChange/requestApproval` / `applyPatchApproval` (v1) / `execCommandApproval` (v1)
- `item/tool/requestUserInput`, `item/permissions/requestApproval`, `mcpServer/elicitation/request`
- `item/tool/call` (dynamic client-side tools!), `account/chatgptAuthTokens/refresh`, `attestation/generate`

## Server → client NOTIFICATIONS to render
- lifecycle: `thread/started`, `turn/started`, `turn/completed`, `thread/status/changed`, `error`, `warning`
- streaming: `item/agentMessage/delta`, `item/reasoning/textDelta`, `item/reasoning/summaryTextDelta`
- items: `item/started`, `item/completed` (typed items: commandExecution, fileChange, mcpToolCall, agentMessage, plan...)
- command output: `item/commandExecution/outputDelta`
- plan/diff: `turn/plan/updated`, `turn/diff/updated`
- tokens: `thread/tokenUsage/updated`; compaction: `thread/compacted`
- `serverRequest/resolved` (an approval was answered elsewhere / auto-resolved)

## Key implications for Kim
- Approval UX (`accept`/`acceptForSession`) maps 1:1 to Claude-style "allow once / allow always" prompts.
- Inline `config` on thread/start removes the `_write_codex_config` temp-dir machinery for app-server path
  (still needed for legacy exec path if kept).
- `thread/compact/start` + `thread/compacted` = native codex-side compaction; Kim's browser-thread
  compaction (sidecar/handoff) remains for the *browser* side. Two independent context budgets.
- `workspace-write` default has `networkAccess:false` → network commands trigger approval requests
  (networkApprovalContext) — exactly the "can install playwright after asking" flow.
- `turn/interrupt` = Esc-to-cancel. `turn/steer` = mid-turn user injection.
- Probe scripts used are in this dir's history; handshake ~instant, thread/start <1s.
