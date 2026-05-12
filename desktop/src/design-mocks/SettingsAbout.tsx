export interface SettingsAboutProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsAbout({ userEmail = "adam@local" }: SettingsAboutProps) {
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
              <button className={item === "About" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>About</h1>
            <p>What's running, and what's new.</p>
            <div className="dm-setting-section">
              <div className="dm-option-row">
                <div className="dm-inline"><div className="dm-about-logo">K</div><div className="dm-option-main"><strong>Kim Desktop</strong><span className="dm-helper">v0.9.6 · current</span></div></div>
                <button className="dm-button">Check for updates</button>
              </div>
              <p className="dm-stream-text">Kim is a local AI agent that runs entirely on your machine. No telemetry, no cloud accounts required.</p>
            </div>
            <div className="dm-setting-section"><h2>what&apos;s new</h2>
              <div className="dm-update-row"><div className="dm-card-header"><strong>▸ v0.9.2 — Headless mode fixes</strong><span className="dm-meta">Mar 14</span></div><p className="dm-helper">Gemini prompts now always send. The hidden browser window keeps a 1024×768 viewport at off-screen position, so layout stays stable.</p></div>
              <div className="dm-update-row"><strong>▸ v0.9.1 — MCP plug-ins land</strong></div>
              <div className="dm-update-row"><strong>▸ v0.9.0 — Local-first refactor</strong></div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
