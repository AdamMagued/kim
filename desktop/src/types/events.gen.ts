// events.gen.ts -- DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via `npm run gen:events`.
// To add or change an event, edit the schema and rerun the generator.

/** All typed IPC event names emitted by the Kim agent. */
export const KimEventNames = {
  STATUS: 'kim:status' as const,
  PLAN: 'kim:plan' as const,
  STEP: 'kim:step' as const,
  DONE: 'kim:done' as const,
  CONTEXT: 'kim:context' as const,
  STATS: 'kim:stats' as const,
  UI: 'kim:ui' as const,
  RUN_DONE: 'kim:run-done' as const,
  RUN_FAILED: 'kim:run-failed' as const,
  PROVIDER_ERROR: 'kim:provider-error' as const,
  RATE_LIMITED: 'kim:rate-limited' as const,
  HITL_APPROVAL_REQUEST: 'kim:hitl-approval-request' as const,
  HITL_APPROVAL_RESULT: 'kim:hitl-approval-result' as const,
  TOOL: 'kim:tool' as const,
  ANSWER: 'kim:answer' as const,
  DIFF: 'kim:diff' as const,
  ACTIVITY: 'kim:activity' as const,
  COMMAND_APPROVAL_REQUEST: 'kim:command-approval-request' as const,
  FILE_CHANGE_APPROVAL_REQUEST: 'kim:file-change-approval-request' as const,
  USER_INPUT_REQUEST: 'kim:user-input-request' as const,
  COMMAND_OUTPUT: 'kim:command-output' as const,
  ASSISTANT_DELTA: 'kim:assistant-delta' as const,
  REASONING_DELTA: 'kim:reasoning-delta' as const,
  PLAN_UPDATE: 'kim:plan-update' as const,
  DIFF_UPDATE: 'kim:diff-update' as const,
  TOKEN_USAGE: 'kim:token-usage' as const,
  ITEM_LIFECYCLE: 'kim:item-lifecycle' as const,
  TURN_LIFECYCLE: 'kim:turn-lifecycle' as const,
} as const;

export type KimEventName = (typeof KimEventNames)[keyof typeof KimEventNames];

/**
 * Run-identity envelope shared by every typed payload. `run_id` is minted at
 * task spawn (Rust `send_task`, forwarded to Python as KIM_RUN_ID); `session_id`
 * is the session the run belongs to (KIM_SESSION_ID). Both are optional so
 * events from legacy/bridge streams that predate the envelope still type-check.
 * The frontend routes and files run output by these fields, never by which
 * view happens to be mounted.
 */
export interface KimRunEnvelope {
  run_id?: string;
  session_id?: string;
}

/** Legacy markers retained for the uncontrolled Codex compatibility stream. */
export const LegacyLogTags = {
  "[STATUS]": {
    "tag": "[STATUS]",
    "event": "kim:status"
  },
  "[PLAN]": {
    "tag": "[PLAN]",
    "event": "kim:plan"
  },
  "[STEP]": {
    "tag": "[STEP]",
    "event": "kim:step"
  },
  "[DONE]": {
    "tag": "[DONE]",
    "event": "kim:done"
  },
  "[CONTEXT]": {
    "tag": "[CONTEXT]",
    "event": "kim:context"
  },
  "[STATS]": {
    "tag": "[STATS]",
    "event": "kim:stats"
  },
  "[TOOL]": {
    "tag": "[TOOL]",
    "event": "kim:tool"
  },
  "[ANSWER]": {
    "tag": "[ANSWER]",
    "event": "kim:answer"
  },
  "[DIFF]": {
    "tag": "[DIFF]",
    "event": "kim:diff"
  },
  "[SUCCESS]": {
    "tag": "[SUCCESS]",
    "event": "kim:activity",
    "kind": "success"
  },
  "[FAILED]": {
    "tag": "[FAILED]",
    "event": "kim:activity",
    "kind": "error"
  },
  "[ERROR]": {
    "tag": "[ERROR]",
    "event": "kim:activity",
    "kind": "error"
  },
  "TASK_COMPLETE:": {
    "tag": "TASK_COMPLETE:",
    "event": "kim:answer"
  },
  "NEED_HELP:": {
    "tag": "NEED_HELP:",
    "event": "kim:activity",
    "kind": "error"
  }
} as const;

