import React, { useState, useEffect } from 'react';

export type PlanStepStatus = 'done' | 'active' | 'pending' | 'todo';

export interface PlanStep {
  status: PlanStepStatus;
  text: string;
}

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
  Reading: 'var(--info)',
  Writing: 'var(--warning)',
  Updated: 'var(--success)',
  Running: 'var(--kim-accent)',
};

function verbColor(verb: string): string {
  return VERB_COLORS[verb] ?? 'var(--kim-text-2)';
}

function InlinePlanBlock({ plan, look = 'card' }: { plan: Extract<TraceItem, { kind: 'plan' }>; look?: 'card' | 'compact' }) {
  const allDone = plan.items.length > 0 && plan.items.every((i) => i.status === 'done');
  const [open, setOpen] = useState(true);
  const panelId = `kim-plan-${plan.title.replace(/\s+/g, '-').toLowerCase()}`;

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
    <div className={`kim-inline-plan kim-inline-plan--${look}${allDone ? ' kim-inline-plan--done' : ''}`}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={panelId}
        className="kim-inline-plan__toggle"
      >
        <svg
          className="kim-inline-plan__chevron"
          width={isCard ? 11 : 10}
          height={isCard ? 11 : 10}
          viewBox="0 0 12 12"
          fill="none"
          data-open={open ? 'true' : 'false'}
        >
          <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {isCard ? (
          <span className="kim-inline-plan__signal" />
        ) : (
          <svg className="kim-inline-plan__icon" width="11" height="11" viewBox="0 0 12 12" fill="none">
            <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" stroke="var(--kim-accent)" strokeWidth="1.2" />
            <path d="M3.5 5.5L5 7L8.5 3.5" stroke="var(--kim-accent)" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        )}
        <span className="kim-inline-plan__title">{plan.title}</span>
        {isCard && (
          <span className="kim-inline-plan__meta">
            {done} of {total} done{inFlight ? ` · ${inFlight} in flight` : ''}
          </span>
        )}
        {!open && activeIdx >= 0 && !isCard && (
          <span className="kim-inline-plan__active-summary">
            <span className="kr-shimmer">{plan.items[activeIdx].text}</span>
          </span>
        )}
        <span className="kim-inline-plan__segments" aria-hidden="true">
          {plan.items.map((it, i) => (
            <span
              key={i}
              className={`kim-inline-plan__segment kim-inline-plan__segment--${it.status}`}
            />
          ))}
        </span>
        {!isCard && (
          <span className="kim-inline-plan__counter">{done}/{total}</span>
        )}
      </button>
      {open && (
        <div id={panelId} className="kim-inline-plan__body">
          {plan.items.map((it, i) => {
            if (isCard) {
              return (
                <div key={i} className={`kim-inline-plan__step kim-inline-plan__step--${it.status}`}>
                  <div className="kim-inline-plan__marker">
                    {it.status === 'done' && (
                      <span className="kim-inline-plan__check">
                        <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
                          <path d="M1.5 4L3 5.5L6.5 2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </span>
                    )}
                    {it.status === 'active' && <span className="kr-spinner" />}
                    {(it.status === 'pending' || it.status === 'todo') && (
                      <span className="kim-inline-plan__pending" />
                    )}
                  </div>
                  <span className="kim-inline-plan__step-text">
                    {it.status === 'active' ? <span className="kr-shimmer">{it.text}</span> : it.text}
                  </span>
                  {it.status === 'active' && (
                    <span className="kim-inline-plan__now">now</span>
                  )}
                </div>
              );
            }
            return (
              <div key={i} className={`kim-inline-plan__compact-step kim-inline-plan__compact-step--${it.status}`}>
                <span className="kim-inline-plan__compact-marker">
                  {it.status === 'done' && (
                    <svg width="9" height="9" viewBox="0 0 10 10" fill="none">
                      <path
                        d="M2 5L4 7L8 3"
                        stroke="currentColor"
                        strokeWidth="1.6"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                  {it.status === 'active' && (
                    <span className="kim-inline-plan__compact-dot" />
                  )}
                </span>
                <span
                  className={it.status === 'active' ? 'kr-shimmer' : ''}
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

export function ThinkingWithPlan({ trace, duration = '', steps, planLook = 'card', style, className, live = true }: Props) {
  const explicitActive = trace.findIndex((i) => 'active' in i && (i as { active?: boolean }).active);
  const streamItems = trace.map((it, i) => ({ ...it, i })).filter((it) => it.kind !== 'plan');
  const lastItem = streamItems[streamItems.length - 1] as { i: number } | undefined;
  const activeIdx = live ? (explicitActive !== -1 ? explicitActive : lastItem?.i ?? -1) : -1;

  return (
    <div
      className={`kim-thinking-panel${live ? ' kim-thinking-panel--live' : ''}${className ? ` ${className}` : ''}`}
      style={style}
    >
      <div className="kim-thinking-panel__header">
        {live ? <span className="kr-pulse-dot" /> : <span className="kim-thinking-panel__rest-dot" />}
        <span className={`kim-thinking-panel__title${live ? ' kr-shimmer' : ''}`}>
          Thinking
        </span>
        <span className="kim-thinking-panel__meta">
          {duration}
          {steps != null && ` · ${steps} steps`}
        </span>
      </div>

      <div className="kim-thinking-panel__trace">
        {trace.map((it, i) => {
          if (it.kind === 'plan') {
            return (
              <div key={i} className="kim-thinking-panel__plan-wrap">
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
                className={`kim-thinking-panel__thought${isActive && live ? ' kr-shimmer-mono' : ''}`}
              >
                {it.text}
              </span>
            ) : (
              <span className="kim-thinking-panel__tool">
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
              className={`kim-thinking-panel__trace-row${live ? ' kr-fade-up' : ''}`}
              style={{ opacity: rowOpacity }}
            >
              <span className="kim-thinking-panel__cursor">{cursor}</span>
              {content}
            </div>
          );
        })}
      </div>
    </div>
  );
}
