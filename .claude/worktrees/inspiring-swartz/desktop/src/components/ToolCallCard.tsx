import { useState } from 'react';
import type { ToolUseBlock, ToolResultBlock } from '../types';

// ── Tool-use card (agent is calling a tool) ──────────────────────────────────

interface ToolUseCardProps {
  block: ToolUseBlock;
  result?: string;
}

export function ToolUseCard({ block, result }: ToolUseCardProps) {
  const [open, setOpen] = useState(false);

  const argsStr = JSON.stringify(block.input, null, 2);

  return (
    <div
      style={{
        border: '1px solid var(--border)',
        borderRadius: '8px',
        overflow: 'hidden',
        marginBottom: '8px',
        background: 'var(--bg-card)',
      }}
    >
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          width: '100%',
          padding: '8px 12px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--text)',
          textAlign: 'left',
        }}
      >
        <span style={{ fontSize: '13px' }}>⚙️</span>
        <span
          style={{
            fontFamily: 'monospace',
            fontSize: '12px',
            fontWeight: 600,
            color: 'var(--accent)',
            flex: 1,
          }}
        >
          {block.name}
        </span>
        <span
          style={{
            fontSize: '10px',
            color: 'var(--text-muted)',
            transform: open ? 'rotate(180deg)' : 'none',
            transition: 'transform 0.15s ease',
          }}
        >
          ▼
        </span>
      </button>

      {/* Body */}
      {open && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '10px 12px' }}>
          <div style={{ marginBottom: '8px' }}>
            <div
              style={{
                fontSize: '10px',
                fontWeight: 600,
                textTransform: 'uppercase',
                color: 'var(--text-muted)',
                marginBottom: '4px',
                letterSpacing: '0.05em',
              }}
            >
              Input
            </div>
            <pre
              style={{
                fontSize: '11px',
                background: 'var(--bg)',
                border: '1px solid var(--border)',
                borderRadius: '6px',
                padding: '8px 10px',
                overflow: 'auto',
                maxHeight: '200px',
                color: 'var(--text)',
                margin: 0,
              }}
            >
              {argsStr}
            </pre>
          </div>

          {result !== undefined && (
            <div>
              <div
                style={{
                  fontSize: '10px',
                  fontWeight: 600,
                  textTransform: 'uppercase',
                  color: 'var(--text-muted)',
                  marginBottom: '4px',
                  letterSpacing: '0.05em',
                }}
              >
                Result
              </div>
              <pre
                style={{
                  fontSize: '11px',
                  background: 'var(--bg)',
                  border: '1px solid var(--border)',
                  borderRadius: '6px',
                  padding: '8px 10px',
                  overflow: 'auto',
                  maxHeight: '200px',
                  color: 'var(--text)',
                  margin: 0,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {result}
              </pre>
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
    typeof block.content === 'string'
      ? block.content
      : JSON.stringify(block.content, null, 2);

  return (
    <div
      style={{
        border: '1px solid var(--border)',
        borderRadius: '8px',
        overflow: 'hidden',
        marginBottom: '8px',
        background: 'var(--bg-card)',
      }}
    >
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          width: '100%',
          padding: '8px 12px',
          background: 'none',
          border: 'none',
          cursor: 'pointer',
          color: 'var(--text)',
          textAlign: 'left',
        }}
      >
        <span style={{ fontSize: '13px' }}>📋</span>
        <span
          style={{
            fontFamily: 'monospace',
            fontSize: '12px',
            color: 'var(--text-muted)',
            flex: 1,
          }}
        >
          Tool result
        </span>
        <span
          style={{
            fontSize: '10px',
            color: 'var(--text-muted)',
            transform: open ? 'rotate(180deg)' : 'none',
            transition: 'transform 0.15s ease',
          }}
        >
          ▼
        </span>
      </button>
      {open && (
        <div style={{ borderTop: '1px solid var(--border)', padding: '10px 12px' }}>
          <pre
            style={{
              fontSize: '11px',
              background: 'var(--bg)',
              border: '1px solid var(--border)',
              borderRadius: '6px',
              padding: '8px 10px',
              overflow: 'auto',
              maxHeight: '200px',
              color: 'var(--text)',
              margin: 0,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}