/**
 * K5: named bracket-tag constants — the single source of truth for the
 * text-protocol vocabulary. Hand parsers (chat/utils.ts, chat/parsers.ts)
 * must reference these instead of re-typing the literals.
 */
export const LogTags = {
  STATUS: "[STATUS]",
  PLAN: "[PLAN]",
  STEP: "[STEP]",
  DONE: "[DONE]",
  CONTEXT: "[CONTEXT]",
  STATS: "[STATS]",
  TOOL: "[TOOL]",
  ANSWER: "[ANSWER]",
  DIFF: "[DIFF]",
  SUCCESS: "[SUCCESS]",
  FAILED: "[FAILED]",
  ERROR: "[ERROR]",
  TASK_COMPLETE: "TASK_COMPLETE:",
  NEED_HELP: "NEED_HELP:",
} as const;

/** A human-readable status message from the agent loop (activity feed item). */
export interface KimStatusPayload extends KimRunEnvelope {
  /** Status text to display in activity feed. */
  message: string;
}

/** Agent emitted a structured plan with step list. */
export interface KimPlanPayload extends KimRunEnvelope {
  /** Array of plan step objects. */
  steps: unknown[];
}

/** Agent advanced to a new plan step. */
export interface KimStepPayload extends KimRunEnvelope {
  /** 1-based step index. */
  n: number;
  /** Step metadata. */
  data: Record<string, unknown>;
}

/** Agent completed a plan step. */
export interface KimDonePayload extends KimRunEnvelope {
  /** 1-based step index that finished. */
  n: number;
}

/** Context window usage snapshot. */
export interface KimContextPayload extends KimRunEnvelope {
  /** Total input tokens consumed so far. */
  cumulative_input: number;
  /** Configured context token budget. */
  budget: number;
  /** Budget phase label (ok / warn / critical / full). */
  phase: string;
  /** Percentage of budget consumed (0–100). */
  percent: number;
  /** Input tokens in the last provider call. */
  last_input: number;
  /** Output tokens in the last provider call. */
  last_output: number;
  /** Provider identifier that produced this snapshot. */
  source: string;
  /** True when token count is estimated rather than exact. */
  estimate: boolean;
}

/** Cumulative token usage for this run. */
export interface KimStatsPayload extends KimRunEnvelope {
  /** Cumulative input tokens. */
  input: number;
  /** Cumulative output tokens. */
  output: number;
  /** input + output. */
  total: number;
}

/** Window/UI control signal from the agent. */
export interface KimUiPayload extends KimRunEnvelope {
  /** 'screenshot_flash' triggers flash overlay; 'show' brings main window to front. */
  action: 'screenshot_flash' | 'show';
}

/** Run completed (success or expected failure). */
export interface KimRunDonePayload extends KimRunEnvelope {
  /** Termination reason key: task_complete | max_iterations | stuck | provider_failed | need_help | cancelled | conversational_loop. */
  termination: string;
  /** True only when termination === 'task_complete'. */
  success: boolean;
}

/** Run ended due to a recoverable or unrecoverable error. */
export interface KimRunFailedPayload extends KimRunEnvelope {
  /** Machine-readable error key (e.g. 'provider_auth', 'rate_limit'). */
  reason: string;
  /** True when the user can retry without config changes. */
  recoverable: boolean;
  /** Human-readable next-action hint. */
  suggestion: string;
}

/** Provider call failed with a specific error code. */
export interface KimProviderErrorPayload extends KimRunEnvelope {
  /** Error code: auth | rate_limit | server_error | timeout | network | invalid_request. */
  code: string;
  /** True when automatic retry is possible. */
  retryable: boolean;
}

