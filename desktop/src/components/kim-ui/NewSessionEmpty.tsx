import type { ReactNode } from 'react';

interface Props {
  mode: 'chat' | 'code';
  /** Optional path / status pill, e.g. ~/code/pong-arena */
  rightLabel?: string;
  /** Optional provider line, e.g. "Browser: Claude · ready" */
  providerStatus?: string;
  /** Optional override of the headline */
  headline?: string;
  /** Optional override of the sub-text */
  subText?: string;
  /** The composer to render. Already styled by the consumer. */
  composer?: ReactNode;
  /** Optional starter chips row */
  starters?: { key: string; label: string }[];
  onStarterClick?: (label: string) => void;
}

const DEFAULTS: Record<'chat' | 'code', { headline: string; sub: string }> = {
  chat: {
    headline: 'What would you like Kim to do?',
    sub: "Ask a question, hand off a chore, or point me at your screen — I'll take it from there.",
  },
  code: {
    headline: 'What are we building?',
    sub: "Describe a feature, hand me a bug, or point me at a file. I'll read first, plan, then write.",
  },
};

export function NewSessionEmpty({
  mode,
  rightLabel,
  providerStatus,
  headline,
  subText,
  composer,
  starters,
  onStarterClick,
}: Props) {
  const defaults = DEFAULTS[mode];
  const title = headline ?? defaults.headline;
  const sub = subText ?? defaults.sub;
  const ribbon = mode === 'code' ? 'CODE' : 'CHAT';
  const ribbonLabel = mode === 'code' ? 'New session' : 'New chat';

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', position: 'relative' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          padding: '14px 22px',
          borderBottom: '1px solid var(--kim-border)',
        }}
      >
        <span
          style={{
            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
            fontSize: 10,
            letterSpacing: '0.14em',
            color: 'var(--kim-accent)',
            border: '1px solid var(--kim-accent-line)',
            padding: '4px 8px',
            borderRadius: 6,
          }}
        >
          {ribbon}
        </span>
        <span style={{ color: 'var(--kim-text-2)', fontSize: 13 }}>{ribbonLabel}</span>
        {rightLabel && (
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
            <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 11, color: 'var(--kim-text-3)' }}>
              {rightLabel}
            </span>
          </div>
        )}
      </div>

      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 40,
        }}
      >
        {providerStatus && (
          <div
            style={{
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              fontSize: 11,
              color: 'var(--kim-text-3)',
              marginBottom: 18,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              padding: '6px 12px',
              border: '1px solid var(--kim-border)',
              borderRadius: 999,
              background: 'var(--kim-surface)',
            }}
          >
            <span className="kr-pulse-dot" style={{ background: 'var(--kim-green)' }} />
            <span>{providerStatus}</span>
          </div>
        )}

        <h1
          style={{
            fontWeight: 500,
            fontSize: 30,
            color: 'var(--kim-text)',
            margin: '0 0 10px',
            letterSpacing: '-0.015em',
            textAlign: 'center',
          }}
        >
          {title}
        </h1>
        <p
          style={{
            color: 'var(--kim-text-3)',
            fontSize: 14,
            margin: 0,
            marginBottom: 32,
            textAlign: 'center',
            maxWidth: 480,
            lineHeight: 1.5,
          }}
        >
          {sub}
        </p>

        {composer && <div style={{ width: 'min(680px, 95%)' }}>{composer}</div>}

        {starters && starters.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 18, justifyContent: 'center' }}>
            {starters.map((s) => (
              <button
                key={s.key}
                type="button"
                className="kr-btn"
                onClick={() => onStarterClick?.(s.label)}
                style={{ background: 'transparent', padding: '7px 12px', fontSize: 12.5, color: 'var(--kim-text-2)' }}
              >
                <span style={{ color: 'var(--kim-accent)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>{s.key}</span>
                <span>{s.label}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
