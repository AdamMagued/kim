export interface FullWindowMessage {
  text: string;
}

export interface FullWindowShellProps {
  userDisplayName?: string;
  userInitial?: string;
  activeProject?: string;
  prompt?: string;
  finalText?: string;
}

const projectGroups = [
  { name: "testing", count: 4 },
  { name: "BoBos", count: 2 },
  { name: "pong-tower", count: 30, open: true },
  { name: "design-system", count: 6 },
];

const childSessions = [
  ["Built tower simulation", "2m"],
  ["Pong CPU difficulty", "14m"],
  ["Neon physics polish", "1h"],
  ["Paddle hit detection", "3h"],
  ["Refactor render loop", "yday"],
];

export function FullWindowShell({
  userDisplayName = "adam",
  userInitial = "A",
  activeProject = "pong-tower",
  prompt = "can u add a CPU opponent w/ difficulty levels to the pong game, and also build me a tower physics sim with 5 balls?",
  finalText = "Done. I updated pong.html with a CPU opponent (Easy / Medium / Hard) and a local PvP mode, and created tower_sim.html where 5 colored balls race down a tower of rotating platforms. Open either in your browser to play.",
}: FullWindowShellProps) {
  return (
    <div className="dm-root">
      <div className="dm-window">
        <aside className="dm-sidebar">
          <div className="dm-brand">
            <div className="dm-kmark">K</div>
            <div className="dm-brand-title"><strong>Kim</strong><span>v0.9.6</span></div>
          </div>
          <button className="dm-nav-button dm-primary">New chat</button>
          <div className="dm-sidebar-search"><span>Search…</span><span className="dm-kbd">⌘K</span></div>
          <button className="dm-nav-button">Chat</button>
          <button className="dm-nav-button">Code</button>
          <div className="dm-sidebar-section">
            <div className="dm-section-label">projects <span className="dm-badge">+</span></div>
            {projectGroups.map((project) => (
              <div key={project.name}>
                <div className="dm-project-row">
                  <span>{project.open ? "▾" : "▸"} {project.name}</span>
                  <span className="dm-badge">{project.count}</span>
                </div>
                {project.name === activeProject && (
                  <div className="dm-sidebar-section" style={{ paddingLeft: 16 }}>
                    {childSessions.map(([name, time]) => (
                      <div className="dm-session-row" key={name}><span className="dm-session-title">{name}</span><span className="dm-session-meta">{time}</span></div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="dm-profile"><div className="dm-avatar">{userInitial}</div><div className="dm-brand-title"><strong>{userDisplayName}</strong><span>▾</span></div></div>
        </aside>

        <main className="dm-main">
          <header className="dm-topbar">
            <div className="dm-topbar-title"><span className="dm-code-label">CODE</span><strong>session-1778461238826-0</strong></div>
            <button className="dm-button">Generate summary</button>
          </header>

          <section className="dm-chat-scroll">
            <div className="dm-chat-column">
              <div className="dm-user-bubble">{prompt}</div>
              <div className="dm-thinking-block">
                <div className="dm-rail-icon">›</div>
                <div className="dm-thinking-content">
                  <div className="dm-thinking-header"><strong className="dm-thinking-word">Thinking</strong><span>0:23 · 8 steps</span></div>
                  <div className="dm-thinking-steps">
                    <div className="dm-step"><span>›</span><span>Two candidates in the directory — pong.html and index.html. Let me figure out which is canonical.</span></div>
                    <div className="dm-step"><span>›</span><span><strong>Running</strong> find . -maxdepth 2</span></div>
                    <div className="dm-step"><span>›</span><span><strong>Reading</strong> pong.html</span></div>
                    <div className="dm-step"><span>›</span><span><strong>Reading</strong> index.html</span></div>
                    <div className="dm-step dm-running"><span>›</span><span>pong.html is the polished one. Here&apos;s my plan before I start editing:</span></div>
                  </div>
                </div>
              </div>

              <section className="dm-plan-card">
                <div className="dm-plan-header"><h3>Plan</h3><span className="dm-plan-progress">3 / 6</span></div>
                {[
                  "Wire a CPU opponent into pong.html",
                  "Add Easy / Medium / Hard difficulty switches",
                  "Add a local PvP toggle",
                  "Build tower_sim.html with 5 balls",
                  "Add rotating platforms + collision",
                  "Tune ball physics so the race is interesting",
                ].map((title, index) => (
                  <div className={`dm-plan-item ${index < 3 ? "dm-done" : index === 3 ? "dm-running" : "dm-todo"}`} key={title}>
                    <span className="dm-plan-dot">{index < 3 ? "✓" : ""}</span>
                    <span className="dm-plan-title">{title}</span><span />
                  </div>
                ))}
              </section>

              <p className="dm-stream-text"><span className="dm-stream-caret" /> Pong + PvP wired up cleanly. Onto the tower sim — 5 colored balls dropping through rotating platforms.</p>
              <div className="dm-step dm-running"><span>›</span><span><strong>Writing</strong> tower_sim.html</span></div>
              <div className="dm-panel" style={{ padding: 18 }}>
                <p className="dm-stream-text">{finalText}</p>
                <div className="dm-file-grid">
                  <div className="dm-file-row"><strong>pong.html</strong><span className="dm-inline"><span className="dm-diff-plus">+578</span><span className="dm-diff-minus">-858</span></span></div>
                  <div className="dm-file-row"><strong>index.html</strong><span className="dm-meta">linked</span></div>
                  <div className="dm-file-row"><strong>tower_sim.html</strong><span className="dm-meta">created</span></div>
                </div>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
