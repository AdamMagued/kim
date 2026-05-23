export interface SettingsAccountProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsAccount({ userEmail = "adam@local" }: SettingsAccountProps) {
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
              <button className={item === "Account" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>Account</h1>
            <p>How you show up across Kim and your linked services.</p>
            <div className="dm-setting-section"><div className="dm-option-row"><div className="dm-inline"><div className="dm-avatar">A</div><div className="dm-option-main"><strong>adam</strong><span className="dm-helper">local · joined Mar 2026</span></div></div><button className="dm-button">Edit profile</button></div></div>
            <div className="dm-setting-section"><h2>integrations</h2><div className="dm-option-grid">
              <div className="dm-option-row"><div className="dm-option-main"><strong>GitHub</strong><span className="dm-helper">Required for Gist backup sync. Use a token with gist and read:user scopes.</span></div><span className="dm-badge">● connected</span><button className="dm-button">Manage</button></div>
              <div className="dm-option-row"><div className="dm-option-main"><strong>OpenAI</strong><span className="dm-helper">For API-mode chats. Goes through your account, not ours.</span></div><button className="dm-button">Connect</button></div>
            </div></div>
            <div className="dm-setting-section"><h2>danger zone</h2><div className="dm-option-grid">
              <div className="dm-option-row"><div className="dm-option-main"><strong>Reset onboarding</strong><span className="dm-helper">Forget your account and re-run the welcome flow on next launch.</span></div><button className="dm-button">Reset…</button></div>
              <div className="dm-option-row"><div className="dm-option-main"><strong className="dm-danger">Delete all sessions</strong><span className="dm-helper">Permanently wipe local JSONL files. This cannot be undone.</span></div><button className="dm-button dm-danger">Delete…</button></div>
            </div></div>
          </section>
        </main>
      </div>
    </div>
  );
}
