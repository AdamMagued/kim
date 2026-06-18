// events.gen.ts — DO NOT HAND-EDIT
// Generated from desktop/src/types/events.schema.json via `npm run gen:events`.
// To add/change an event: edit events.schema.json, then re-run the script.

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
} as const;

export type KimEventName = (typeof KimEventNames)[keyof typeof KimEventNames];

/** A human-readable status message from the agent loop (activity feed item). */
export interface KimStatusPayload {
  /** Status text to display in activity feed. */
  message: string;
}

/** Agent emitted a structured plan with step list. */
export interface KimPlanPayload {
  /** Array of plan step objects. */
  steps: unknown[];
}

/** Agent advanced to a new plan step. */
export interface KimStepPayload {
  /** 1-based step index. */
  n: number;
  /** Step metadata. */
  data: Record<string, unknown>;
}

/** Agent completed a plan step. */
export interface KimDonePayload {
  /** 1-based step index that finished. */
  n: number;
}

/** Context window usage snapshot. */
export interface KimContextPayload {
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
export interface KimStatsPayload {
  /** Cumulative input tokens. */
  input: number;
  /** Cumulative output tokens. */
  output: number;
  /** input + output. */
  total: number;
}

/** Window/UI control signal from the agent. */
export interface KimUiPayload {
  /** 'screenshot_flash' triggers flash overlay; 'show' brings main window to front. */
  action: 'screenshot_flash' | 'show';
}

/** Run completed (success or expected failure). */
export interface KimRunDonePayload {
  /** Termination reason key: task_complete | max_iterations | stuck | provider_failed | need_help | cancelled | conversational_loop. */
  termination: string;
  /** True only when termination === 'task_complete'. */
  success: boolean;
}

/** Run ended due to a recoverable or unrecoverable error. */
export interface KimRunFailedPayload {
  /** Machine-readable error key (e.g. 'provider_auth', 'rate_limit'). */
  reason: string;
  /** True when the user can retry without config changes. */
  recoverable: boolean;
  /** Human-readable next-action hint. */
  suggestion: string;
}

/** Provider call failed with a specific error code. */
export interface KimProviderErrorPayload {
  /** Error code: auth | rate_limit | server_error | timeout | network | invalid_request. */
  code: string;
  /** True when automatic retry is possible. */
  retryable: boolean;
}

/** Provider rate-limited; agent will retry after a delay. */
export interface KimRateLimitedPayload {
  /** Seconds until next retry. */
  delay: number;
  /** Current retry attempt number (1-based). */
  attempt: number;
  /** Maximum retries configured. */
  max_retries: number;
}

/** Agent is requesting human approval before executing a high-risk tool. */
export interface KimHitlApprovalRequestPayload {
  /** MCP tool name requiring approval. */
  tool: string;
  /** Risk level: high | medium. Determines UI prominence. */
  risk: string;
  /** Machine-readable reason key (e.g. 'arbitrary_code_execution'). */
  reason: string;
  /** K6: human-readable preview — command string, unified diff (<=40 lines), or URL + element label. May be empty. */
  preview: string;
}

/** Human approval decision sent back to the agent. */
export interface KimHitlApprovalResultPayload {
  /** MCP tool name that was approved or denied. */
  tool: string;
  /** True if the user approved the tool call. */
  approved: boolean;
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
  | { event: typeof KimEventNames.HITL_APPROVAL_RESULT; payload: KimHitlApprovalResultPayload };
