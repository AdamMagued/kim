import { useState } from 'react';
import type { ToolUseBlock, ToolResultBlock } from '../types';

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`kim-tool-card__chevron${open ? ' kim-tool-card__chevron--open' : ''}`}
      viewBox="0 0 16 16"
      width="11"
      height="11"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  );
}

function WrenchIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.5 2a3.5 3.5 0 0 0-3.3 4.7L2 11.9 4.1 14l5.2-5.2A3.5 3.5 0 1 0 10.5 2z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8.5l3 3 7-7" />
    </svg>
  );
}

// ── Tool-use card ─────────────────────────────────────────────────────────────

interface ToolUseCardProps {
  block: ToolUseBlock;
  result?: string;
}

export function ToolUseCard({ block, result }: ToolUseCardProps) {
  const [open, setOpen] = useState(false);
  const argsStr = JSON.stringify(block.input, null, 2);

  let displayName = block.name;
  if (block.name === 'run_command') {
    displayName = 'Ran a command';
  }

  return (
    <div className="kim-tool-card">
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className="kim-tool-card__icon" aria-hidden>
          <WrenchIcon />
        </span>
        <span className="kim-tool-card__name">{displayName}</span>
        <Chevron open={open} />
      </button>

      {open && (
        <div className="kim-tool-card__body">
          <div style={{ marginBottom: 10 }}>
            <div className="kim-tool-card__section-label">Input</div>
            <pre className="kim-tool-card__pre">{argsStr}</pre>
          </div>

          {result !== undefined && (
            <div>
              <div className="kim-tool-card__section-label">Result</div>
              <pre className="kim-tool-card__pre">{result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Tool-result card ──────────────────────────────────────────────────────────

interface ToolResultCardProps {
  block: ToolResultBlock;
}

export function ToolResultCard({ block }: ToolResultCardProps) {
  const [open, setOpen] = useState(false);
  const content =
    typeof block.content === 'string' ? block.content : JSON.stringify(block.content, null, 2);

  return (
    <div className="kim-tool-card">
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className="kim-tool-card__icon kim-tool-card__icon--result" aria-hidden>
          <CheckIcon />
        </span>
        <span className="kim-tool-card__name kim-tool-card__name--neutral">Tool result</span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="kim-tool-card__body">
          <pre className="kim-tool-card__pre">{content}</pre>
        </div>
      )}
    </div>
  );
}

// ── Signal card (for NEED_HELP and TASK_COMPLETE) ─────────────────────────────

interface SignalCardProps {
  kind: 'success' | 'error';
  text: string;
  onAction?: () => void;
  actionLabel?: string;
}

export function SignalCard({ kind, text, onAction, actionLabel }: SignalCardProps) {
  const [open, setOpen] = useState(false);
  const isError = kind === 'error';

  return (
    <div className={`kim-tool-card ${isError ? 'kim-tool-card--error' : ''}`}>
      <button onClick={() => setOpen(o => !o)} className="kim-tool-card__header">
        <span className={`kim-tool-card__icon ${isError ? 'kim-tool-card__icon--error' : 'kim-tool-card__icon--result'}`} aria-hidden>
          {isError ? '⚠' : <CheckIcon />}
        </span>
        <span className={`kim-tool-card__name ${isError ? 'kim-tool-card__name--error' : 'kim-tool-card__name--neutral'}`}>
          {isError ? 'Needs Help' : 'Task Complete'}
        </span>
        <Chevron open={open} />
      </button>
      {open && (
        <div className="kim-tool-card__body">
          <pre className="kim-tool-card__pre" style={{ whiteSpace: 'pre-wrap' }}>{text}</pre>
          {onAction && actionLabel && (
            <div style={{ marginTop: '8px' }}>
              <button 
                type="button" 
                className="kim-task-error__retry" 
                onClick={onAction}
              >
                {actionLabel}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
