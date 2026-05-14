import { useState } from 'react';

export type PlanStepStatus = 'done' | 'active' | 'pending' | 'todo';

export interface PlanStep {
  status: PlanStepStatus;
  text: string;
}

interface Props {
  steps: PlanStep[];
  title?: string;
  defaultOpen?: boolean;
}

export function CollapsiblePlan({ steps, title = 'Plan', defaultOpen = true }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const done = steps.filter((s) => s.status === 'done').length;
  const total = steps.length;
  const inFlight = steps.filter((s) => s.status === 'active').length;

  return (
    <div
      style={{
        background: 'var(--kim-surface)',
        border: '1px solid var(--kim-border)',
        borderRadius: 14,
        overflow: 'hidden',
      }}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '14px 18px',
          background: 'transparent',
          border: 0,
          cursor: 'pointer',
          color: 'var(--kim-text)',
          fontSize: 13.5,
          textAlign: 'left',
        }}
      >
        <svg
          width="11"
          height="11"
          viewBox="0 0 12 12"
          fill="none"
          style={{
            transform: open ? 'rotate(90deg)' : 'none',
            transition: 'transform .2s',
            color: 'var(--kim-text-3)',
          }}
        >
          <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <span style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--kim-accent)' }} />
        <span style={{ fontWeight: 500 }}>{title}</span>
        <span style={{ color: 'var(--kim-text-3)', fontSize: 12, fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
          {done} of {total} done{inFlight ? ` · ${inFlight} in flight` : ''}
        </span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 3 }}>
          {steps.map((s, i) => (
            <span
              key={i}
              style={{
                width: 22,
                height: 3,
                borderRadius: 999,
                background:
                  s.status === 'done'
                    ? 'var(--kim-accent)'
                    : s.status === 'active'
                    ? 'var(--kim-accent-line)'
                    : 'var(--kim-border)',
              }}
            />
          ))}
        </span>
      </button>
      {open && (
        <div style={{ padding: '4px 18px 16px 18px', borderTop: '1px solid var(--kim-border)' }}>
          {steps.map((s, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 12,
                padding: '10px 0',
                borderBottom: i < steps.length - 1 ? '1px solid var(--kim-border)' : 'none',
              }}
            >
              <div style={{ width: 18, flexShrink: 0, paddingTop: 2, display: 'flex', justifyContent: 'center' }}>
                {s.status === 'done' && (
                  <span
                    style={{
                      width: 14,
                      height: 14,
                      borderRadius: 999,
                      background: 'var(--kim-accent)',
                      color: 'var(--kim-on-accent)',
                      display: 'inline-flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                  >
                    <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
                      <path d="M1.5 4L3 5.5L6.5 2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </span>
                )}
                {s.status === 'active' && <span className="kr-spinner" />}
                {(s.status === 'pending' || s.status === 'todo') && (
                  <span
                    style={{
                      width: 13,
                      height: 13,
                      borderRadius: 999,
                      border: '1.5px dashed var(--kim-border)',
                    }}
                  />
                )}
              </div>
              <span
                style={{
                  fontSize: 14,
                  lineHeight: 1.5,
                  color: s.status === 'done' ? 'var(--kim-text-3)' : s.status === 'active' ? 'var(--kim-text)' : 'var(--kim-text-2)',
                  textDecoration: s.status === 'done' ? 'line-through' : 'none',
                  textDecorationColor: 'var(--kim-text-4)',
                  flex: 1,
                }}
              >
                {s.status === 'active' ? <span className="kr-shimmer">{s.text}</span> : s.text}
              </span>
              {s.status === 'active' && (
                <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 11, color: 'var(--kim-accent)' }}>
                  now
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
