import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { open as openDialog } from '@tauri-apps/plugin-dialog';
import type { SessionInfo, KimAccount, ClawProject } from '../types';

interface Props {
  kimSessions: SessionInfo[];
  activeSessionId: string | null;
  onSelectSession: (session: SessionInfo) => void;
  onNewChat: () => void;
  collapsed: boolean;
  onToggle: () => void;
  onOpenSettings: () => void;
  loading: boolean;
  account: KimAccount;
  onAccountChange: (a: KimAccount) => Promise<void>;
  activeTab: 'chat' | 'code';
  onTabChange: (tab: 'chat' | 'code') => void;
  activeProjectPath: string | null;
  onSelectProject: (path: string) => void;
  onRefreshSessions: () => void;
  kimSessionsDir: string | null;
  clawSessionsDir: string | null;
}

// ── Icons ──────────────────────────────────────────────────────────────────────

function ChatBubbleIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6l-4 3V5z" />
    </svg>
  );
}
function CodeBracketIcon() {
  return (
    <svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 7l-4 3 4 3M13 7l4 3-4 3M11 4l-2 12" />
    </svg>
  );
}
function ComposeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.5 2.5a2.121 2.121 0 0 1 3 3L6 17l-4 1 1-4 11.5-11.5z" />
    </svg>
  );
}
function FolderIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 4.5A1.5 1.5 0 0 1 2.5 3h3l1.5 2H13.5A1.5 1.5 0 0 1 15 6.5v6A1.5 1.5 0 0 1 13.5 14h-11A1.5 1.5 0 0 1 1 12.5v-8z" />
    </svg>
  );
}
function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
      style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.12s ease', flexShrink: 0 }}>
      <path d="M5 3l5 5-5 5" />
    </svg>
  );
}
function SettingsIcon() {
  return (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9c.2.6.8 1 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  );
}
function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M8 3v10M3 8h10" />
    </svg>
  );
}
function XIcon() {
  return (
    <svg viewBox="0 0 16 16" width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M4 4l8 8M12 4l-8 8" />
    </svg>
  );
}

// ── Session item ───────────────────────────────────────────────────────────────

