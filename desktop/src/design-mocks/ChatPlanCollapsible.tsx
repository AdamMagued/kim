export interface ChatPlanStep {
  title: string;
  status: "done" | "running" | "todo";
  meta?: string;
}

export interface ChatFileChange {
  file: string;
  added: number;
  removed: number;
}

export interface ChatPlanCollapsibleProps {
  userDisplayName?: string;
  userInitial?: string;
  title?: string;
  prompt?: string;
  thinkingElapsed?: string;
  thinkingSteps?: string[];
  planSteps?: ChatPlanStep[];
  streamUpdate?: string;
  fileChanges?: ChatFileChange[];
}

const defaultThinkingSteps = [
  "Two candidates in the directory — pong.html and index.html. Let me figure out which is canonical.",
  "Running find . -maxdepth 2",
  "Reading pong.html",
  "Reading index.html",
  "pong.html is the polished one. Here's my plan before I start editing:",
];

const defaultPlanSteps: ChatPlanStep[] = [
  { title: "Find both Pong files and pick the canonical one", status: "done" },
  { title: "Add CPU opponent with Easy / Medium / Hard scaling", status: "done" },
  { title: "Wire up local PvP mode (shared keyboard)", status: "done" },
  { title: "Scaffold tower_sim.html with 5 balls + rotating platforms", status: "running", meta: "now" },
  { title: "Add scoring + finish-line detection", status: "todo" },
  { title: "Cross-link both files from index.html", status: "todo" },
];

const defaultFileChanges: ChatFileChange[] = [
  { file: "pong.html", added: 578, removed: 858 },
  { file: "tower_sim.html", added: 268, removed: 134 },
];

export function ChatPlanCollapsible({
  userDisplayName = "adam",
  userInitial = "A",
  title = "Add PvP + tower sim to pong-arena",
  prompt = "Build a CPU opponent into pong.html with three difficulty levels, plus local PvP. Then scaffold a tower_sim with five balls and rotating platforms.",
  thinkingElapsed = "0:23 · 8 steps",
  thinkingSteps = defaultThinkingSteps,
  planSteps = defaultPlanSteps,
  streamUpdate = "Pong is wired up — CPU opponent picks its difficulty from a select in the top-right, and PvP shares the keyboard (W/S vs ↑/↓). Updating now to the tower sim.",
  fileChanges = defaultFileChanges,
}: ChatPlanCollapsibleProps) {
  const doneCount = planSteps.filter((step) => step.status === "done").length;
  const runningCount = planSteps.filter((step) => step.status === "running").length;

  return (
    <div className="dm-root">
      <div className="dm-window">
        <aside className="dm-sidebar">
          <div className="dm-brand">
            <div className="dm-kmark">K</div>
            <div className="dm-brand-title"><strong>kim · adam@local</strong><span>v0.9.6</span></div>
          </div>
          <button className="dm-nav-button dm-primary">✦ New chat</button>
          <button className="dm-nav-button">Chat</button>
          <button className="dm-nav-button">Code</button>
          <div className="dm-sidebar-search"><span>Search sessions</span><span className="dm-kbd">⌘K</span></div>
          <div className="dm-sidebar-section">
            <div className="dm-section-label">projects</div>
            {['testing-grounds', 'BoBos', 'pong-arena'].map((project) => (
              <div className="dm-project-row" key={project}><span>{project}</span><span className="dm-meta">main</span></div>
            ))}
          </div>
          <div className="dm-sidebar-section">
            <div className="dm-section-label">today · pong-arena</div>
            <div className="dm-session-row"><span className="dm-session-title">{title}</span><span className="dm-session-meta">now · 30 msgs</span></div>
            <div className="dm-session-row"><span className="dm-session-title">Audit unused dependencies</span><span className="dm-session-meta">8:14 AM · 3 msgs</span></div>
          </div>
          <div className="dm-profile"><div className="dm-avatar">{userInitial}</div><div className="dm-brand-title"><strong>{userDisplayName}</strong><span>local · ready</span></div></div>
        </aside>

        <main className="dm-main">
          <header className="dm-topbar">
            <div className="dm-topbar-title">
              <span className="dm-code-label">CODE</span>
              <strong>{title}</strong>
              <span className="dm-meta">30 messages</span>
            </div>
            <button className="dm-button">Generate summary</button>
          </header>

          <section className="dm-chat-scroll">
            <div className="dm-chat-column">
              <div className="dm-user-bubble">{prompt}</div>

              <div className="dm-thinking-block">
                <div className="dm-rail-icon">›</div>
                <div className="dm-thinking-content">
                  <div className="dm-thinking-header">
                    <strong className="dm-thinking-word">Thinking</strong>
                    <span>{thinkingElapsed}</span>
                  </div>
                  <div className="dm-thinking-steps">
                    {thinkingSteps.map((step, index) => (
                      <div className={index === thinkingSteps.length - 1 ? "dm-step dm-running" : "dm-step"} key={step}>
                        <span>›</span>
                        <span>{index === 1 ? <><strong>Running</strong> find . -maxdepth 2</> : index === 2 ? <><strong>Reading</strong> pong.html</> : index === 3 ? <><strong>Reading</strong> index.html</> : step}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              <section className="dm-plan-card" aria-label="Collapsible plan">
                <div className="dm-plan-header">
                  <h3>Plan</h3>
                  <span className="dm-plan-progress">{doneCount} of {planSteps.length} done · {runningCount} in flight</span>
                </div>
                <div className="dm-plan-list">
                  {planSteps.map((step) => (
                    <div className={`dm-plan-item dm-${step.status}`} key={step.title}>
                      <span className="dm-plan-dot">{step.status === "done" ? "✓" : ""}</span>
                      <span className="dm-plan-title">{step.title}</span>
                      {step.meta ? <span className="dm-meta">{step.meta}</span> : <span />}
                    </div>
                  ))}
                </div>
              </section>

              <div className="dm-assistant-block">
                <div className="dm-rail-icon">▌</div>
                <p className="dm-stream-text">{streamUpdate} <span className="dm-stream-caret" /></p>
              </div>

              <div className="dm-file-grid">
                {fileChanges.map((change) => (
                  <div className="dm-file-row" key={change.file}>
                    <strong>{change.file}</strong>
                    <span className="dm-inline"><span className="dm-diff-plus">+{change.added}</span><span className="dm-diff-minus">-{change.removed}</span></span>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <footer className="dm-composer">
            <div className="dm-composer-box"><span>Message Kim…</span><span className="dm-kbd">⌘↵</span><button className="dm-send-button">→</button></div>
          </footer>
        </main>
      </div>
    </div>
  );
}