/** Provider rate-limited; agent will retry after a delay. */
export interface KimRateLimitedPayload extends KimRunEnvelope {
  /** Seconds until next retry. */
  delay: number;
  /** Current retry attempt number (1-based). */
  attempt: number;
  /** Maximum retries configured. */
  max_retries: number;
}

/** Agent is requesting human approval before executing a high-risk tool. */
export interface KimHitlApprovalRequestPayload extends KimRunEnvelope {
  /** MCP tool name requiring approval. */
  tool: string;
  /** Risk level: high | medium. Determines UI prominence. */
  risk: string;
  /** Machine-readable reason key (e.g. 'arbitrary_code_execution'). */
  reason: string;
  /** K6: human-readable preview — command string, unified diff (<=40 lines), or URL + element label. May be empty. */
  preview: string;
  /** T1 decision id — unique per approval request. Echo it back in the hitl_approve stdin line; a decision whose id does not match the currently pending request is discarded. May be empty on legacy emitters. */
  id?: string;
}

/** Human approval decision sent back to the agent. */
export interface KimHitlApprovalResultPayload extends KimRunEnvelope {
  /** MCP tool name that was approved or denied. */
  tool: string;
  /** True if the user approved the tool call. */
  approved: boolean;
}

/** A tool invocation shown in the live activity feed. */
export interface KimToolPayload extends KimRunEnvelope {
  /** Canonical MCP tool name. */
  name: string;
  /** Tool arguments used to build the activity label. */
  args: Record<string, unknown>;
}

/** A final assistant answer to append to the conversation. */
export interface KimAnswerPayload extends KimRunEnvelope {
  /** Answer text without a legacy marker prefix. */
  text: string;
}

/** Line-count summary for a file changed by a tool. */
export interface KimDiffPayload extends KimRunEnvelope {
  /** Display-safe file basename. */
  path: string;
  /** Lines added. */
  added: number;
  /** Lines removed. */
  removed: number;
}

/** A structured status, success, or error activity item. */
export interface KimActivityPayload extends KimRunEnvelope {
  /** Activity severity and presentation kind. */
  kind: 'status' | 'success' | 'error';
  /** Human-readable activity text. */
  text: string;
}

/** Codex app-server: native per-command approval request. Block until the user answers with an approval_decision stdin line (accept | acceptForSession | decline). */
export interface KimCommandApprovalRequestPayload extends KimRunEnvelope {
  /** Approval request id — echo it back in the approval_decision stdin line. */
  id: string;
  /** Full shell command codex wants to run. */
  command: string;
  /** Working directory the command would run in. */
  cwd: string;
  /** Model-provided justification (may be empty). */
  reason: string;
  /** Risk hint: 'network' when the command needs network access, else '' (sandbox escalation). */
  risk: string;
  /** True when this is a managed-network approval (command needs network access). */
  network: boolean;
  /** Proposed execpolicy amendment tokens (allow similar commands without prompting); may be empty. */
  amendment: unknown[];
}

/** Codex app-server: native file-change (patch) approval request. */
export interface KimFileChangeApprovalRequestPayload extends KimRunEnvelope {
  /** Approval request id — echo it back in the approval_decision stdin line. */
  id: string;
  /** Changed files: [{path, kind}] best-effort; may be empty. */
  files: unknown[];
  /** Model-provided justification (may be empty). */
  reason: string;
}

/** Codex app-server: codex is asking the USER a question (item/tool/requestUserInput) or an MCP server raised an elicitation. Render the questions and answer via the respond_user_input Tauri command, which writes a {"type":"user_input","id":…,"answers":…} stdin line back to the bridge. */
export interface KimUserInputRequestPayload extends KimRunEnvelope {
  /** Request id — echo it back in the user_input stdin line (respond_user_input's id argument). */
  id: string;
  /** 'questions' (codex requestUserInput) or 'elicitation' (MCP elicitation; informational — Kim auto-declines these today). */
  kind: string;
  /** Codex thread item id the questions belong to; may be empty (always empty for elicitation). */
  item_id: string;
  /** Question objects as sent by codex: [{id, header, question, options?: [{label, description?}], isOther?, isSecret?}]. Empty for elicitation. */
  questions: unknown[];
  /** Elicitation message text; empty for kind='questions'. */
  message: string;
}