function SessionItem({ session, active, onClick, editMode, selected, onToggleSelect }: {
  session: SessionInfo; active: boolean; onClick: () => void;
  editMode?: boolean; selected?: boolean; onToggleSelect?: () => void;
}) {
  const chatTitle = session.title?.trim() || session.session_id;
  let summaryText = session.summary || '';
  if (summaryText) {
    const match = summaryText.match(/^Task:.*?(?:\.\s*Result:\s*|\nResult:\s*)([\s\S]*)$/i);
    if (match) {
      summaryText = match[1].trim();
    }
  }

  const preview = summaryText
    ? summaryText.slice(0, 60) + (summaryText.length > 60 ? '…' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;
  
  if (editMode) {
    return (
      <div className={`kim-session-item kim-session-item--edit${selected ? ' kim-session-item--selected' : ''}`} onClick={onToggleSelect} style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8, paddingRight: 8 }}>
        <input type="checkbox" checked={selected} onChange={() => {}} style={{ cursor: 'pointer' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="kim-session-item__title">{chatTitle}</div>
          <div className="kim-session-item__preview">{preview}</div>
        </div>
      </div>
    );
  }

  return (
    <button
      onClick={onClick}
      title={session.summary ?? chatTitle}
      className={`kim-session-item${active ? ' kim-session-item--active' : ''}`}
    >
      <div className="kim-session-item__title">{chatTitle}</div>
      <div className="kim-session-item__preview">{preview}</div>
    </button>
  );
}

// ── Claw session item (no onSelectSession needed yet) ─────────────────────────

function ClawSessionItem({ session }: { session: { session_id: string; message_count: number; summary?: string | null } }) {
  const preview = session.summary
    ? session.summary.slice(0, 55) + (session.summary.length > 55 ? '…' : '')
    : `${session.message_count} message${session.message_count !== 1 ? 's' : ''}`;
  return (
    <div className="kim-session-item" title={session.summary ?? session.session_id}>
      <div className="kim-session-item__title">{session.session_id.slice(0, 18)}</div>
      <div className="kim-session-item__preview">{preview}</div>
    </div>
  );
}

// ── Project tree in Code tab ───────────────────────────────────────────────────

function ClawProjectTree({ project, onRemove, isActive, onSelect }: {
  project: ClawProject;
  onRemove: (path: string) => void;
  isActive: boolean;
  onSelect: () => void;
}) {
  const [open, setOpen] = useState(isActive);
  const totalSessions = project.branches.reduce((n, b) => n + b.sessions.length, 0);

  return (
    <div className={`kim-project-item${isActive ? ' kim-project-item--active' : ''}`}>
      <div className="kim-project-item__header" onClick={() => { onSelect(); setOpen(true); }} style={{ cursor: 'pointer' }}>
        <button className="kim-project-item__toggle" onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}>
          <ChevronIcon open={open} />
        </button>
        <span className="kim-project-item__icon"><FolderIcon /></span>
        <span className="kim-project-item__name">{project.name}</span>
        <span className="kim-project-item__branch-pill">{project.current_branch}</span>
        <button
          className="kim-project-item__remove"
          onClick={() => onRemove(project.path)}
          title="Remove project"
          aria-label="Remove project"
        >
          <XIcon />
        </button>
      </div>

      {open && (
        <div className="kim-project-item__sessions">
          {totalSessions === 0 ? (
            <div className="kim-empty-section" style={{ paddingLeft: 28 }}>
              No Claw sessions yet.
            </div>
          ) : (
            project.branches.map(branch => (
              <div key={branch.name}>
                {project.branches.length > 1 && (
                  <div className="kim-branch-label">{branch.name}</div>
                )}
                {branch.sessions.map(s => (
                  <div key={s.session_id} style={{ paddingLeft: 12 }}>
                    <ClawSessionItem session={s} />
                  </div>
                ))}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Add project form ───────────────────────────────────────────────────────────

/** Opens the native folder picker and immediately adds the selected path. */
async function pickAndAddProject(onAdd: (path: string) => Promise<void>, setErr: (e: string) => void, setAdding: (b: boolean) => void) {
  try {
    const selected = await openDialog({
      directory: true,
      multiple: false,
      title: 'Select project folder',
    });
    if (typeof selected === 'string' && selected) {
      setAdding(true);
      setErr('');
      try {
        await onAdd(selected);
      } catch (e) {
        setErr(String(e));
      } finally {
        setAdding(false);
      }
    }
  } catch (e) {
    setErr(String(e));
  }
}

// ── Main Sidebar ───────────────────────────────────────────────────────────────

export function Sidebar({
  kimSessions, activeSessionId,
  onSelectSession, onNewChat,
  collapsed, onToggle, onOpenSettings, loading,
  account, onAccountChange, activeTab, onTabChange,
  activeProjectPath, onSelectProject, onRefreshSessions,
  kimSessionsDir, clawSessionsDir,
}: Props) {
  const [kimExpanded, setKimExpanded] = useState(true);
  const [clawProjects, setClawProjects] = useState<ClawProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsAdding, setProjectsAdding] = useState(false);
  const [projectsErr, setProjectsErr] = useState('');
  
  const [editMode, setEditMode] = useState(false);
  const [selectedSessions, setSelectedSessions] = useState<Set<string>>(new Set());
  const [deleteConfirmStep, setDeleteConfirmStep] = useState<0 | 1 | 2>(0); // 0=hidden, 1=first confirm, 2=final confirm
  const [deleting, setDeleting] = useState(false);

  const projectPaths = account.code_projects ?? [];

  const loadProjects = useCallback(() => {
    if (projectPaths.length === 0) {
      setClawProjects([]);
      return;
    }
    setProjectsLoading(true);
    invoke<ClawProject[]>('list_claw_projects', { projectPaths })
      .then(p => setClawProjects(p))
      .catch(() => setClawProjects([]))
      .finally(() => setProjectsLoading(false));
  }, [JSON.stringify(projectPaths)]);

  useEffect(() => {
    if (activeTab === 'code') loadProjects();
  }, [activeTab, loadProjects]);

  async function handleAddProject(path: string) {
    const newPaths = await invoke<string[]>('add_code_project', { path });
    await onAccountChange({ ...account, code_projects: newPaths });
  }

  function handlePickProject() {
    pickAndAddProject(handleAddProject, setProjectsErr, setProjectsAdding);
  }

  async function handleRemoveProject(path: string) {
    const newPaths = await invoke<string[]>('remove_code_project', { path });
    await onAccountChange({ ...account, code_projects: newPaths });
    setClawProjects(prev => prev.filter(p => p.path !== path));
  }

  const initials = account.display_name
    .split(' ').map((w: string) => w[0]).join('').slice(0, 2).toUpperCase();

  return (
    <>
    <aside className={`kim-sidebar${collapsed ? ' kim-sidebar--collapsed' : ''}`}>
      {/* Top controls */}
      <div className="kim-sidebar__top">
        <button
          onClick={onToggle}
          title={collapsed ? 'Expand (Cmd+B)' : 'Collapse (Cmd+B)'}
          className="kim-sidebar__toggle"
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            {collapsed ? <path d="M6 3l5 5-5 5" /> : <path d="M10 3l-5 5 5 5" />}
          </svg>
        </button>
        {!collapsed && (
          <button onClick={onNewChat} className="kim-sidebar__new-chat" title="New chat (Cmd+N)">
            <ComposeIcon />
            <span>New chat</span>
          </button>
        )}
      </div>

      {/* Tab bar */}
      {!collapsed && (
        <div className="kim-sidebar__tabs">
          <div className="kim-tab-bar">
            <button
              className={`kim-tab${activeTab === 'chat' ? ' kim-tab--active' : ''}`}
              onClick={() => onTabChange('chat')}
            >
              <ChatBubbleIcon /><span>Chat</span>
            </button>
            <button
              className={`kim-tab${activeTab === 'code' ? ' kim-tab--active' : ''}`}
              onClick={() => onTabChange('code')}
            >
              <CodeBracketIcon /><span>Code</span>
            </button>
          </div>
        </div>
      )}

      {/* Scrollable content */}
      {!collapsed && (
        <div className="kim-sidebar__scroll">
          {loading ? (
            <div className="kim-sidebar__loading">
              {[32, 28, 36, 28].map((h, i) => (
                <div key={i} className="kim-skeleton" style={{ height: h, marginBottom: 4 }} />
              ))}
            </div>
          ) : activeTab === 'chat' ? (
            // ── Chat tab ──
            <div>
              <div className="kim-section-header" style={{ display: 'flex', alignItems: 'center' }}>
                <button style={{ flex: 1, display: 'flex', alignItems: 'center', background: 'none', border: 'none', color: 'inherit', padding: 0, font: 'inherit', cursor: 'pointer' }} onClick={() => setKimExpanded(v => !v)}>
                  <ChevronIcon open={kimExpanded} />
                  <span className="kim-section-header__label">Kim</span>
                  <span className="kim-section-header__count">{kimSessions.length}</span>
                </button>
                {kimSessions.length > 0 && (
                  <button 
                    className={editMode ? 'kim-action-btn' : 'kim-action-btn'}
                    style={{ 
                      fontSize: 11, 
                      padding: '3px 10px',
                      background: editMode ? 'var(--accent)' : 'var(--bg-card)',
                      color: editMode ? '#fff' : 'var(--text-muted)',
                      border: `1px solid ${editMode ? 'var(--accent)' : 'var(--border)'}`,
                      borderRadius: 6,
                    }} 
                    onClick={() => { setEditMode(!editMode); setSelectedSessions(new Set()); }}
                  >
                    {editMode ? 'Done' : 'Edit'}
                  </button>
                )}
              </div>
              
              {editMode && selectedSessions.size > 0 && (
                <div style={{ padding: '4px 8px 8px', display: 'flex', gap: 6 }}>
                  <button 
                    className="kim-action-btn kim-action-btn--danger"
                    style={{ flex: 1, fontSize: 12, padding: '7px 12px' }}
                    onClick={() => setDeleteConfirmStep(1)}
                  >
                    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M2 4h12M5.333 4V2.667a1.333 1.333 0 0 1 1.334-1.334h2.666a1.333 1.333 0 0 1 1.334 1.334V4M13 4v9.333a1.333 1.333 0 0 1-1.333 1.334H4.333A1.333 1.333 0 0 1 3 13.333V4" />
                    </svg>
                    Delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}
                  </button>
                </div>
              )}
              {kimExpanded && (
                <div style={{ marginTop: 1 }}>
                  {kimSessions.length === 0 ? (
                    <div className="kim-empty-section">No sessions yet</div>
                  ) : (
                    kimSessions.map(s => (
                      <SessionItem
                        key={s.session_id}
                        session={s}
                        active={s.session_id === activeSessionId}
                        onClick={() => onSelectSession(s)}
                        editMode={editMode}
                        selected={selectedSessions.has(s.session_id)}
                        onToggleSelect={() => {
                          const next = new Set(selectedSessions);
                          if (next.has(s.session_id)) next.delete(s.session_id);
                          else next.add(s.session_id);
                          setSelectedSessions(next);
                        }}
                      />
                    ))
                  )}
                </div>
              )}
            </div>
          ) : (
            // ── Code tab ──
            <div>
              <div className="kim-code-tab-header">
                <span className="kim-section-header__label" style={{ flex: 1 }}>Projects</span>
                <button
                  className="kim-code-tab-add-btn"
                  onClick={handlePickProject}
                  disabled={projectsAdding}
                  title="Add project folder"
                >
                  {projectsAdding ? '…' : <PlusIcon />}
                </button>
              </div>

              {projectsErr && (
                <div className="kim-add-project-error" style={{ padding: '0 8px 6px' }}>{projectsErr}</div>
              )}

              {projectPaths.length === 0 ? (
                <div className="kim-empty-section" style={{ marginTop: 8 }}>
                  <div style={{ marginBottom: 8 }}>No projects added yet.</div>
                  <button className="kim-link-btn" onClick={handlePickProject} disabled={projectsAdding}>
                    {projectsAdding ? 'Selecting…' : 'Choose a project folder'}
                  </button>
                </div>
              ) : projectsLoading ? (
                <div className="kim-sidebar__loading">
                  <div className="kim-skeleton" style={{ height: 36 }} />
                  <div className="kim-skeleton" style={{ height: 36 }} />
                </div>
              ) : (
                // Merge: show all added project paths, use loaded project data where available
                projectPaths.map(path => {
                  const loaded = clawProjects.find(p => p.path === path);
                  if (loaded) {
                    return (
                      <ClawProjectTree
                        key={path}
                        project={loaded}
                        onRemove={handleRemoveProject}
                        isActive={activeProjectPath === path}
                        onSelect={() => onSelectProject(path)}
                      />
                    );
                  }
                  // Project path exists but no .claw/sessions/ yet
                  const name = path.split('/').filter(Boolean).pop() ?? path;
                  return (
                    <div key={path} className={`kim-project-item${activeProjectPath === path ? ' kim-project-item--active' : ''}`}>
                      <div className="kim-project-item__header" onClick={() => onSelectProject(path)} style={{ cursor: 'pointer' }}>
                        <button className="kim-project-item__toggle" style={{ flex: 1, cursor: 'default' }}>
                          <span className="kim-project-item__icon"><FolderIcon /></span>
                          <span className="kim-project-item__name">{name}</span>
                        </button>
                        <button
                          className="kim-project-item__remove"
                          onClick={() => handleRemoveProject(path)}
                          title="Remove project"
                        >
                          <XIcon />
                        </button>
                      </div>
                      <div className="kim-empty-section" style={{ paddingLeft: 28, fontSize: 11 }}>
                        No Claw sessions yet
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}

      {/* Bottom: account + settings */}
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
        <button
          onClick={onOpenSettings}
          title="Settings (Cmd+,)"
          className="kim-sidebar__settings-btn"
          aria-label="Settings"
        >
          <SettingsIcon />
          {!collapsed && <span>Settings</span>}
        </button>
      </div>
    </aside>

      {/* ── Delete confirmation modal ── */}
      {deleteConfirmStep > 0 && (
        <div className="kim-confirm-overlay" onClick={() => setDeleteConfirmStep(0)}>
          <div className="kim-confirm-dialog" onClick={e => e.stopPropagation()}>
            <div className="kim-confirm-dialog__icon">
              <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="var(--danger)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
              </svg>
            </div>
            {deleteConfirmStep === 1 ? (
              <>
                <div className="kim-confirm-dialog__title">Delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}?</div>
                <div className="kim-confirm-dialog__text">This action cannot be undone. The selected chat{selectedSessions.size > 1 ? 's' : ''} will be permanently removed.</div>
                <div className="kim-confirm-dialog__actions">
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--cancel" onClick={() => setDeleteConfirmStep(0)}>Cancel</button>
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--danger" onClick={() => setDeleteConfirmStep(2)}>Yes, delete</button>
                </div>
              </>
            ) : (
              <>
                <div className="kim-confirm-dialog__title">Are you absolutely sure?</div>
                <div className="kim-confirm-dialog__text">This will <strong>permanently delete {selectedSessions.size} chat{selectedSessions.size > 1 ? 's' : ''}</strong>. There is no way to recover them.</div>
                <div className="kim-confirm-dialog__actions">
                  <button className="kim-confirm-dialog__btn kim-confirm-dialog__btn--cancel" onClick={() => setDeleteConfirmStep(0)}>No, keep them</button>
                  <button 
                    className="kim-confirm-dialog__btn kim-confirm-dialog__btn--danger" 
                    disabled={deleting}
                    onClick={async () => {
                      setDeleting(true);
                      try {
                        await invoke('delete_sessions', { 
                          sessionIds: Array.from(selectedSessions),
                          kimDir: kimSessionsDir,
                          clawDir: clawSessionsDir,
                        });
                        setEditMode(false);
                        setSelectedSessions(new Set());
                        setDeleteConfirmStep(0);
                        onRefreshSessions();
                      } catch (e) {
                        alert(`Failed to delete: ${e}`);
                      } finally {
                        setDeleting(false);
                      }
                    }}
                  >{deleting ? 'Deleting…' : 'Delete permanently'}</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
