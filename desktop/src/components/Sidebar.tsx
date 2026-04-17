import { useState, useEffect } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, KimAccount, ClawProject } from '../types';

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
  account: KimAccount;
  activeTab: 'chat' | 'code';
  onTabChange: (tab: 'chat' | 'code') => void;
  clawSessionsDir?: string;
}

function ChatBubbleIcon() {
  return (
    <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6l-4 3V5z" />
    </svg>
  );
}
function CodeBracketIcon() {
  return (
    <svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 7l-4 3 4 3M13 7l4 3-4 3M11 4l-2 12" />
    </svg>
  );
}
function ComposeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.5 2.5a2.121 2.121 0 0 1 3 3L6 17l-4 1 1-4 11.5-11.5z" />
    </svg>
  );
}
function FolderIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor">
      <path d="M1.5 3A1.5 1.5 0 0 0 0 4.5v8A1.5 1.5 0 0 0 1.5 14h13a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 14.5 4H8.4l-1.2-1.6A1.5 1.5 0 0 0 6 2H1.5z" />
    </svg>
  );
}
function ChevronSmall({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
      style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s', flexShrink: 0 }}>
      <path d="M5 3l5 5-5 5" />
    </svg>
  );
}
function SettingsIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.8 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  );
}

function SessionItem({ session, active, onClick }: { session: SessionInfo; active: boolean; onClick: () => void }) {
  const preview = session.summary
    ? session.summary.slice(0, 72) + (session.summary.length > 72 ? '...' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;
  return (
    <button onClick={onClick} title={session.summary ?? session.session_id}
      className={`kim-session-item${active ? ' kim-session-item--active' : ''}`}>
      <div className="kim-session-item__title">{session.session_id}</div>
      <div className="kim-session-item__preview">{preview}</div>
    </button>
  );
}

function SectionHeader({ label, count, expanded, onToggle }: { label: string; count: number; expanded: boolean; onToggle: () => void }) {
  return (
    <button onClick={onToggle} className="kim-section-header">
      <ChevronSmall open={expanded} />
      <span className="kim-section-header__label">{label}</span>
      <span className="kim-section-header__count">{count}</span>
    </button>
  );
}

function ClawProjectTree({ project }: { project: ClawProject }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="kim-project-item">
      <button className="kim-project-item__header" onClick={() => setOpen(o => !o)}>
        <span className="kim-project-item__icon"><FolderIcon /></span>
        <span className="kim-project-item__name">{project.name}</span>
        <span className="kim-project-item__branch-pill">{project.current_branch}</span>
        <ChevronSmall open={open} />
      </button>
      {open && (
        <div className="kim-project-item__sessions">
          {project.branches.map(branch => (
            <div key={branch.name}>
              {project.branches.length > 1 && (
                <div style={{ padding: '4px 8px', fontSize: 11, color: 'var(--text-subtle)', fontFamily: 'monospace' }}>
                  {branch.name}
                </div>
              )}
              {branch.sessions.map(s => (
                <button key={s.session_id} className="kim-session-item" title={s.summary ?? s.session_id}>
                  <div className="kim-session-item__title">{s.session_id.slice(0, 20)}</div>
                  <div className="kim-session-item__preview">{s.summary ?? `${s.message_count} messages`}</div>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Sidebar({
  kimSessions, clawSessions, activeSessionId,
  onSelectSession, onNewChat,
  collapsed, onToggle, onOpenSettings, loading,
  account, activeTab, onTabChange, clawSessionsDir,
}: Props) {
  const [kimExpanded, setKimExpanded] = useState(true);
  const [clawExpanded, setClawExpanded] = useState(true);
  const [clawProjects, setClawProjects] = useState<ClawProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);

  useEffect(() => {
    if (activeTab !== 'code') return;
    setProjectsLoading(true);
    invoke<ClawProject[]>('list_claw_projects', { clawDir: clawSessionsDir || null })
      .then(p => setClawProjects(p))
      .catch(() => setClawProjects([]))
      .finally(() => setProjectsLoading(false));
  }, [activeTab, clawSessionsDir]);

  const initials = account.display_name
    .split(' ').map((w: string) => w[0]).join('').slice(0, 2).toUpperCase();

  return (
    <aside className={`kim-sidebar${collapsed ? ' kim-sidebar--collapsed' : ''}`}>
      <div className="kim-sidebar__top">
        <button onClick={onToggle} title={collapsed ? 'Expand (Cmd+B)' : 'Collapse (Cmd+B)'}
          className="kim-sidebar__toggle" aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {collapsed ? <path d="M6 3l5 5-5 5" /> : <path d="M10 3l-5 5 5 5" />}
          </svg>
        </button>
        {!collapsed && (
          <button onClick={onNewChat} className="kim-sidebar__new-chat" title="New chat (Cmd+N)">
            <ComposeIcon /><span>New chat</span>
          </button>
        )}
      </div>

      {!collapsed && (
        <div style={{ padding: '8px 10px 4px' }}>
          <div className="kim-tab-bar">
            <button className={`kim-tab${activeTab === 'chat' ? ' kim-tab--active' : ''}`} onClick={() => onTabChange('chat')}>
              <ChatBubbleIcon /><span>Chat</span>
            </button>
            <button className={`kim-tab${activeTab === 'code' ? ' kim-tab--active' : ''}`} onClick={() => onTabChange('code')}>
              <CodeBracketIcon /><span>Code</span>
            </button>
          </div>
        </div>
      )}

      {!collapsed && (
        <div className="kim-sidebar__scroll">
          {loading ? (
            <div className="kim-sidebar__loading">
              <div className="kim-skeleton" style={{ height: 32 }} />
              <div className="kim-skeleton" style={{ height: 32 }} />
              <div className="kim-skeleton" style={{ height: 32 }} />
            </div>
          ) : activeTab === 'chat' ? (
            <div style={{ marginBottom: 4 }}>
              <SectionHeader label="Kim" count={kimSessions.length} expanded={kimExpanded} onToggle={() => setKimExpanded(v => !v)} />
              {kimExpanded && (
                <div style={{ marginTop: 2 }}>
                  {kimSessions.length === 0
                    ? <div className="kim-empty-section">No sessions yet</div>
                    : kimSessions.map(s => (
                        <SessionItem key={s.session_id} session={s}
                          active={s.session_id === activeSessionId} onClick={() => onSelectSession(s)} />
                      ))}
                </div>
              )}
            </div>
          ) : (
            <>
              {projectsLoading ? (
                <div className="kim-sidebar__loading">
                  <div className="kim-skeleton" style={{ height: 40 }} />
                  <div className="kim-skeleton" style={{ height: 40 }} />
                </div>
              ) : clawProjects.length > 0 ? (
                clawProjects.map(p => <ClawProjectTree key={p.path} project={p} />)
              ) : (
                <>
                  <div className="kim-empty-section" style={{ marginBottom: 8 }}>
                    No projects found in ~/.claude/projects
                  </div>
                  {clawSessions.length > 0 && (
                    <>
                      <SectionHeader label="Sessions" count={clawSessions.length}
                        expanded={clawExpanded} onToggle={() => setClawExpanded(v => !v)} />
                      {clawExpanded && clawSessions.map(s => (
                        <SessionItem key={s.session_id} session={s}
                          active={s.session_id === activeSessionId} onClick={() => onSelectSession(s)} />
                      ))}
                    </>
                  )}
                </>
              )}
            </>
          )}
        </div>
      )}

      <div className="kim-sidebar__bottom">
        {!collapsed && (
          <div className="kim-account-chip">
            <div className="kim-account-chip__avatar">
              {account.github_avatar_url
                ? <img src={account.github_avatar_url} alt={account.display_name} />
                : initials}
            </div>
            <div className="kim-account-chip__info">
              <div className="kim-account-chip__name">{account.display_name}</div>
              {account.github_username && (
                <div className="kim-account-chip__sub">@{account.github_username}</div>
              )}
            </div>
          </div>
        )}
        <button onClick={onOpenSettings} title="Settings (Cmd+,)" className="kim-sidebar__settings-btn" aria-label="Settings">
          <SettingsIcon />
          {!collapsed && <span>Settings</span>}
        </button>
      </div>
    </aside>
  );
}
