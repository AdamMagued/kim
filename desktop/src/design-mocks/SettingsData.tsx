export interface SettingsDataProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsData({ userEmail = "adam@local" }: SettingsDataProps) {
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
              <button className={item === "Data" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Data</h1>
            <p>Your sessions live on your machine. Nothing leaves unless you sync.</p>
            <div className="dm-panel" style={{ padding: 16 }}>
              <p className="dm-stream-text">All data stays on this machine. Pick one of the methods below to sync or back it up.</p>
            </div>
            <div className="dm-setting-section"><h2>backup methods</h2><div className="dm-option-grid">
              <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">G</span><div className="dm-option-main"><strong>GitHub Gist sync</strong><span className="dm-helper">Backs up your session index to a private Gist. Restores on any machine where you sign in.</span></div></div><button className="dm-button">Connect</button></div>
              <div className="dm-option-row"><div className="dm-inline"><span className="dm-provider-mark">↓</span><div className="dm-option-main"><strong>File export / import</strong><span className="dm-helper">ZIP, JSON, or Markdown — no internet required. Good for offline archives.</span></div></div><button className="dm-button">Export</button></div>
            </div></div>
            <div className="dm-setting-section"><h2>export</h2><div className="dm-segmented"><button className="dm-segment dm-active">.zip</button><button className="dm-segment">.json</button><button className="dm-segment">.md</button></div><p className="dm-helper">ZIP = raw JSONL · JSON = structured index · MD = human-readable.</p></div>
          </section>
        </main>
      </div>
    </div>
  );
}
