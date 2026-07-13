// useCodexTurn — state for the codex app-server transport's live UX
// (parity Part 5): native approval requests, plan checklist, streaming
// command output, unified diff, codex-side token usage.
//
// Kept OUT of useChatStream deliberately: that hook is at the file-size
// gate, and this state is only alive during code-tab runs on the
// app-server transport.

import { useCallback, useEffect, useRef, useState } from 'react';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { toast } from '../components/Toast';
import {
  KimEventNames,
  type KimCommandApprovalRequestPayload,
  type KimFileChangeApprovalRequestPayload,
  type KimCommandOutputPayload,
  type KimPlanUpdatePayload,
  type KimDiffUpdatePayload,
  type KimTokenUsagePayload,
  type KimTurnLifecyclePayload,
} from '../types/events.gen';

export type CodexApprovalDecision = 'accept' | 'acceptForSession' | 'decline';

export interface PendingCodexApproval {
  id: string;
  kind: 'command' | 'fileChange';
  command: string;
  cwd: string;
  reason: string;
  network: boolean;
  files: Array<{ path?: string; kind?: string }>;
  /** null while pending; set to the chosen decision after responding. */
  resolved: CodexApprovalDecision | null;
}

export interface CodexPlanStep {
  step: string;
  status: string;
}

/** C4: codex asked the USER a question (item/tool/requestUserInput) or an MCP
 *  elicitation, forwarded by Rust as `kim:user-input-request`. Payload matches
 *  the Rust contract verbatim. */
export interface KimUserInputRequestPayload {
  id: string;
  kind: string; // "questions" (codex) | "elicitation" (MCP, already declined)
  item_id: string;
  questions: unknown[];
  message: string;
}

export interface CodexUserInputQuestion {
  id: string;
  header?: string;
  question?: string;
  options?: Array<{ label?: string; description?: string }>;
  isOther?: boolean;
  isSecret?: boolean;
}

export interface PendingUserInput {
  id: string;
  kind: string;
  message: string;
  questions: CodexUserInputQuestion[];
  /** true once answered/dismissed so the card locks and shows a record. */
  resolved: boolean;
}

function toUserInputQuestions(raw: unknown[]): CodexUserInputQuestion[] {
  const out: CodexUserInputQuestion[] = [];
  for (let i = 0; i < raw.length; i++) {
    const q = raw[i];
    if (!q || typeof q !== 'object') continue;
    const rec = q as Record<string, unknown>;
    const options = Array.isArray(rec.options)
      ? (rec.options.filter(o => o && typeof o === 'object') as Array<{ label?: string; description?: string }>)
      : undefined;
    out.push({
      // codex questions carry an id; fall back to the index so answers still key.
      id: typeof rec.id === 'string' && rec.id ? rec.id : String(i),
      header: typeof rec.header === 'string' ? rec.header : undefined,
      question: typeof rec.question === 'string' ? rec.question : undefined,
      options,
      isOther: Boolean(rec.isOther),
      isSecret: Boolean(rec.isSecret),
    });
  }
  return out;
}

export interface CodexTurnState {
  approval: PendingCodexApproval | null;
  plan: CodexPlanStep[];
  /** Rolling command output for the current turn (display-capped). */
  commandOutput: string;
  diff: string;
  tokenUsage: { input: number; output: number; total: number } | null;
  turnPhase: string | null;
  respond: (decision: CodexApprovalDecision) => void;
  /** C4: codex's pending question to the user (null when none). */
  userInput: PendingUserInput | null;
  /** C4: answer a codex question. `answers` is forwarded verbatim to the
   *  `respond_user_input` Tauri command (object map, list, or bare string). */
  respondUserInput: (answers: unknown) => void;
  /** C4: dismiss without answering (codex proceeds with no answer). */
  dismissUserInput: () => void;
}

const MAX_OUTPUT_CHARS = 20_000;

function toPlanSteps(steps: unknown[]): CodexPlanStep[] {
  const out: CodexPlanStep[] = [];
  for (const raw of steps) {
    if (!raw || typeof raw !== 'object') continue;
    const rec = raw as Record<string, unknown>;
    const step = typeof rec.step === 'string' ? rec.step : typeof rec.text === 'string' ? rec.text : '';
    if (!step) continue;
    out.push({ step, status: typeof rec.status === 'string' ? rec.status : 'pending' });
  }
  return out;
}

