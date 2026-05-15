import React, { useState, useEffect } from 'react';
import type { PlanStep } from './CollapsiblePlan';

export type TraceItem =
  | { kind: 'thought'; text: string; active?: boolean }
  | { kind: 'tool'; verb: string; target: string; active?: boolean }
  | { kind: 'plan'; title: string; items: PlanStep[] };

interface Props {
  trace: TraceItem[];
  duration?: string;
  steps?: number;
  planLook?: 'card' | 'compact';
  style?: React.CSSProperties;
  className?: string;
  /** false = historical view: no pulse dot, no shimmer, all items full opacity */
  live?: boolean;
}

const VERB_COLORS: Record<string, string> = {
  Reading: '#a9c8e8',
  Writing: '#e8b89a',
  Updated: '#b6dab1',
  Running: '#d8b8e8',
};

function verbColor(verb: string): string {
  return VERB_COLORS[verb] ?? 'var(--kim-text-2)';
}

function InlinePlanBlock({ plan, look = 'card' }: { plan: Extract<TraceItem, { kind: 'plan' }>; look?: 'card' | 'compact' }) {
  const allDone = plan.items.length > 0 && plan.items.every((i) => i.status === 'done');
  const [open, setOpen] = useState(true);

  // Auto-collapse once every step is checked off
  useEffect(() => {
    if (allDone) setOpen(false);
  }, [allDone]);

  const done = plan.items.filter((i) => i.status === 'done').length;
  const total = plan.items.length;
  const inFlight = plan.items.filter((i) => i.status === 'active').length;
  const activeIdx = plan.items.findIndex((i) => i.status === 'active');
  const isCard = look === 'card';

  return (
    <div
      style={{
        background: 'var(--kim-surface)',
        border: '1px solid var(--kim-border)',
        borderLeft: isCard ? '1px solid var(--kim-border)' : '2px solid var(--kim-accent)',
        borderRadius: isCard ? 14 : 10,
        marginLeft: 24,
        overflow: 'hidden',
        fontFamily: 'Inter, system-ui, sans-serif',
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
          padding: isCard ? '14px 18px' : '10px 12px',
          background: 'transparent',
          border: 0,
          cursor: 'pointer',
          color: 'var(--kim-text)',
          fontSize: 13.5,
          textAlign: 'left',
        }}
      >
        <svg
          width={isCard ? 11 : 10}
          height={isCard ? 11 : 10}
          viewBox="0 0 12 12"
          fill="none"
          style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .2s', color: 'var(--kim-text-3)' }}
        >
          <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {isCard ? (
          <span style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--kim-accent)' }} />
        ) : (
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
            <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" stroke="var(--kim-accent)" strokeWidth="1.2" />
            <path d="M3.5 5.5L5 7L8.5 3.5" stroke="var(--kim-accent)" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        )}
        <span
          style={
            isCard
              ? { fontWeight: 500 }
              : {
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 11,
                  letterSpacing: '0.1em',
                  textTransform: 'uppercase',
                  color: 'var(--kim-accent)',
                }
          }
        >
          {plan.title}
        </span>
        {isCard && (
          <span style={{ color: 'var(--kim-text-3)', fontSize: 12, fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
            {done} of {total} done{inFlight ? ` · ${inFlight} in flight` : ''}
          </span>
        )}
        {!open && activeIdx >= 0 && !isCard && (
          <span
            style={{
              fontSize: 12.5,
              color: 'var(--kim-text-2)',
              marginLeft: 6,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flex: 1,
              minWidth: 0,
            }}
          >
            <span className="kr-shimmer">{plan.items[activeIdx].text}</span>
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 3, flexShrink: 0 }}>
          {plan.items.map((it, i) => (
            <span
              key={i}
              style={{
                width: isCard ? 22 : 18,
                height: 3,
                borderRadius: 999,
                background:
                  it.status === 'done'
                    ? 'var(--kim-accent)'
                    : it.status === 'active'
                    ? 'var(--kim-accent-line)'
                    : 'var(--kim-border)',
              }}
            />
          ))}
        </span>
        {!isCard && (
          <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 11, color: 'var(--kim-text-3)', flexShrink: 0 }}>
            {done}/{total}
          </span>
        )}
      </button>
      {open && (
        <div style={{ padding: isCard ? '4px 18px 16px 18px' : '2px 12px 10px', borderTop: '1px solid var(--kim-border)' }}>
          {plan.items.map((it, i) => {
            if (isCard) {
              return (
                <div
                  key={i}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 12,
                    padding: '10px 0',
                    borderBottom: i < plan.items.length - 1 ? '1px solid var(--kim-border)' : 'none',
                  }}
                >
                  <div style={{ width: 18, flexShrink: 0, paddingTop: 2, display: 'flex', justifyContent: 'center' }}>
                    {it.status === 'done' && (
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
                    {it.status === 'active' && <span className="kr-spinner" />}
                    {(it.status === 'pending' || it.status === 'todo') && (
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
                      color:
                        it.status === 'done' ? 'var(--kim-text-3)' : it.status === 'active' ? 'var(--kim-text)' : 'var(--kim-text-2)',
                      textDecoration: it.status === 'done' ? 'line-through' : 'none',
                      textDecorationColor: 'var(--kim-text-4)',
                      flex: 1,
                    }}
                  >
                    {it.status === 'active' ? <span className="kr-shimmer">{it.text}</span> : it.text}
                  </span>
                  {it.status === 'active' && (
                    <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 11, color: 'var(--kim-accent)' }}>
                      now
                    </span>
                  )}
                </div>
              );
            }
            // compact style
            const palette =
              it.status === 'done'
                ? { color: 'var(--kim-green)', bg: '#1d2a22', border: '#2a3d33' }
                : it.status === 'active'
                ? { color: 'var(--kim-accent)', bg: 'var(--kim-accent-soft)', border: 'var(--kim-accent-line)' }
                : { color: 'var(--kim-text-4)', bg: 'transparent', border: 'var(--kim-border)' };
            return (
              <div
                key={i}
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'center',
                  padding: '7px 6px',
                  borderRadius: 7,
                  background: it.status === 'active' ? palette.bg : 'transparent',
                }}
              >
                <span
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: 4,
                    border: `1px solid ${palette.border}`,
                    background: it.status === 'done' ? palette.bg : 'transparent',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  {it.status === 'done' && (
                    <svg width="9" height="9" viewBox="0 0 10 10" fill="none">
                      <path
                        d="M2 5L4 7L8 3"
                        stroke={palette.color}
                        strokeWidth="1.6"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                  {it.status === 'active' && (
                    <span style={{ width: 6, height: 6, borderRadius: 999, background: palette.color }} />
                  )}
                </span>
                <span
                  className={it.status === 'active' ? 'kr-shimmer' : ''}
                  style={{
                    fontSize: 13.5,
                    color:
                      it.status === 'pending' || it.status === 'todo'
                        ? 'var(--kim-text-3)'
                        : it.status === 'done'
                        ? 'var(--kim-text-2)'
                        : 'var(--kim-text)',
                    textDecoration: it.status === 'done' ? 'line-through' : 'none',
                    textDecorationColor: 'var(--kim-text-4)',
                  }}
                >
                  {it.text}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function ThinkingWithPlan({ trace, duration = '0:23', steps, planLook = 'card', style, className, live = true }: Props) {
  const explicitActive = trace.findIndex((i) => 'active' in i && (i as { active?: boolean }).active);
  const streamItems = trace.map((it, i) => ({ ...it, i })).filter((it) => it.kind !== 'plan');
  const lastItem = streamItems[streamItems.length - 1] as { i: number } | undefined;
  const activeIdx = live ? (explicitActive !== -1 ? explicitActive : lastItem?.i ?? -1) : -1;

  return (
    <div
      className={className}
      style={{
        background: 'var(--kim-bg-2)',
        border: '1px solid var(--kim-border)',
        borderRadius: 14,
        padding: '16px 20px 18px',
        fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
        ...style,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: 14,
          paddingBottom: 12,
          borderBottom: '1px dashed var(--kim-border)',
        }}
      >
        {live ? <span className="kr-pulse-dot" /> : <span style={{ width: 8, height: 8, borderRadius: 999, background: 'var(--kim-text-4)', flexShrink: 0 }} />}
        <span className={live ? 'kr-shimmer' : undefined} style={{ fontSize: 13.5, color: live ? undefined : 'var(--kim-text-2)' }}>
          Thinking
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--kim-text-3)', fontSize: 11.5 }}>
          {duration}
          {steps != null && ` · ${steps} steps`}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {trace.map((it, i) => {
          if (it.kind === 'plan') {
            return (
              <div key={i} style={{ margin: '4px 0' }}>
                <InlinePlanBlock plan={it} look={planLook} />
              </div>
            );
          }
          const isActive = i === activeIdx;
          const cursor = isActive ? '▌' : '›';
          // In history mode (live=false), all items render at full opacity with no shimmer
          const rowOpacity = live ? (isActive ? 1 : 0.55) : 0.8;
          const content =
            it.kind === 'thought' ? (
              <span
                style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 14, color: isActive ? undefined : 'var(--kim-text-3)' }}
                className={isActive && live ? 'kr-shimmer-mono' : ''}
              >
                {it.text}
              </span>
            ) : (
              <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 14 }}>
                <span
                  style={{ color: isActive ? undefined : verbColor(it.verb), opacity: isActive ? 1 : 0.85 }}
                  className={isActive && live ? 'kr-shimmer-mono' : ''}
                >
                  {it.verb}
                </span>
                {it.target && <span style={{ color: 'var(--kim-text-3)' }}> </span>}
                {it.target && <span style={{ color: isActive ? 'var(--kim-text)' : 'var(--kim-text-2)' }}>{it.target}</span>}
              </span>
            );
          return (
            <div
              key={i}
              className={live ? 'kr-fade-up' : undefined}
              style={{ display: 'flex', gap: 12, alignItems: 'baseline', opacity: rowOpacity }}
            >
              <span style={{ color: 'var(--kim-text-4)', width: 12, flexShrink: 0, fontSize: 14 }}>{cursor}</span>
              {content}
            </div>
          );
        })}
      </div>
    </div>
  );
}
