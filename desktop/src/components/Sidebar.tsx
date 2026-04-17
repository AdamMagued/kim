import { useState } from 'react';
import type { SessionInfo } from '../types';

interface Props {
  kimSessions: SessionInfo[];
  clawSessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (session: SessionInfo) => void;
  onNewChat: () => void;
  collapsed: boolean;
  onToggle: () => void;
  onOpenSettings: () => void;
  loading: boolean;
}

function SessionItem({
  session,
  active,
  onClick,
}: {
  session: SessionInfo;
  active: boolean;
  onClick: () => void;
}) {
  const preview = session.summary
    ? session.summary.slice(0, 80) + (session.summary.length > 80 ? '…' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;

  return (
    <button
      onClick={onClick}
      title={session.summary ?? session.session_id}
      className={`kim-session-item${active ? ' kim-session-item--active' : ''}`}
    >
      <div className="kim-session-item__title">{session.session_id}</div>
      <div className="kim-session-item__preview">{preview}</div>
    </button>
  );
}

function SectionHeader({
  label,
  count,
  expanded,
  onToggle,
}: {
  label: string;
  count: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button onClick={onToggle} className="kim-section-header">
      <svg
        className={`kim-section-header__chevron${expanded ? ' kim-section-header__chevron--open' : ''}`}
        viewBox="0 0 16 16"
        width="10"
        height="10"
        fill="currentColor"
        aria-hidden
      >
        <path d="M6 4l4 4-4 4V4z" />
      </svg>
      <span className="kim-section-header__label">{label}</span>
      <span className="kim-section-header__count">{count}</span>
    </button>
  );
}

export function Sidebar({
  kimSessions,
  clawSessions,
  activeSessionId,
  onSelectSession,
  onNewChat,
  collapsed,
  onToggle,
  onOpenSettings,
  loading,
}: Props) {
  const [kimExpanded, setKimExpanded] = useState(true);
  const [clawExpanded, setClawExpanded] = useState(true);

  return (
    <aside className={`kim-sidebar${collapsed ? ' kim-sidebar--collapsed' : ''}`}>
      {/* Top bar */}
      <div className="kim-sidebar__top">
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand sidebar (⌘B)' : 'Collapse sidebar (⌘B)'}
          className="kim-sidebar__toggle"
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {collapsed ? (
              <path d="M6 3l5 5-5 5" />
            ) : (
              <path d="M10 3l-5 5 5 5" />
            )}
          </svg>
        </button>

        {!collapsed && (
          <button
            onClick={onNewChat}
            className="kim-sidebar__new-chat"
            title="New chat (⌘N)"
          >
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
              <path d="M8 3v10M3 8h10" />
            </svg>
            <span>New chat</span>
          </button>
        )}
      </div>

      {/* Session lists */}
      {!collapsed && (
        <div className="kim-sidebar__scroll">
          {loading ? (
            <div className="kim-sidebar__loading">
              <div className="kim-skeleton" style={{ height: 32 }} />
              <div className="kim-skeleton" style={{ height: 32 }} />
              <div className="kim-skeleton" style={{ height: 32 }} />
            </div>
          ) : (
            <>
              {/* Kim sessions */}
              <div style={{ marginBottom: 4 }}>
                <SectionHeader
                  label="Kim"
                  count={kimSessions.length}
                  expanded={kimExpanded}
                  onToggle={() => setKimExpanded(v => !v)}
                />
                {kimExpanded && (
                  <div style={{ marginTop: 2 }}>
                    {kimSessions.length === 0 ? (
                      <div className="kim-empty-section">No sessions yet</div>
                    ) : (
                      kimSessions.map(s => (
                        <SessionItem
                          key={s.session_id}
                          session={s}
                          active={s.session_id === activeSessionId}
                          onClick={() => onSelectSession(s)}
                        />
                      ))
                    )}
                  </div>
                )}
              </div>

              {/* Claw sessions */}
              <div>
                <SectionHeader
                  label="Claw Code"
                  count={clawSessions.length}
                  expanded={clawExpanded}
                  onToggle={() => setClawExpanded(v => !v)}
                />
                {clawExpanded && (
                  <div style={{ marginTop: 2 }}>
                    {clawSessions.length === 0 ? (
                      <div className="kim-empty-section">
                        No sessions — configure path in Settings
                      </div>
                    ) : (
                      clawSessions.map(s => (
                        <SessionItem
                          key={s.session_id}
                          session={s}
                          active={s.session_id === activeSessionId}
                          onClick={() => onSelectSession(s)}
                        />
                      ))
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {/* Bottom: settings */}
      <div className="kim-sidebar__bottom">
        <button
          onClick={onOpenSettings}
          title="Settings (⌘,)"
          className="kim-sidebar__settings-btn"
          aria-label="Settings"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.8 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
          </svg>
          {!collapsed && <span>Settings</span>}
        </button>
      </div>
    </aside>
  );
}
