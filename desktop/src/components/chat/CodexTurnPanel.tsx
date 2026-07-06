// CodexTurnPanel — the code-tab live UX for the codex app-server transport
// (parity Part 5): native ApprovalCard (Allow once / Always this session /
// Deny), PlanChecklist, collapsible CommandOutputBlock, expandable
// DiffSummary. Rendered by StreamRenderer whenever a codex turn produced
// any of this state.

import { useEffect, useRef, useState } from 'react';
import type { CodexTurnState, CodexApprovalDecision, PendingCodexApproval, CodexPlanStep } from '../../hooks/useCodexTurn';
import type { HitlApprovalStatus } from './types';

/** The legacy task-level HITL card (moved verbatim from StreamRenderer so all
 * approval UI lives in one module). */
export function HitlStatusCard({
  status,
  onRespond,
}: {
  status: HitlApprovalStatus;
  onRespond?: (approved: boolean) => void;
}) {
  const isPending = status.approved === null;
  const stateLabel = isPending ? 'Approval required' : status.approved ? 'Approved' : 'Denied';
  const detail = isPending
    ? 'Kim paused before a high-risk action.'
    : status.approved
      ? 'Kim can continue with the approved action.'
      : 'Kim will choose another approach or ask for help.';
  return (
    <div
      className={`kim-hitl-status${status.approved === false ? ' kim-hitl-status--denied' : ''}`}
      role="status"
      aria-live="polite"
    >
      <span className="kim-hitl-status__label">{stateLabel}</span>
      <span className="kim-hitl-status__body">
        {detail} Tool: <strong>{status.tool}</strong>. Risk: {status.risk} ({status.reason}).
      </span>
      {status.preview && (
        <pre className="kim-hitl-status__preview"><code>{status.preview}</code></pre>
      )}
      {isPending && onRespond && (
        <span className="kim-hitl-status__actions">
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onRespond(true)}
            aria-label="Approve tool execution"
          >
            Approve
          </button>
          <button
            className="kim-hitl-btn kim-hitl-btn--deny"
            onClick={() => onRespond(false)}
            aria-label="Deny tool execution"
          >
            Deny
          </button>
        </span>
      )}
    </div>
  );
}

export function ApprovalCard({
  approval,
  onDecision,
}: {
  approval: PendingCodexApproval;
  onDecision: (decision: CodexApprovalDecision) => void;
}) {
  const pending = approval.resolved === null;
  const verdict =
    approval.resolved === 'accept'
      ? 'Approved once'
      : approval.resolved === 'acceptForSession'
        ? 'Approved for this session'
        : approval.resolved === 'decline'
          ? 'Denied'
          : null;
  return (
    <div
      className={`kim-hitl-status${approval.resolved === 'decline' ? ' kim-hitl-status--denied' : ''}`}
      role="status"
      aria-live="polite"
      data-testid="codex-approval-card"
    >
      <span className="kim-hitl-status__label">
        {pending
          ? approval.kind === 'fileChange'
            ? 'Codex wants to change files'
            : 'Codex wants to run a command'
          : verdict}
      </span>
      <span className="kim-hitl-status__body">
        {approval.kind === 'fileChange' ? (
          <>Files: <strong>{approval.command || '(patch)'}</strong></>
        ) : (
          <>
            <code className="kim-codex-approval__cmd">$ {approval.command}</code>
            {approval.cwd ? <span className="kim-codex-approval__cwd"> in {approval.cwd}</span> : null}
          </>
        )}
        {approval.reason ? <> — {approval.reason}</> : null}
        {approval.network ? <span className="kim-codex-approval__badge"> network access</span> : null}
      </span>
      {pending && (
        <span className="kim-hitl-status__actions">
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onDecision('accept')}
            aria-label="Allow once"
          >
            Allow once
          </button>
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onDecision('acceptForSession')}
            aria-label="Always allow this session"
          >
            Always this session
          </button>
          <button
            className="kim-hitl-btn kim-hitl-btn--deny"
            onClick={() => onDecision('decline')}
            aria-label="Deny"
          >
            Deny
          </button>
        </span>
      )}
    </div>
  );
}

export function PlanChecklist({ steps }: { steps: CodexPlanStep[] }) {
  if (steps.length === 0) return null;
  return (
    <ul className="kim-codex-plan" data-testid="codex-plan">
      {steps.map((s, i) => {
        const mark = s.status === 'completed' ? '✓' : s.status === 'inProgress' || s.status === 'in_progress' ? '▸' : '○';
        return (
          <li key={i} className={`kim-codex-plan__step kim-codex-plan__step--${s.status}`}>
            <span className="kim-codex-plan__mark">{mark}</span> {s.step}
          </li>
        );
      })}
    </ul>
  );
}

export function CommandOutputBlock({ output }: { output: string }) {
  const [collapsed, setCollapsed] = useState(false);
  const preRef = useRef<HTMLPreElement | null>(null);
  useEffect(() => {
    // Auto-scroll to the newest output while streaming.
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [output]);
  if (!output) return null;
  return (
    <div className="kim-codex-output" data-testid="codex-command-output">
      <button
        className="kim-codex-output__toggle"
        onClick={() => setCollapsed(c => !c)}
        aria-expanded={!collapsed}
      >
        {collapsed ? '▸ command output' : '▾ command output'}
      </button>
      {!collapsed && (
        <pre ref={preRef} className="kim-codex-output__pre">
          <code>{output}</code>
        </pre>
      )}
    </div>
  );
}

export function diffSummary(diff: string): string {
  const files = diff.split('\n').filter(l => l.startsWith('+++ ')).length;
  const added = diff.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).length;
  const removed = diff.split('\n').filter(l => l.startsWith('-') && !l.startsWith('---')).length;
  return `diff: ${files} file(s), +${added} −${removed}`;
}

export function DiffSummary({ diff }: { diff: string }) {
  const [expanded, setExpanded] = useState(false);
  if (!diff.trim()) return null;
  return (
    <div className="kim-codex-diff" data-testid="codex-diff">
      <button
        className="kim-codex-diff__toggle"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
      >
        {expanded ? '▾ ' : '▸ '}
        {diffSummary(diff)}
      </button>
      {expanded && (
        <pre className="kim-codex-diff__pre">
          <code>{diff}</code>
        </pre>
      )}
    </div>
  );
}

/** Everything a codex app-server turn streams, in one feed block. */
export function CodexTurnPanel({ turn }: { turn: CodexTurnState }) {
  const hasContent = turn.approval || turn.plan.length > 0 || turn.commandOutput || turn.diff.trim();
  if (!hasContent) return null;
  return (
    <div className="kim-msg-row kim-msg-row--assistant kim-codex-turn" data-testid="codex-turn-panel">
      <PlanChecklist steps={turn.plan} />
      <CommandOutputBlock output={turn.commandOutput} />
      {turn.approval && <ApprovalCard approval={turn.approval} onDecision={turn.respond} />}
      <DiffSummary diff={turn.diff} />
    </div>
  );
}
