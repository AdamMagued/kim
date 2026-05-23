import { Mascot } from './Mascot';
import type { MascotVariant } from './Mascot';
import type { SessionInfo } from '../../types';

interface Props {
  displayName: string;
  recentSessions?: SessionInfo[];
  mascot?: MascotVariant;
  onNewChat?: () => void;
  onNewCodeSession?: () => void;
  onSelectSession?: (s: SessionInfo) => void;
}

function timeOfDayGreeting(): string {
  const h = new Date().getHours();
  if (h < 5) return 'Up late';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

export function AppLaunchView({
  displayName,
  recentSessions = [],
  mascot = 'none',
  onNewChat,
  onNewCodeSession,
  onSelectSession,
}: Props) {
  const greeting = `${timeOfDayGreeting()}, ${displayName.split(' ')[0]}.`;
  const recents = recentSessions.slice(0, 3);

  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 40,
        position: 'relative',
      }}
    >
      <Mascot size={64} variant={mascot} />
      <h1
        style={{
          fontWeight: 500,
          fontSize: 28,
          color: 'var(--kim-text)',
          margin: mascot === 'none' ? '0 0 6px' : '22px 0 6px',
          letterSpacing: '-0.01em',
        }}
      >
        {greeting}
      </h1>
      <p
        style={{
          color: 'var(--kim-text-3)',
          fontSize: 14,
          margin: 0,
          marginBottom: 28,
          fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
        }}
      >
        Pick up where you left off, or start fresh.
      </p>

      <div style={{ display: 'flex', gap: 10, marginBottom: 36 }}>
        <button
          type="button"
          className="kr-btn kr-btn-primary"
          onClick={onNewChat}
          style={{ padding: '10px 18px' }}
        >
          <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
            <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
          New chat
          <span
            className="kr-kbd"
            style={{
              background: 'rgba(0,0,0,0.15)',
              borderColor: 'rgba(0,0,0,0.2)',
              color: 'var(--kim-on-accent)',
            }}
          >
            ⌘N
          </span>
        </button>
        <button type="button" className="kr-btn" onClick={onNewCodeSession} style={{ padding: '10px 16px' }}>
          <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
            <path d="M4 3l-3 3 3 3m4-6l3 3-3 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          New code session
        </button>
      </div>

      {recents.length > 0 && (
        <div style={{ width: 'min(560px, 90%)', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <span className="kr-eyebrow" style={{ marginBottom: 4, paddingLeft: 4 }}>
            pick up where you left off
          </span>
          {recents.map((s, i) => {
            const title = s.title?.trim() || s.session_id;
            return (
              <div
                key={i}
                className="kr-row-hover"
                onClick={() => onSelectSession?.(s)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '12px 16px',
                  background: 'var(--kim-surface)',
                  border: '1px solid var(--kim-border)',
                  borderRadius: 11,
                  cursor: 'pointer',
                }}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style={{ color: 'var(--kim-text-3)' }}>
                  <path
                    d="M2 3.5h10a1 1 0 011 1V10a1 1 0 01-1 1H5l-2.5 2v-2H2a1 1 0 01-1-1V4.5a1 1 0 011-1z"
                    stroke="currentColor"
                    strokeWidth="1.2"
                  />
                </svg>
                <span style={{ flex: 1, fontSize: 13.5, color: 'var(--kim-text)' }}>{title}</span>
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" style={{ color: 'var(--kim-text-4)' }}>
                  <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </div>
            );
          })}
        </div>
      )}

      <div
        style={{
          marginTop: 36,
          display: 'flex',
          gap: 18,
          fontSize: 11.5,
          color: 'var(--kim-text-3)',
          fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
        }}
      >
        <span>⌘N new chat</span>
        <span>⌘K search</span>
        <span>⌘, settings</span>
      </div>
    </div>
  );
}
