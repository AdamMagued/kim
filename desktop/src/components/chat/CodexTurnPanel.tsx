// CodexTurnPanel — the code-tab live UX for the codex app-server transport
// (parity Part 5): native ApprovalCard (Allow once / Always this session /
// Deny), PlanChecklist, collapsible CommandOutputBlock, expandable
// DiffSummary. Rendered by StreamRenderer whenever a codex turn produced
// any of this state.

import { useEffect, useRef, useState } from 'react';
import type { CodexTurnState, CodexApprovalDecision, PendingCodexApproval, CodexPlanStep, PendingUserInput } from '../../hooks/useCodexTurn';
import type { HitlApprovalStatus } from './types';

// ── A1: honest approval-card copy ────────────────────────────────────────────
// The backend preview (mcp_server/policy.py build_approval_preview) returns ""
// for most gated OS-control tools, so the card must at least say what the tool
// WILL DO instead of leaving the user to approve keystroke/mouse injection
// blind. Payloads carry no tool args, so the exact text/keys/target can only
// come from a richer backend preview (flagged for the mcp_server owner).

/** Plain-language description of what approving a given tool allows. */
export const TOOL_ACTION_DESCRIPTIONS: Record<string, string> = {
  type_text: 'Types text into whatever window/field currently has focus on your computer',
  key_press: 'Presses a keyboard key on your computer',
  press_key: 'Presses a keyboard key on your computer',
  hotkey: 'Presses a keyboard shortcut on your computer',
  click: 'Clicks the mouse at a screen position',
  double_click: 'Double-clicks the mouse at a screen position',
  right_click: 'Right-clicks the mouse at a screen position',
  click_ui: 'Clicks a UI element in the focused application',
  scroll: 'Scrolls the focused window',
  drag: 'Drags the mouse across the screen',
  move_mouse: 'Moves the mouse pointer',
  git_add: 'Stages files in the git repository',
  git_commit: 'Creates a git commit',
  write_memory: "Writes to Kim's persistent memory",
  focus_window: 'Switches focus to another window',
  open_application: 'Opens an application',
  close_application: 'Closes an application',
  write_clipboard: 'Replaces your system clipboard contents',
  run_command: 'Runs a shell command on your computer',
  delete_file: 'Deletes a file',
};

/** Human-readable phrasing for machine reason codes (e.g. `input_injection`). */
export const APPROVAL_REASON_LABELS: Record<string, string> = {
  input_injection: 'it injects keyboard/mouse input',
  arbitrary_code_execution: 'it can run arbitrary code',
  file_write: 'it writes to your files',
  file_delete: 'it deletes files',
  network_access: 'it accesses the network',
  clipboard_access: 'it accesses your clipboard',
  approval_result: 'approval decision',
};

export function approvalReasonLabel(reason: string): string {
  if (!reason) return '';
  return APPROVAL_REASON_LABELS[reason] ?? reason.replace(/_/g, ' ');
}

/** The legacy task-level HITL card (moved verbatim from StreamRenderer so all
 * approval UI lives in one module). */
