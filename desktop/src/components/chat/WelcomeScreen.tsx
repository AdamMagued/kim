import type { ReactNode } from 'react';
import type { SessionInfo, KimAccount } from '../../types';
import { getGreeting, projectLabel } from './utils';

export interface WelcomeScreenProps {
  account: KimAccount;
  activeTab: 'chat' | 'code';
  onNewChat?: () => void;
  onNewCodeSession?: () => void;
  recentSessions?: SessionInfo[];
  onSelectSession?: (s: SessionInfo) => void;
  handlePickCodeProject: (mode: 'create' | 'open') => void;
  renderConnectorsChrome: () => ReactNode;
}

export function WelcomeScreen({
  account,
  activeTab,
  onNewChat,
  onNewCodeSession,
  recentSessions,
  onSelectSession,
  handlePickCodeProject,
  renderConnectorsChrome,
}: WelcomeScreenProps) {
  const firstName = account.display_name.split(' ')[0] || 'there';
  const greeting = activeTab === 'code' ? 'What are we building?' : getGreeting(firstName);
  const subtitle =
    activeTab === 'code'
      ? 'Create a new project or open an existing project folder.'
      : 'Pick up where you left off, or start fresh.';

  return (
    <div className="kim-chat">
      {renderConnectorsChrome()}

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
        <h1
          style={{
            fontWeight: 500,
            fontSize: 28,
            color: 'var(--kim-text)',
            margin: '0 0 6px',
            letterSpacing: '-0.01em',
            textAlign: 'center',
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
            textAlign: 'center',
          }}
        >
          {subtitle}
        </p>

        {activeTab === 'code' ? (
          <div style={{ display: 'flex', gap: 10, marginBottom: 36 }}>
            <button
              type="button"
              className="kr-btn kr-btn-primary"
              onClick={() => void handlePickCodeProject('create')}
              style={{ padding: '10px 18px' }}
            >
              <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
                <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
              Create new project
            </button>
            <button
              type="button"
              className="kr-btn"
              onClick={() => void handlePickCodeProject('open')}
              style={{ padding: '10px 16px' }}
            >
              <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
                <path
                  d="M2 3.5h3l1 1h4a1 1 0 011 1V9a1 1 0 01-1 1H2a1 1 0 01-1-1V4.5a1 1 0 011-1z"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinejoin="round"
                />
              </svg>
              Open project folder
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', gap: 10, marginBottom: 36 }}>
            <button
              type="button"
              className="kr-btn kr-btn-primary"
              onClick={() => onNewChat?.()}
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
            <button
              type="button"
              className="kr-btn"
              onClick={() => onNewCodeSession?.()}
              style={{ padding: '10px 16px' }}
            >
              <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
                <path
                  d="M4 3l-3 3 3 3m4-6l3 3-3 3"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              New code session
            </button>
          </div>
        )}

        {(() => {
          const pickupSessions = (recentSessions ?? [])
            .filter(s => (activeTab === 'code' ? s.session_type === 'codex' : s.session_type === 'kim'))
            .slice(0, 3);
          if (pickupSessions.length === 0) return null;
          return (
            <div
              style={{
                width: 'min(560px, 90%)',
                display: 'flex',
                flexDirection: 'column',
                gap: 8,
                marginBottom: 36,
              }}
            >
              <span className="kr-eyebrow" style={{ marginBottom: 4, paddingLeft: 4 }}>
                pick up where you left off
              </span>
              {pickupSessions.map((s, i) => {
                const t = s.title?.trim() || s.session_id;
                const project = activeTab === 'code' ? projectLabel(s.project_path) : null;
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
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 14 14"
                      fill="none"
                      style={{ color: 'var(--kim-text-3)' }}
                    >
                      <path
                        d="M2 3.5h10a1 1 0 011 1V10a1 1 0 01-1 1H5l-2.5 2v-2H2a1 1 0 01-1-1V4.5a1 1 0 011-1z"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                    </svg>
                    <span style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
                      <span
                        style={{
                          fontSize: 13.5,
                          color: 'var(--kim-text)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {t}
                      </span>
                      {project && (
                        <span
                          style={{
                            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                            fontSize: 11,
                            color: 'var(--kim-text-3)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                          title={s.project_path}
                        >
                          {project}
                        </span>
                      )}
                    </span>
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 12 12"
                      fill="none"
                      style={{ color: 'var(--kim-text-4)' }}
                    >
                      <path
                        d="M4 2.5L7.5 6L4 9.5"
                        stroke="currentColor"
                        strokeWidth="1.4"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </div>
                );
              })}
            </div>
          );
        })()}

        <div
          style={{
            display: 'flex',
            gap: 18,
            fontSize: 11.5,
            color: 'var(--kim-text-3)',
            fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
          }}
        >
          <span>⌘N new chat</span>
          <span>⌘, settings</span>
        </div>
      </div>
    </div>
  );
}