/** Codex app-server: live output chunk from a running command. */
export interface KimCommandOutputPayload extends KimRunEnvelope {
  /** Command-execution item id this chunk belongs to. */
  item_id: string;
  /** Raw output chunk (may contain partial lines). */
  chunk: string;
}

/** Codex app-server: streaming assistant-message text delta. */
export interface KimAssistantDeltaPayload extends KimRunEnvelope {
  /** Assistant text delta. */
  chunk: string;
}

/** Codex app-server: streaming reasoning text delta (provider names scrubbed). */
export interface KimReasoningDeltaPayload extends KimRunEnvelope {
  /** Reasoning text delta. */
  chunk: string;
}

/** Codex app-server: the turn's plan checklist changed. */
export interface KimPlanUpdatePayload extends KimRunEnvelope {
  /** Plan steps: [{text/step, status}] as sent by codex. */
  steps: unknown[];
}

/** Codex app-server: cumulative unified diff of the turn's file changes. */
export interface KimDiffUpdatePayload extends KimRunEnvelope {
  /** Unified diff text (whole-turn snapshot, not incremental). */
  unified_diff: string;
}

/** Codex app-server: codex-side transcript token usage snapshot. */
export interface KimTokenUsagePayload extends KimRunEnvelope {
  /** Cumulative input tokens. */
  input: number;
  /** Cumulative output tokens. */
  output: number;
  /** Cumulative total tokens. */
  total: number;
}

/** Codex app-server: a thread item started or completed. */
export interface KimItemLifecyclePayload extends KimRunEnvelope {
  /** Item id. */
  item_id: string;
  /** Item kind (commandExecution | agentMessage | fileChange | reasoning | …). */
  kind: string;
  /** 'started' or 'completed'. */
  phase: string;
  /** Human-readable label (command string, file list, …); may be empty. */
  title: string;
}

/** Codex app-server: a turn started / completed / was interrupted. */
export interface KimTurnLifecyclePayload extends KimRunEnvelope {
  /** 'started' | 'completed' | 'interrupted' | 'failed'. */
  phase: string;
  /** Codex turn id (may be empty). */
  turn_id: string;
}

/** Discriminated union of all typed IPC events. */
export type KimEvent =
  | { event: typeof KimEventNames.STATUS; payload: KimStatusPayload }
  | { event: typeof KimEventNames.PLAN; payload: KimPlanPayload }
  | { event: typeof KimEventNames.STEP; payload: KimStepPayload }
  | { event: typeof KimEventNames.DONE; payload: KimDonePayload }
  | { event: typeof KimEventNames.CONTEXT; payload: KimContextPayload }
  | { event: typeof KimEventNames.STATS; payload: KimStatsPayload }
  | { event: typeof KimEventNames.UI; payload: KimUiPayload }
  | { event: typeof KimEventNames.RUN_DONE; payload: KimRunDonePayload }
  | { event: typeof KimEventNames.RUN_FAILED; payload: KimRunFailedPayload }
  | { event: typeof KimEventNames.PROVIDER_ERROR; payload: KimProviderErrorPayload }
  | { event: typeof KimEventNames.RATE_LIMITED; payload: KimRateLimitedPayload }
  | { event: typeof KimEventNames.HITL_APPROVAL_REQUEST; payload: KimHitlApprovalRequestPayload }
  | { event: typeof KimEventNames.HITL_APPROVAL_RESULT; payload: KimHitlApprovalResultPayload }
  | { event: typeof KimEventNames.TOOL; payload: KimToolPayload }
  | { event: typeof KimEventNames.ANSWER; payload: KimAnswerPayload }
  | { event: typeof KimEventNames.DIFF; payload: KimDiffPayload }
  | { event: typeof KimEventNames.ACTIVITY; payload: KimActivityPayload }
  | { event: typeof KimEventNames.COMMAND_APPROVAL_REQUEST; payload: KimCommandApprovalRequestPayload }
  | { event: typeof KimEventNames.FILE_CHANGE_APPROVAL_REQUEST; payload: KimFileChangeApprovalRequestPayload }
  | { event: typeof KimEventNames.USER_INPUT_REQUEST; payload: KimUserInputRequestPayload }
  | { event: typeof KimEventNames.COMMAND_OUTPUT; payload: KimCommandOutputPayload }
  | { event: typeof KimEventNames.ASSISTANT_DELTA; payload: KimAssistantDeltaPayload }
  | { event: typeof KimEventNames.REASONING_DELTA; payload: KimReasoningDeltaPayload }
  | { event: typeof KimEventNames.PLAN_UPDATE; payload: KimPlanUpdatePayload }
  | { event: typeof KimEventNames.DIFF_UPDATE; payload: KimDiffUpdatePayload }
  | { event: typeof KimEventNames.TOKEN_USAGE; payload: KimTokenUsagePayload }
  | { event: typeof KimEventNames.ITEM_LIFECYCLE; payload: KimItemLifecyclePayload }
  | { event: typeof KimEventNames.TURN_LIFECYCLE; payload: KimTurnLifecyclePayload };

