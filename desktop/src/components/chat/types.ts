/**
 * Shared TypeScript interfaces for ChatView and its helpers.
 *
 * Extracted from ChatView.tsx (Phase 3 restructure).
 * No JSX, no hooks, no side-effects.
 */

import type { KimMessage } from '../../types';

/** A single entry in the live activity feed rendered below the main chat. */
export interface ActivityItem {
  id: number;
  kind: 'tool' | 'info' | 'error' | 'success' | 'cancelled' | 'status';
  icon: string;
  text: string;
}

/**
 * Parsed live plan extracted from streamed status lines.
 *
 * Preferred path: structured `[PLAN]{json}` / `[STEP]{json}` envelopes.
 * Fallback path: heuristic detection of numbered bullet plans.
 */
export interface LivePlanParsed {
  steps: string[];
  /** 1-based index of the step currently in progress (0 = no step started yet,
   *  but the plan is known). Derived from the most recent `STEP n:` marker.
   *  Steps with index < activeStep are complete; step at activeStep is in
   *  flight; steps > activeStep are pending. */
  activeStep: number;
  /** 1-based indexes explicitly marked complete by `[DONE]{json}`. */
  doneSteps: number[];
  /** True when the plan came from the new structured `[PLAN]{json}` event
   *  emitted by the agent (vs the older heuristic phrase-detection). When
   *  structured, we trust the step count exactly; otherwise we treat it as
   *  best-effort. */
  structured: boolean;
}

/** A source file touched (added/removed lines) during a Codex session run. */
export interface TouchedFile {
  path: string;
  added: number;
  removed: number;
}

/** All messages belonging to one user-initiated Codex session run. */
export interface CodexRunGroup {
  userMessage: KimMessage;
  intermediateMessages: KimMessage[];
  intermediateActivity: ActivityItem[];
  finalAssistantMessage: KimMessage | null;
  touchedFiles: TouchedFile[];
  durationSec: number;
}

/** A file the user has attached to the current task input. */
export interface AttachedFile {
  /** L10: stable identity for React keys — index keys mis-reconciled the chip
   *  list when an attachment was removed from the middle. */
  id?: string;
  name: string;
  kind: 'text' | 'image' | 'binary';
  content?: string;   // text files: raw text; binary: unused
  savedPath?: string; // images: temp path on disk
  previewUrl?: string; // images: object URL for thumbnail
  sizeLabel: string;
}

/** An in-flight or queued task shown in the task input area. */
export interface PendingTask {
  id: number;
  text: string;
  provider: string;
}

/** Live provider token / performance counters shown in the status bar. */
export interface ProviderUsageState {
  provider: string;
  model?: string;
  mode?: string;
  input?: number;
  output?: number;
  total?: number;
  usage_available?: boolean;
  tokens_per_second?: number;
  context_limit?: number;
  context_limit_source?: string;
  billing?: string;
  total_duration?: number;
  load_duration?: number;
  prompt_eval_duration?: number;
  eval_duration?: number;
}

/** Last high-risk approval event surfaced by the typed desktop stream. */
export interface HitlApprovalStatus {
  tool: string;
  risk: string;
  reason: string;
  approved: boolean | null;
  /** K6: optional preview (command / unified diff / URL+label). */
  preview?: string;
  /** T1: correlation id — echoed back on the decision so Python voids stale approvals. */
  id?: string;
  /** A3/B10: true once the user clicked a decision and it was handed to the
   *  backend. Disables the card's buttons so a second click can't buffer a
   *  stdin decision line that the NEXT approval gate would consume. */
  decisionSent?: boolean;
}
