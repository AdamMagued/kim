// ContextMeter — renders the context-usage gauge (audit A1/D-A1).
//
// The whole metering pipeline already exists end-to-end
// (orchestrator/context_meter.py → kim:context → Rust → useChatStream
// contextState) but its output was rendered by NOTHING — the user's stated
// gap ("no visible indication of context usage") was literally true. This is
// the missing last-mile JSX.

export interface ContextMeterState {
  cumulative_input: number;
  budget: number;
  phase: string;
  percent: number;
  last_input: number;
  last_output: number;
  source: string;
  estimate: boolean;
}

/** Compact k/M formatting for token counts. */
function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(Math.round(n));
}

export function ContextMeter({ state }: { state: ContextMeterState | null }) {
  if (!state) return null;
  const budget = state.budget > 0 ? state.budget : 0;
  // Prefer the backend-provided percent; fall back to a derived one.
  const rawPct = Number.isFinite(state.percent)
    ? state.percent
    : budget > 0
      ? (state.cumulative_input / budget) * 100
      : 0;
  const pct = Math.max(0, Math.min(100, rawPct));
  const level = pct >= 95 ? 'crit' : pct >= 80 ? 'warn' : 'ok';
  const color =
    level === 'crit' ? 'var(--kim-danger, #e5484d)' : level === 'warn' ? '#e0a52b' : 'var(--kim-accent, #6b8afd)';

  const compacting = state.phase === 'compacting' || state.phase === 'compact';
  const usedLabel = budget > 0 ? `${fmtTokens(state.cumulative_input)} / ${fmtTokens(budget)}` : fmtTokens(state.cumulative_input);
  const title =
    `Context: ${pct.toFixed(0)}% of budget used` +
    (budget > 0 ? ` (${state.cumulative_input.toLocaleString()} / ${budget.toLocaleString()} tokens)` : '') +
    (state.estimate ? ' · estimated' : '') +
    (state.source ? ` · ${state.source}` : '');

  return (
    <div
      className="kim-context-meter"
      data-testid="context-meter"
      data-level={level}
      role="meter"
      aria-label="Context usage"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      title={title}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        fontSize: 11.5,
        color: 'var(--kim-text-3, #8a8f98)',
        padding: '2px 2px',
        userSelect: 'none',
      }}
    >
      <span style={{ whiteSpace: 'nowrap' }}>
        {compacting ? 'Compacting…' : 'Context'}
      </span>
      <span
        aria-hidden="true"
        style={{
          position: 'relative',
          flex: '1 1 auto',
          minWidth: 60,
          maxWidth: 180,
          height: 5,
          borderRadius: 3,
          background: 'var(--kim-border, rgba(255,255,255,0.1))',
          overflow: 'hidden',
        }}
      >
        <span
          style={{
            position: 'absolute',
            inset: 0,
            width: `${pct}%`,
            background: color,
            borderRadius: 3,
            transition: 'width 240ms ease',
          }}
        />
      </span>
      <span style={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums', color }}>
        {pct.toFixed(0)}%
      </span>
      <span style={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
        {usedLabel}
        {state.estimate ? ' ~' : ''}
      </span>
      {level === 'warn' && !compacting && (
        <span style={{ color }} title="Approaching the context budget — Kim may compact soon.">
          · near budget
        </span>
      )}
      {level === 'crit' && !compacting && (
        <span style={{ color }} title="Context budget nearly full — run /compact to free space.">
          · run /compact
        </span>
      )}
    </div>
  );
}