const KimWireEventMap = {
  "status": { event: KimEventNames.STATUS },
  "plan": { event: KimEventNames.PLAN },
  "step": { event: KimEventNames.STEP },
  "done": { event: KimEventNames.DONE },
  "context": { event: KimEventNames.CONTEXT },
  "stats": { event: KimEventNames.STATS },
  "ui_screenshot_flash": { event: KimEventNames.UI, fixedPayload: {"action":"screenshot_flash"} },
  "ui_show": { event: KimEventNames.UI, fixedPayload: {"action":"show"} },
  "run_done": { event: KimEventNames.RUN_DONE },
  "run_failed": { event: KimEventNames.RUN_FAILED },
  "provider_error": { event: KimEventNames.PROVIDER_ERROR },
  "rate_limited": { event: KimEventNames.RATE_LIMITED },
  "hitl_approval_request": { event: KimEventNames.HITL_APPROVAL_REQUEST },
  "hitl_approval_result": { event: KimEventNames.HITL_APPROVAL_RESULT },
  "tool": { event: KimEventNames.TOOL },
  "answer": { event: KimEventNames.ANSWER },
  "diff": { event: KimEventNames.DIFF },
  "activity": { event: KimEventNames.ACTIVITY },
  "command_approval_request": { event: KimEventNames.COMMAND_APPROVAL_REQUEST },
  "file_change_approval_request": { event: KimEventNames.FILE_CHANGE_APPROVAL_REQUEST },
  "user_input_request": { event: KimEventNames.USER_INPUT_REQUEST },
  "command_output": { event: KimEventNames.COMMAND_OUTPUT },
  "assistant_delta": { event: KimEventNames.ASSISTANT_DELTA },
  "reasoning_delta": { event: KimEventNames.REASONING_DELTA },
  "plan_update": { event: KimEventNames.PLAN_UPDATE },
  "diff_update": { event: KimEventNames.DIFF_UPDATE },
  "token_usage": { event: KimEventNames.TOKEN_USAGE },
  "item_lifecycle": { event: KimEventNames.ITEM_LIFECYCLE },
  "turn_lifecycle": { event: KimEventNames.TURN_LIFECYCLE },
} as const;

/** Decode one Python stdout JSON event using the schema-generated wire map. */
export function decodeKimEventLine(raw: string): KimEvent | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const record = parsed as Record<string, unknown>;
    if (typeof record.type !== 'string' || !(record.type in KimWireEventMap)) return null;
    const mapping = KimWireEventMap[record.type as keyof typeof KimWireEventMap];
    const { type: _type, ...payload } = record;
    const fixedPayload = 'fixedPayload' in mapping ? mapping.fixedPayload : {};
    return { event: mapping.event, payload: { ...payload, ...fixedPayload } } as KimEvent;
  } catch {
    return null;
  }
}
