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

export interface CodexTurnState {
  approval: PendingCodexApproval | null;
  plan: CodexPlanStep[];
  /** Rolling command output for the current turn (display-capped). */
  commandOutput: string;
  diff: string;
  tokenUsage: { input: number; output: number; total: number } | null;
  turnPhase: string | null;
  respond: (decision: CodexApprovalDecision) => void;
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
  const approvalRef = useRef<PendingCodexApproval | null>(null);
  approvalRef.current = approval;

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
      listen<KimTurnLifecyclePayload>(KimEventNames.TURN_LIFECYCLE, e => {
        setTurnPhase(e.payload.phase);
        if (e.payload.phase === 'started') {
          // Fresh turn: clear the previous turn's transient stream state.
          setApproval(null);
          setPlan([]);
          setCommandOutput('');
          setDiff('');
        }
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
    void invoke('respond_approval_decision', { id: current.id, decision }).catch(() => undefined);
  }, []);

  return { approval, plan, commandOutput, diff, tokenUsage, turnPhase, respond };
}
