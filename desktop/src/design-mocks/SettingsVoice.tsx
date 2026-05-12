export interface SettingsVoiceProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsVoice({ userEmail = "adam@local" }: SettingsVoiceProps) {
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
              <button className={item === "Voice" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Voice</h1>
            <p>Kim speaks task completions, stuck detection, and tool announcements.</p>
            <div className="dm-setting-section">
              <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Enable voice</strong><span className="dm-helper">Speak completions, errors, and confirmations aloud.</span></div><div className="dm-toggle dm-on"><span /></div></div>
            </div>
            <div className="dm-setting-section"><h2>engine</h2><div className="dm-option-grid">
              <div className="dm-option-row"><div className="dm-option-main"><strong>Kokoro</strong><span className="dm-helper">Local · 90 MB · fast</span></div><span className="dm-badge">selected</span></div>
              <div className="dm-option-row"><div className="dm-option-main"><strong>ElevenLabs</strong><span className="dm-helper">Cloud · API key</span></div></div>
              <div className="dm-option-row"><div className="dm-option-main"><strong>System TTS</strong><span className="dm-helper">OS native</span></div></div>
            </div></div>
            <div className="dm-setting-section"><h2>voice · 4 available</h2><div className="dm-option-grid">
              <div className="dm-option-row"><div><strong>Heart</strong><span className="dm-helper"> warm female · default</span></div><span className="dm-badge">✓</span></div>
              <div className="dm-option-row"><div><strong>Bella</strong><span className="dm-helper"> neutral female</span></div></div>
              <div className="dm-option-row"><div><strong>Adam</strong><span className="dm-helper"> deep male</span></div></div>
              <div className="dm-option-row"><div><strong>Nova</strong><span className="dm-helper"> synthetic</span></div></div>
            </div><p className="dm-helper">speed · 0.5× — 1.0× — 2.0×</p></div>
          </section>
        </main>
      </div>
    </div>
  );
}