export function HitlStatusCard({
  status,
  onRespond,
}: {
  status: HitlApprovalStatus;
  // M3: full decision vocabulary — hitlRespond(approved, decision?) supports
  // acceptForSession, but the old narrowed type made it unreachable here.
  onRespond?: (approved: boolean, decision?: 'accept' | 'acceptForSession' | 'decline') => void;
}) {
  const isPending = status.approved === null;
  const decisionSent = Boolean(status.decisionSent);
  const stateLabel = isPending
    ? decisionSent ? 'Sending decision…' : 'Approval required'
    : status.approved ? 'Approved' : 'Denied';
  // A1: say the ACTUAL risk level — the old copy claimed "high-risk" even for
  // medium-risk gates.
  const riskWord = status.risk === 'high' ? 'high-risk' : status.risk === 'medium' ? 'medium-risk' : `${status.risk}-risk`;
  const detail = isPending
    ? `Kim paused before a ${riskWord} action.`
    : status.approved
      ? 'Kim can continue with the approved action.'
      : 'Kim will choose another approach or ask for help.';
  const reasonText = approvalReasonLabel(status.reason);
  const actionDescription = TOOL_ACTION_DESCRIPTIONS[status.tool];
  return (
    <div
      className={`kim-hitl-status${status.approved === false ? ' kim-hitl-status--denied' : ''}`}
      role="status"
      aria-live="polite"
    >
      <span className="kim-hitl-status__label">{stateLabel}</span>
      <span className="kim-hitl-status__body">
        {detail} Tool: <strong>{status.tool}</strong>
        {reasonText ? <> — {reasonText}</> : null}.
      </span>
      {status.preview ? (
        <pre className="kim-hitl-status__preview"><code>{status.preview}</code></pre>
      ) : actionDescription ? (
        // A1: no backend preview — at minimum state what the tool does and be
        // honest that the exact input (text/keys/target) is not shown.
        <span className="kim-hitl-status__body" data-testid="hitl-action-description">
          {actionDescription}. The exact input isn&apos;t included in this preview, so
          approve only if you expected Kim to do this right now.
        </span>
      ) : null}
      {isPending && onRespond && (
        <span className="kim-hitl-status__actions">
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onRespond(true, 'accept')}
            disabled={decisionSent}
            aria-label="Approve tool execution"
          >
            Approve
          </button>
          {/* A2: honest scope — the HITL gate lives in a per-task MCP server
              process, so "always" only covers the CURRENT task, and it covers
              every use of this tool within it. Label + tooltip say exactly that. */}
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onRespond(true, 'acceptForSession')}
            disabled={decisionSent}
            aria-label="Always allow for this task"
            title={`Approves every ${status.tool} action until the current task finishes. Kim will ask again on the next message.`}
          >
            Always for this task
          </button>
          <button
            className="kim-hitl-btn kim-hitl-btn--deny"
            onClick={() => onRespond(false, 'decline')}
            disabled={decisionSent}
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
        ? 'Approved for this task'
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
          {/* A2/H2: honest scope — the codex app-server process is restarted
              per message, so its session-approval cache only survives the
              CURRENT task. Don't promise "this session". */}
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={() => onDecision('acceptForSession')}
            aria-label="Always allow for this task"
            title="Approves similar actions until the current task finishes. Codex will ask again on the next message."
          >
            Always for this task
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

/** M2/A2: codex-side token usage — plumbed end-to-end but previously rendered
 *  nowhere. Shown as a small monospace chip inside the turn panel. */
export function TokenUsageChip({ usage }: { usage: CodexTurnState['tokenUsage'] }) {
  if (!usage || (usage.input === 0 && usage.output === 0 && usage.total === 0)) return null;
  return (
    <span
      className="kim-codex-token-usage"
      data-testid="codex-token-usage"
      title="Token usage reported by codex for this conversation"
      style={{
        fontFamily: 'var(--kim-mono, ui-monospace, monospace)',
        fontSize: 11.5,
        color: 'var(--kim-text-3)',
        border: '1px solid var(--kim-border)',
        borderRadius: 6,
        padding: '2px 7px',
        alignSelf: 'flex-start',
      }}
    >
      tokens: {usage.input.toLocaleString()} in · {usage.output.toLocaleString()} out
      {usage.total > 0 ? ` · ${usage.total.toLocaleString()} total` : ''}
    </span>
  );
}

/** C4: render codex's question to the user (item/tool/requestUserInput) as an
 *  answerable card. Previously these were silently auto-declined so codex
 *  guessed and the user never saw the question. */
export function QuestionCard({
  request,
  onAnswer,
  onDismiss,
}: {
  request: PendingUserInput;
  onAnswer: (answers: unknown) => void;
  onDismiss: () => void;
}) {
  // answers: questionId → selected label (or typed free-text).
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const resolved = request.resolved;
  const isElicitation = request.kind === 'elicitation';

  // MCP elicitation is already declined server-side — render as an honest,
  // non-answerable notice rather than pretending Kim can fill the form.
  if (isElicitation) {
    return (
      <div className="kim-hitl-status" role="status" aria-live="polite" data-testid="codex-question-card">
        <span className="kim-hitl-status__label">An MCP server asked for input</span>
        <span className="kim-hitl-status__body">
          {request.message
            ? request.message
            : 'A tool codex is using requested input.'}{' '}
          Kim cannot fill MCP elicitation forms yet, so it was declined and codex
          continued without it.
        </span>
      </div>
    );
  }

  const submit = () => {
    // Build the object map codex expects: { [id]: { answers: [label] } }.
    // parse_user_input_line accepts bare strings / lists / this object shape.
    const payload: Record<string, { answers: string[] }> = {};
    for (const q of request.questions) {
      const val = answers[q.id];
      if (val !== undefined && val !== '') payload[q.id] = { answers: [val] };
    }
    onAnswer(payload);
  };

  const setAns = (qid: string, value: string) =>
    setAnswers(prev => ({ ...prev, [qid]: value }));

  const hasAnyAnswer = Object.values(answers).some(v => v !== '');

  return (
    <div className="kim-hitl-status" role="status" aria-live="polite" data-testid="codex-question-card">
      <span className="kim-hitl-status__label">
        {resolved ? 'Answer sent to Codex' : 'Codex needs your input'}
      </span>
      {request.questions.length === 0 && (
        <span className="kim-hitl-status__body">
          {request.message || 'Codex asked a question but sent no details.'}
        </span>
      )}
      {request.questions.map(q => {
        const label = [q.header, q.question].filter(Boolean).join(' — ') || 'Question';
        const options = q.options ?? [];
        return (
          <div key={q.id} className="kim-codex-question">
            <span className="kim-hitl-status__body"><strong>{label}</strong></span>
            {options.length > 0 && !resolved && (
              <span className="kim-hitl-status__actions">
                {options.map((o, oi) => {
                  const optLabel = o.label ?? `Option ${oi + 1}`;
                  const picked = answers[q.id] === optLabel;
                  return (
                    <button
                      key={oi}
                      className={`kim-hitl-btn kim-hitl-btn--approve${picked ? ' kim-hitl-btn--active' : ''}`}
                      onClick={() => setAns(q.id, optLabel)}
                      aria-pressed={picked}
                      title={o.description || undefined}
                    >
                      {optLabel}
                    </button>
                  );
                })}
              </span>
            )}
            {/* Free-text answer: always available for `isOther`/no-option
                questions; codex accepts a typed reply as the answer. */}
            {(options.length === 0 || q.isOther) && !resolved && (
              <input
                className="kim-codex-question__input"
                type={q.isSecret ? 'password' : 'text'}
                placeholder="Type your answer…"
                value={answers[q.id] ?? ''}
                onChange={e => setAns(q.id, e.target.value)}
                aria-label={`Answer: ${label}`}
              />
            )}
            {resolved && (
              <span className="kim-hitl-status__body" style={{ opacity: 0.75 }}>
                {answers[q.id] ? `You answered: ${q.isSecret ? '••••' : answers[q.id]}` : 'No answer given.'}
              </span>
            )}
          </div>
        );
      })}
      {!resolved && (
        <span className="kim-hitl-status__actions">
          <button
            className="kim-hitl-btn kim-hitl-btn--approve"
            onClick={submit}
            disabled={!hasAnyAnswer}
            aria-label="Send answer to Codex"
          >
            Send answer
          </button>
          <button
            className="kim-hitl-btn kim-hitl-btn--deny"
            onClick={onDismiss}
            aria-label="Skip the question"
            title="Codex proceeds without an answer."
          >
            Skip
          </button>
        </span>
      )}
    </div>
  );
}

/** Everything a codex app-server turn streams, in one feed block. */
export function CodexTurnPanel({ turn }: { turn: CodexTurnState }) {
  const hasContent =
    turn.approval || turn.plan.length > 0 || turn.commandOutput || turn.diff.trim() || turn.tokenUsage || turn.userInput;
  if (!hasContent) return null;
  return (
    <div className="kim-msg-row kim-msg-row--assistant kim-codex-turn" data-testid="codex-turn-panel">
      <PlanChecklist steps={turn.plan} />
      <CommandOutputBlock output={turn.commandOutput} />
      {turn.approval && <ApprovalCard approval={turn.approval} onDecision={turn.respond} />}
      {turn.userInput && (
        <QuestionCard
          request={turn.userInput}
          onAnswer={turn.respondUserInput}
          onDismiss={turn.dismissUserInput}
        />
      )}
      <DiffSummary diff={turn.diff} />
      <TokenUsageChip usage={turn.tokenUsage} />
    </div>
  );
}
