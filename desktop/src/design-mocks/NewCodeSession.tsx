export interface CodeSessionProject {
  name: string;
  branch?: string;
}

export interface CodeSessionRecent {
  title: string;
  when: string;
  messages: number;
}

export interface NewCodeSessionProps {
  userDisplayName?: string;
  userInitial?: string;
  version?: string;
  projectPath?: string;
  browserStatus?: string;
  placeholder?: string;
  projects?: CodeSessionProject[];
  recents?: CodeSessionRecent[];
}

const defaultProjects: CodeSessionProject[] = [
  { name: "testing-grounds", branch: "main" },
  { name: "BoBos", branch: "main" },
  { name: "pong-arena", branch: "main" },
];

const defaultRecents: CodeSessionRecent[] = [
  { title: "Add PvP mode + tower sim", when: "9:42 AM", messages: 30 },
  { title: "Audit the unused dependencies", when: "8:14 AM", messages: 3 },
  { title: "Wire up websocket reconnect logic", when: "Yesterday", messages: 19 },
];

export function NewCodeSession({
  userDisplayName = "adam",
  userInitial = "A",
  version = "v0.9.6",
  projectPath = "~/code/pong-arena",
  browserStatus = "Browser: Claude · ready",
  placeholder = "Add a CPU opponent to pong.html with three difficulty levels…",
  projects = defaultProjects,
  recents = defaultRecents,
}: NewCodeSessionProps) {
  return (
    <div className="dm-root">
      <div className="dm-window">
        <aside className="dm-sidebar">
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
          <div className="dm-sidebar-search"><span>Search sessions</span><span className="dm-kbd">⌘K</span></div>

          <div className="dm-sidebar-section">
            <div className="dm-section-label">projects</div>
            {projects.map((project) => (
              <div className="dm-project-row" key={project.name}>
                <span>{project.name}</span>
                <span className="dm-meta">{project.branch}</span>
              </div>
            ))}
          </div>

          <div className="dm-sidebar-section">
            <div className="dm-section-label">today · pong-arena</div>
            {recents.map((recent) => (
              <div className="dm-session-row" key={recent.title}>
                <span className="dm-session-title">{recent.title}</span>
                <span className="dm-session-meta">{recent.when} · {recent.messages} msgs</span>
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
          <section className="dm-code-wrap">
            <div className="dm-code-card">
              <div className="dm-card-header">
                <div>
                  <div className="dm-code-label">CODE</div>
                  <h1 className="dm-empty-title" style={{ fontSize: 36, marginTop: 8 }}>New session</h1>
                </div>
                <span className="dm-status-pill">{browserStatus}</span>
              </div>

              <div className="dm-path-box">{projectPath}</div>

              <div className="dm-panel" style={{ padding: 18 }}>
                <div className="dm-card-header">
                  <h3>What are we building?</h3>
                  <span className="dm-kbd">⌘↵</span>
                </div>
                <p className="dm-muted">Describe a feature, hand me a bug, or point me at a file. I&apos;ll read first, plan, then write.</p>
                <div className="dm-prompt-box">{placeholder}</div>
              </div>

              <div className="dm-mode-grid">
                <div className="dm-mode-card"><strong>⌥</strong>Plan first, then execute</div>
                <div className="dm-mode-card"><strong>⇧</strong>Read-only review</div>
                <div className="dm-mode-card"><strong>⌃</strong>Edit + run tests</div>
                <div className="dm-mode-card"><strong>✦</strong>Pick up last task</div>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
