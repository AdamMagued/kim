export interface SettingsAppearanceProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsAppearance({ userEmail = "adam@local" }: SettingsAppearanceProps) {
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
              <button className={item === "Appearance" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Appearance</h1>
            <p>How Kim looks on your machine.</p>
            <div className="dm-setting-section">
              <h2>theme</h2>
              <div className="dm-segmented">
                <button className="dm-segment">Light</button>
                <button className="dm-segment">System</button>
                <button className="dm-segment dm-active">Dark</button>
              </div>
            </div>
            <div className="dm-setting-section">
              <h2>accent</h2>
              <div className="dm-accent-swatches" aria-label="Accent color choices">
                <span className="dm-swatch dm-swatch-terracotta" title="Terracotta" />
                <span className="dm-swatch dm-swatch-green" title="Green" />
                <span className="dm-swatch dm-swatch-blue" title="Blue" />
                <span className="dm-swatch dm-swatch-red" title="Rose" />
              </div>
              <p className="dm-helper">Terracotta</p>
            </div>
            <div className="dm-setting-section">
              <h2>message animation</h2>
              <div className="dm-option-grid">
                <div className="dm-option-row"><div className="dm-option-main"><strong>Instant</strong><span className="dm-helper">No animation, just appear</span></div><span className="dm-badge">selected</span></div>
                <div className="dm-option-row"><div className="dm-option-main"><strong>Typewriter</strong><span className="dm-helper">One character at a time</span></div></div>
                <div className="dm-option-row"><div className="dm-option-main"><strong>Word fade</strong><span className="dm-helper">Words drift up and fade in</span></div></div>
                <div className="dm-option-row"><div className="dm-option-main"><strong>Char blur</strong><span className="dm-helper">Letters crystallise from blur</span></div></div>
              </div>
              <p className="dm-helper">Applies to the newest message only.</p>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
