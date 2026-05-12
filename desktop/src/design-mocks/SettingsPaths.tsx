export interface SettingsPathsProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsPaths({ userEmail = "adam@local" }: SettingsPathsProps) {
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
              <button className={item === "Paths" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Paths</h1>
            <p>Where Kim reads and writes on disk.</p>
            <div className="dm-setting-section"><div className="dm-option-grid">
              <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Kim sessions directory</strong><span className="dm-helper">Leave empty to use the default (~/Desktop/kim/kim_sessions or ~/.kim/sessions).</span></div><div className="dm-input-box">~/.kim/sessions</div></div>
              <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Code sessions directory</strong><span className="dm-helper">Path where Claw stores its JSONL session files.</span></div><div className="dm-input-box">~/code/.claw/sessions</div></div>
              <div className="dm-setting-row"><div className="dm-setting-main-copy"><strong>Project root</strong><span className="dm-helper">Root of your Kim installation (where orchestrator/ lives). Leave empty for auto-detect.</span></div><div className="dm-input-box">~/code</div></div>
            </div></div>
          </section>
        </main>
      </div>
    </div>
  );
}
