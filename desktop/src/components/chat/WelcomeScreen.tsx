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
    <div className="kim-chat kim-launch">
      {renderConnectorsChrome()}

      <div className="kim-launch__content">
        <div className="kim-launch__mark" aria-hidden="true">K</div>
        <h1 className="kim-launch__title">{greeting}</h1>
        <p className="kim-launch__subtitle">{subtitle}</p>

        {activeTab === 'code' ? (
          <div className="kim-launch__actions">
            <button
              type="button"
              className="kr-btn kr-btn-primary"
              onClick={() => void handlePickCodeProject('create')}
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
          <div className="kim-launch__actions">
            <button
              type="button"
              className="kr-btn kr-btn-primary"
              onClick={() => onNewChat?.()}
            >
              <svg width="13" height="13" viewBox="0 0 12 12" fill="none">
                <path d="M6 1v10M1 6h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
              New chat
              <span
                className="kr-kbd kim-launch__primary-kbd"
              >
                ⌘N
              </span>
            </button>
            <button
              type="button"
              className="kr-btn"
              onClick={() => onNewCodeSession?.()}
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
            <div className="kim-launch__recents">
              <span className="kr-eyebrow kim-launch__recents-label">Pick up where you left off</span>
              {pickupSessions.map((s, i) => {
                const t = s.title?.trim() || s.session_id;
                const project = activeTab === 'code' ? projectLabel(s.project_path) : null;
                return (
                  <button
                    type="button"
                    key={s.session_key ?? `${s.session_id}-${i}`}
                    className="kim-launch__recent"
                    onClick={() => onSelectSession?.(s)}
                  >
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 14 14"
                      fill="none"
                      className="kim-launch__recent-icon"
                    >
                      <path
                        d="M2 3.5h10a1 1 0 011 1V10a1 1 0 01-1 1H5l-2.5 2v-2H2a1 1 0 01-1-1V4.5a1 1 0 011-1z"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                    </svg>
                    <span className="kim-launch__recent-copy">
                      <span className="kim-launch__recent-title">{t}</span>
                      {project && (
                        <span
                          className="kim-launch__recent-project"
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
                      className="kim-launch__recent-arrow"
                    >
                      <path
                        d="M4 2.5L7.5 6L4 9.5"
                        stroke="currentColor"
                        strokeWidth="1.4"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </button>
                );
              })}
            </div>
          );
        })()}

        <div className="kim-launch__shortcuts">
          <span>⌘N new chat</span>
          <span>⌘, settings</span>
        </div>
      </div>
    </div>
  );
}