export function useCodexTurn(): CodexTurnState {
  const [approval, setApproval] = useState<PendingCodexApproval | null>(null);
  const [plan, setPlan] = useState<CodexPlanStep[]>([]);
  const [commandOutput, setCommandOutput] = useState('');
  const [diff, setDiff] = useState('');
  const [tokenUsage, setTokenUsage] = useState<CodexTurnState['tokenUsage']>(null);
  const [turnPhase, setTurnPhase] = useState<string | null>(null);
  const [userInput, setUserInput] = useState<PendingUserInput | null>(null);
  const approvalRef = useRef<PendingCodexApproval | null>(null);
  approvalRef.current = approval;
  const userInputRef = useRef<PendingUserInput | null>(null);
  userInputRef.current = userInput;

  useEffect(() => {
    const unlisteners: Array<Promise<UnlistenFn>> = [
      listen<KimCommandApprovalRequestPayload>(KimEventNames.COMMAND_APPROVAL_REQUEST, e => {
        setApproval({
          id: e.payload.id,
          kind: 'command',
          command: e.payload.command,
          cwd: e.payload.cwd,
          reason: e.payload.reason ?? '',
          network: Boolean(e.payload.network),
          files: [],
          resolved: null,
        });
      }),
      listen<KimFileChangeApprovalRequestPayload>(KimEventNames.FILE_CHANGE_APPROVAL_REQUEST, e => {
        const files = Array.isArray(e.payload.files)
          ? (e.payload.files.filter(f => f && typeof f === 'object') as Array<{ path?: string; kind?: string }>)
          : [];
        setApproval({
          id: e.payload.id,
          kind: 'fileChange',
          command: files.map(f => f.path).filter(Boolean).join(', '),
          cwd: '',
          reason: e.payload.reason ?? '',
          network: false,
          files,
          resolved: null,
        });
      }),
      listen<KimCommandOutputPayload>(KimEventNames.COMMAND_OUTPUT, e => {
        setCommandOutput(prev => {
          const next = prev + e.payload.chunk;
          return next.length > MAX_OUTPUT_CHARS ? next.slice(next.length - MAX_OUTPUT_CHARS) : next;
        });
      }),
      listen<KimPlanUpdatePayload>(KimEventNames.PLAN_UPDATE, e => {
        setPlan(toPlanSteps(e.payload.steps ?? []));
      }),
      listen<KimDiffUpdatePayload>(KimEventNames.DIFF_UPDATE, e => {
        setDiff(e.payload.unified_diff ?? '');
      }),
      listen<KimTokenUsagePayload>(KimEventNames.TOKEN_USAGE, e => {
        setTokenUsage({
          input: e.payload.input ?? 0,
          output: e.payload.output ?? 0,
          total: e.payload.total ?? 0,
        });
      }),
      // C4: codex is asking the USER a question (or an MCP elicitation). Render
      // it as a question card instead of the previous silent auto-decline.
      listen<KimUserInputRequestPayload>('kim:user-input-request', e => {
        setUserInput({
          id: e.payload.id,
          kind: e.payload.kind,
          message: e.payload.message ?? '',
          questions: toUserInputQuestions(Array.isArray(e.payload.questions) ? e.payload.questions : []),
          resolved: false,
        });
      }),
      listen<KimTurnLifecyclePayload>(KimEventNames.TURN_LIFECYCLE, e => {
        setTurnPhase(e.payload.phase);
        if (e.payload.phase === 'started') {
          // Fresh turn: clear the previous turn's transient stream state.
          setApproval(null);
          setPlan([]);
          setCommandOutput('');
          setDiff('');
          setUserInput(null);
        } else {
          // M6: terminal phases (completed / failed / interrupted) — a still-
          // pending approval belongs to a dead turn; clear it so the card's
          // buttons don't target a dead approval id. Resolved cards stay
          // visible as a record of the decision.
          setApproval(prev => (prev && prev.resolved === null ? null : prev));
          setUserInput(prev => (prev && !prev.resolved ? null : prev));
        }
      }),
      // M6: the legacy run lifecycle can end a run without a turn/completed
      // notification (cancel, agent exit). Clear a pending approval then too.
      listen('kim-agent-done', () => {
        setApproval(prev => (prev && prev.resolved === null ? null : prev));
        setUserInput(prev => (prev && !prev.resolved ? null : prev));
      }),
      listen('kim-agent-cancelled', () => {
        setApproval(prev => (prev && prev.resolved === null ? null : prev));
        setUserInput(prev => (prev && !prev.resolved ? null : prev));
      }),
    ];
    return () => {
      unlisteners.forEach(p => {
        p.then(un => un()).catch(() => undefined);
      });
    };
  }, []);

  const respond = useCallback((decision: CodexApprovalDecision) => {
    const current = approvalRef.current;
    if (!current || current.resolved) return;
    setApproval({ ...current, resolved: decision });
    void invoke('respond_approval_decision', { id: current.id, decision }).catch(() => {
      // M5: the optimistic "Approved/Denied" was a lie on failure — codex never
      // got the decision, the turn hung, and the resolved-guard blocked
      // re-answering. Re-open the card and tell the user.
      setApproval(prev =>
        prev && prev.id === current.id ? { ...prev, resolved: null } : prev
      );
      toast('Could not send the approval decision — try again.', 'error', 4000);
    });
  }, []);

  const respondUserInput = useCallback((answers: unknown) => {
    const current = userInputRef.current;
    if (!current || current.resolved) return;
    // Elicitation requests were already declined server-side (Kim can't fill
    // MCP forms yet) — nothing to send back, just dismiss the informational card.
    if (current.kind === 'elicitation') {
      setUserInput({ ...current, resolved: true });
      return;
    }
    setUserInput({ ...current, resolved: true });
    void invoke('respond_user_input', { id: current.id, answers }).catch(() => {
      // The answer never reached codex — re-open so the user can retry.
      setUserInput(prev => (prev && prev.id === current.id ? { ...prev, resolved: false } : prev));
      toast('Could not send your answer to Codex — try again.', 'error', 4000);
    });
  }, []);

  const dismissUserInput = useCallback(() => {
    setUserInput(prev => (prev ? { ...prev, resolved: true } : prev));
  }, []);

  return {
    approval,
    plan,
    commandOutput,
    diff,
    tokenUsage,
    turnPhase,
    respond,
    userInput,
    respondUserInput,
    dismissUserInput,
  };
}
