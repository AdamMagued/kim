export interface HybridPlanItem {
  title: string;
  status: "done" | "running" | "todo";
}

export interface ChatStreamHybridProps {
  title?: string;
  prompt?: string;
  elapsed?: string;
  thinkingLines?: string[];
  planItems?: HybridPlanItem[];
  liveMessage?: string;
}

const defaultPlanItems: HybridPlanItem[] = [
  { title: "Wire a CPU opponent into pong.html", status: "done" },
  { title: "Add Easy / Medium / Hard difficulty switches", status: "done" },
  { title: "Add a local PvP toggle", status: "done" },
  { title: "Build tower_sim.html with 5 balls", status: "running" },
  { title: "Add rotating platforms + collision", status: "todo" },
  { title: "Tune ball physics so the race is interesting", status: "todo" },
];

const defaultThinkingLines = [
  "Two candidates in the directory — pong.html and index.html. Let me figure out which is canonical.",
  "Running find . -maxdepth 2",
  "Reading pong.html",
  "Reading index.html",
  "pong.html is the polished one. Here's my plan before I start editing:",
];

export function ChatStreamHybrid({
  title = "session-1778461238826-0",
  prompt = "Wire a CPU opponent into pong.html",
  elapsed = "0:23 · 8 steps",
  thinkingLines = defaultThinkingLines,
  planItems = defaultPlanItems,
  liveMessage = "Pong + PvP wired up cleanly. Onto the tower sim — 5 colored balls dropping through rotating platforms.",
}: ChatStreamHybridProps) {
  const done = planItems.filter((item) => item.status === "done").length;

  return (
    <div className="dm-root">
      <main className="dm-window dm-window-compact">
        <section className="dm-stream-shell dm-main-narrow" style={{ marginTop: 32, marginBottom: 32 }}>
          <header className="dm-topbar" style={{ borderRadius: "24px 24px 0 0" }}>
            <div className="dm-topbar-title">
              <span className="dm-code-label">CODE</span>
              <strong>{title}</strong>
            </div>
            <button className="dm-button">Generate summary</button>
          </header>

          <div className="dm-chat-scroll" style={{ minHeight: 640 }}>
            <div className="dm-chat-column">
              <div className="dm-user-bubble">{prompt}</div>

              <div className="dm-thinking-block">
                <div className="dm-rail-icon">›</div>
                <div className="dm-thinking-content">
                  <div className="dm-thinking-header">
                    <strong className="dm-thinking-word">Thinking</strong>
                    <span>{elapsed}</span>
                  </div>
                  <div className="dm-thinking-steps">
                    {thinkingLines.map((line, index) => (
                      <div className={index === thinkingLines.length - 1 ? "dm-step dm-running" : "dm-step"} key={line}>
                        <span>›</span>
                        <span>{line}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              <section className="dm-plan-card">
                <div className="dm-plan-header">
                  <h3>Plan</h3>
                  <span className="dm-plan-progress">{done} / {planItems.length}</span>
                </div>
                <div className="dm-plan-list">
                  {planItems.map((item) => (
                    <div className={`dm-plan-item dm-${item.status}`} key={item.title}>
                      <span className="dm-plan-dot">{item.status === "done" ? "✓" : ""}</span>
                      <span className="dm-plan-title">{item.title}</span>
                      <span />
                    </div>
                  ))}
                </div>
              </section>

              <div className="dm-assistant-block">
                <div className="dm-rail-icon">▌</div>
                <p className="dm-stream-text">{liveMessage} <span className="dm-stream-caret" /></p>
              </div>
              <div className="dm-step dm-running"><span>›</span><span><strong>Writing</strong> tower_sim.html</span></div>
            </div>
          </div>

          <footer className="dm-composer" style={{ borderRadius: "0 0 24px 24px" }}>
            <div className="dm-composer-box"><span>Message Kim…</span><button className="dm-send-button">→</button><span className="dm-scroll-hint">↵ to send · ⇧ + ↵ for new line</span></div>
          </footer>
        </section>
      </main>
    </div>
  );
}
