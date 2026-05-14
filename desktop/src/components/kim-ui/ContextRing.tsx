import { useState } from 'react';

interface Props {
  used?: number;
  total?: number;
  size?: number;
  align?: 'left' | 'right';
  onCompact?: () => void;
}

function fmt(n: number): string {
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k';
  return String(n);
}

export function ContextRing({ used = 0, total = 200000, size = 28, align = 'right', onCompact }: Props) {
  const [open, setOpen] = useState(false);
  const pct = Math.min(1, used / Math.max(1, total));
  const r = 12;
  const circ = 2 * Math.PI * r;
  const offset = circ * (1 - pct);
  const warn = pct > 0.85;
  const ringColor = warn ? 'var(--kim-red)' : 'var(--kim-accent)';
  const pctLabel = Math.round(pct * 100);

  return (
    <div style={{ position: 'relative', display: 'inline-flex' }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        title={`Context · ${pctLabel}% of ${fmt(total)} tokens used`}
        style={{
          width: size,
          height: size,
          borderRadius: 999,
          border: '1px solid var(--kim-border)',
          background: 'var(--kim-bg-2)',
          position: 'relative',
          padding: 0,
          cursor: 'pointer',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <svg width={size - 4} height={size - 4} viewBox="0 0 32 32" style={{ display: 'block' }}>
          <circle cx="16" cy="16" r={r} stroke="var(--kim-border)" strokeWidth="3" fill="none" />
          <circle
            cx="16"
            cy="16"
            r={r}
            stroke={ringColor}
            strokeWidth="3"
            fill="none"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            strokeLinecap="round"
            transform="rotate(-90 16 16)"
            style={{ transition: 'stroke-dashoffset .25s' }}
          />
        </svg>
      </button>
      {open && (
        <>
          <div onClick={() => setOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 40 }} />
          <div
            style={{
              position: 'absolute',
              bottom: 'calc(100% + 10px)',
              [align === 'left' ? 'left' : 'right']: 0,
              background: 'var(--kim-surface)',
              border: '1px solid var(--kim-border)',
              borderRadius: 12,
              padding: 14,
              boxShadow: '0 18px 40px rgba(0,0,0,0.5)',
              width: 280,
              zIndex: 50,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
              <span
                style={{
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 16,
                  color: warn ? 'var(--kim-red)' : 'var(--kim-text)',
                }}
              >
                {fmt(used)}
                <span style={{ color: 'var(--kim-text-3)' }}>/{fmt(total)}</span>
              </span>
              <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 12, color: 'var(--kim-text-3)' }}>
                tokens
              </span>
              <span
                style={{
                  marginLeft: 'auto',
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 12,
                  color: warn ? 'var(--kim-red)' : 'var(--kim-accent)',
                }}
              >
                {pctLabel}%
              </span>
            </div>

            <div
              style={{
                width: '100%',
                height: 6,
                borderRadius: 999,
                background: 'var(--kim-bg-2)',
                border: '1px solid var(--kim-border)',
                overflow: 'hidden',
                marginBottom: 12,
                position: 'relative',
              }}
            >
              <div
                style={{
                  width: `${pct * 100}%`,
                  height: '100%',
                  background: warn
                    ? 'linear-gradient(90deg, var(--kim-accent), var(--kim-red))'
                    : 'linear-gradient(90deg, var(--kim-accent-line), var(--kim-accent))',
                  borderRadius: 999,
                  transition: 'width .3s',
                }}
              />
            </div>

            <button
              type="button"
              className="kr-btn"
              onClick={() => {
                onCompact?.();
                setOpen(false);
              }}
              style={{ width: '100%', justifyContent: 'center', padding: '9px 12px' }}
            >
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none">
                <path d="M2 7h10M4 4l-2 3 2 3M10 4l2 3-2 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Compact conversation
            </button>

            <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginTop: 10, lineHeight: 1.55 }}>
              More context means slower, less accurate replies. Compacting summarises older turns so Kim can keep up.
            </div>
          </div>
        </>
      )}
    </div>
  );
}
