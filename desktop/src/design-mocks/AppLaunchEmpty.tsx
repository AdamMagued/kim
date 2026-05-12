export interface AppLaunchProject {
  name: string;
  branch?: string;
}

export interface AppLaunchSession {
  title: string;
  when: string;
  messages: number;
}

export interface AppLaunchEmptyProps {
  userDisplayName?: string;
  userInitial?: string;
  version?: string;
  projects?: AppLaunchProject[];
  sessions?: AppLaunchSession[];
}

const defaultProjects: AppLaunchProject[] = [
  { name: "testing-grounds", branch: "main" },
  { name: "BoBos", branch: "main" },
  { name: "pong-arena", branch: "main" },
];

const defaultSessions: AppLaunchSession[] = [
  { title: "Reorg the file picker after onboarding", when: "9:42 AM", messages: 8 },
  { title: "Open cms.guc.edu.eg and check the inbox", when: "8:14 AM", messages: 12 },
  { title: "What's running on my screen right now?", when: "Yesterday", messages: 4 },
  { title: "Draft a polite reply to Sarah", when: "Yesterday", messages: 6 },
];

export function AppLaunchEmpty({
  userDisplayName = "adam",
  userInitial = "A",
  version = "v0.9.6",
  projects = defaultProjects,
  sessions = defaultSessions,
}: AppLaunchEmptyProps) {
  return (
    <div className="dm-root">
      <div className="dm-window">
        <aside className="dm-sidebar" aria-label="Kim sidebar">
          <div className="dm-brand">
            <div className="dm-kmark">K</div>
            <div className="dm-brand-title">
              <strong>kim · adam@local</strong>
              <span>{version}</span>
            </div>
          </div>

          <button className="dm-nav-button dm-primary">✦ New chat</button>
          <button className="dm-nav-button">Chat</button>
          <button className="dm-nav-button">Code</button>
          <div className="dm-sidebar-search">
            <span>Search sessions</span>
            <span className="dm-kbd">⌘K</span>
          </div>

          <div className="dm-sidebar-section">
            <div className="dm-section-label">projects</div>
            {projects.map((project) => (
              <div className="dm-project-row" key={project.name}>
                <span className="dm-project-name">{project.name}</span>
                <span className="dm-meta">{project.branch}</span>
              </div>
            ))}
          </div>

          <div className="dm-sidebar-section">
            <div className="dm-section-label">today · pong-arena</div>
            {sessions.map((session) => (
              <div className="dm-session-row" key={session.title}>
                <span className="dm-session-title">{session.title}</span>
                <span className="dm-session-meta">
                  {session.when} · {session.messages} msgs
                </span>
              </div>
            ))}
          </div>

          <div className="dm-profile">
            <div className="dm-avatar">{userInitial}</div>
            <div className="dm-brand-title">
              <strong>{userDisplayName}</strong>
              <span>local · ready</span>
            </div>
          </div>
        </aside>

        <main className="dm-main">
          <section className="dm-empty-wrap">
            <div className="dm-empty-card">
              <div className="dm-kmark" aria-hidden="true">K</div>
              <h1 className="dm-empty-title">Good morning, {userDisplayName}.</h1>
              <p className="dm-empty-subtitle">Pick up where you left off, or start fresh.</p>

              <div className="dm-action-row">
                <button className="dm-button dm-primary">New chat <span className="dm-kbd">⌘N</span></button>
                <button className="dm-button">New code session</button>
              </div>

              <div className="dm-recents">
                <div className="dm-section-label">recents</div>
                {sessions.slice(0, 3).map((session) => (
                  <div className="dm-recent-row" key={session.title}>
                    <strong>{session.title}</strong>
                    <span className="dm-meta">{session.when} · {session.messages} messages</span>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <footer className="dm-composer">
            <div className="dm-action-row">
              <span className="dm-scroll-hint">⌘N new chat</span>
              <span className="dm-scroll-hint">⌘K search</span>
              <span className="dm-scroll-hint">⌘, settings</span>
            </div>
          </footer>
        </main>
      </div>
    </div>
  );
}
