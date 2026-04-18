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
      style={{
        width: '100%',
        padding: '10px 12px',
        borderRadius: '8px',
        border: 'none',
        background: active ? 'var(--accent-muted)' : 'transparent',
        cursor: 'pointer',
        textAlign: 'left',
        transition: 'background 0.1s',
      }}
      onMouseEnter={e => {
        if (!active) (e.currentTarget as HTMLButtonElement).style.background = 'var(--bg-card)';
      }}
      onMouseLeave={e => {
        if (!active) (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
      }}
    >
      <div
        style={{
          fontSize: '13px',
          fontWeight: 500,
          color: active ? 'var(--accent)' : 'var(--text)',
          marginBottom: '2px',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {session.session_id}
      </div>
      <div
        style={{
          fontSize: '11px',
          color: 'var(--text-muted)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {preview}
      </div>
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
    <button
      onClick={onToggle}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '6px',
        width: '100%',
        padding: '6px 8px',
        borderRadius: '6px',
        border: 'none',
        background: 'transparent',
        cursor: 'pointer',
        color: 'var(--text-muted)',
        textAlign: 'left',
      }}
    >
      <span
        style={{
          fontSize: '10px',
          transition: 'transform 0.15s',
          transform: expanded ? 'rotate(90deg)' : 'none',
          display: 'inline-block',
        }}
      >
        ▶
      </span>
      <span style={{ fontSize: '11px', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em', flex: 1 }}>
        {label}
      </span>
      <span
        style={{
          fontSize: '10px',
          background: 'var(--bg-card)',
          borderRadius: '10px',
          padding: '1px 7px',
          color: 'var(--text-muted)',
        }}
      >
        {count}
      </span>
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

  const WIDTH = 280;

  return (
    <div
      style={{
        width: collapsed ? '48px' : `${WIDTH}px`,
        minWidth: collapsed ? '48px' : `${WIDTH}px`,
        background: 'var(--bg-sidebar)',
        borderRight: '1px solid var(--border)',
        display: 'flex',
        flexDirection: 'column',
        transition: 'width 0.2s ease, min-width 0.2s ease',
        overflow: 'hidden',
      }}
    >
      {/* Top bar */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          padding: '12px 8px',
          gap: '8px',
          borderBottom: '1px solid var(--border)',
        }}
      >
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          style={{
            width: '32px',
            height: '32px',
            borderRadius: '8px',
            border: 'none',
            background: 'transparent',
            cursor: 'pointer',
            color: 'var(--text-muted)',
            fontSize: '16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          {collapsed ? '»' : '«'}
        </button>

        {!collapsed && (
          <button
            onClick={onNewChat}
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: '6px',
              padding: '7px 12px',
              borderRadius: '8px',
              border: '1px solid var(--border)',
              background: 'var(--accent)',
              color: '#fff',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            <span>+</span>
            <span>New chat</span>
          </button>
        )}
      </div>

      {/* Session lists */}
      {!collapsed && (
        <div style={{ flex: 1, overflowY: 'auto', padding: '8px' }}>
          {loading ? (
            <div
              style={{
                padding: '20px',
                textAlign: 'center',
                color: 'var(--text-muted)',
                fontSize: '13px',
              }}
            >
              Loading sessions…
            </div>
          ) : (
            <>
              {/* Kim sessions */}
              <div style={{ marginBottom: '4px' }}>
                <SectionHeader
                  label="Kim"
                  count={kimSessions.length}
                  expanded={kimExpanded}
                  onToggle={() => setKimExpanded(v => !v)}
                />
                {kimExpanded && (
                  <div style={{ marginTop: '2px', paddingLeft: '4px' }}>
                    {kimSessions.length === 0 ? (
                      <div
                        style={{
                          padding: '8px 12px',
                          fontSize: '12px',
                          color: 'var(--text-muted)',
                        }}
                      >
                        No sessions yet
                      </div>
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
                  <div style={{ marginTop: '2px', paddingLeft: '4px' }}>
                    {clawSessions.length === 0 ? (
                      <div
                        style={{
                          padding: '8px 12px',
                          fontSize: '12px',
                          color: 'var(--text-muted)',
                        }}
                      >
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
      <div
        style={{
          padding: '8px',
          borderTop: '1px solid var(--border)',
          display: 'flex',
          justifyContent: collapsed ? 'center' : 'flex-start',
        }}
      >
        <button
          onClick={onOpenSettings}
          title="Settings"
          style={{
            width: collapsed ? '32px' : '100%',
            height: '34px',
            borderRadius: '8px',
            border: 'none',
            background: 'transparent',
            cursor: 'pointer',
            color: 'var(--text-muted)',
            fontSize: '15px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: collapsed ? 'center' : 'flex-start',
            gap: '8px',
            padding: collapsed ? '0' : '0 8px',
          }}
        >
          <span>⚙️</span>
          {!collapsed && (
            <span style={{ fontSize: '13px' }}>Settings</span>
          )}
        </button>
      </div>
    </div>
  );
}
