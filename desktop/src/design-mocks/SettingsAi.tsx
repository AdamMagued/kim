export interface SettingsAiProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsAi({ userEmail = "adam@local" }: SettingsAiProps) {
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
              <button className={item === "AI" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>AI</h1>
            <p>Which model runs Kim and how it behaves while working.</p>
            <div className="dm-setting-section">
              <h2>default provider</h2>
              <div className="dm-option-grid">
                <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">C</span><div className="dm-option-main"><strong>Claude</strong><span className="dm-helper">via browser · signed in</span></div></div><span className="dm-badge">● connected</span></div>
                <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">C</span><div className="dm-option-main"><strong>ChatGPT</strong><span className="dm-helper">via browser · sign in</span></div></div><button className="dm-button">Connect</button></div>
                <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">G</span><div className="dm-option-main"><strong>Gemini</strong><span className="dm-helper">via browser · sign in</span></div></div><button className="dm-button">Connect</button></div>
                <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">A</span><div className="dm-option-main"><strong>API key</strong><span className="dm-helper">Anthropic / OpenAI / OpenRouter</span></div></div></div>
              </div>
            </div>
            <div className="dm-setting-section">
              <h2>behavior</h2>
              <div className="dm-option-grid">
                <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Queue messages while Kim is working</strong><span className="dm-helper">Off: a new send interrupts the current task. On: sends queue and run in order.</span></div><div className="dm-toggle"><span /></div></div>
                <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Keep browser visible while running</strong><span className="dm-helper">Testing only — leaves the provider window on-screen so you can watch what Kim does.</span></div><div className="dm-toggle dm-on"><span /></div></div>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
