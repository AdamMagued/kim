export interface SettingsFeedbackProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsFeedback({ userEmail = "adam@local" }: SettingsFeedbackProps) {
  return (
    <div className="dm-root">
      <div className="dm-settings-root">
        <aside className="dm-settings-sidebar">
          <div className="dm-brand">
            <div className="dm-kmark">K</div>
            <div className="dm-brand-title"><strong>kim · {userEmail}</strong><span>Settings</span></div>
          </div>
          <nav className="dm-settings-nav" aria-label="Settings sections">
            {settingsNav.map((item) => (
              <button className={item === "Feedback" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Feedback</h1>
            <p>Tell us what's on your mind. We read every one.</p>
            <div className="dm-setting-section"><h2>what kind of feedback?</h2><div className="dm-feedback-grid">
              <button className="dm-feedback-type dm-active"><span>🐛</span><strong>Bug</strong></button>
              <button className="dm-feedback-type"><span>✦</span><strong>Feature</strong></button>
              <button className="dm-feedback-type"><span>○</span><strong>General</strong></button>
              <button className="dm-feedback-type"><span>♡</span><strong>Praise</strong></button>
              <button className="dm-feedback-type"><span>…</span><strong>Other</strong></button>
            </div></div>
            <div className="dm-setting-section"><h2>your message</h2><textarea className="dm-textarea" defaultValue={"The Worked-on bar collapses too fast. I'd love to see the plan stay around so I can…"} /></div>
            <div className="dm-action-row" style={{ justifyContent: "flex-start" }}><button className="dm-button">Attach screenshot</button><button className="dm-button">Include session logs</button><button className="dm-button dm-primary">Send feedback</button></div>
          </section>
        </main>
      </div>
    </div>
  );
}
