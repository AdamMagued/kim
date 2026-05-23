export interface SettingsMcpProps {
  userEmail?: string;
}

const settingsNav = ['Appearance', 'AI', 'Voice', 'Paths', 'Data', 'Account', 'MCP', 'Feedback', 'About'];

export function SettingsMcp({ userEmail = "adam@local" }: SettingsMcpProps) {
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
              <button className={item === "MCP" ? "dm-settings-nav-button dm-active" : "dm-settings-nav-button"} key={item}>{item}</button>
            ))}
          </nav>
        </aside>
        <main className="dm-settings-main">
          <section className="dm-settings-panel dm-settings-content">
            <h1>MCP</h1>
            <p>Tool packs Kim can call — built-in and your own.</p>
            <div className="dm-panel" style={{ padding: 16 }}>
              <div className="dm-card-header"><h3>What is MCP?</h3><button className="dm-button">Learn more</button></div>
              <p className="dm-stream-text">Model Context Protocol — a standard for AI agents to talk to external tools. Each server is a plugin that gives Kim new capabilities.</p>
            </div>
            <div className="dm-setting-section"><h2>built-in tools · 10</h2><div className="dm-card-header"><span className="dm-badge">● all connected</span></div><div className="dm-tool-grid">
              {[
                ["take_screenshot", "Capture the current screen"],
                ["read_file", "Read a file from the filesystem"],
                ["write_file", "Write or create a file"],
                ["run_command", "Execute a shell command"],
                ["click", "Click at screen coordinates"],
                ["type_text", "Type using the keyboard"],
                ["browser_navigate", "Navigate a browser to a URL"],
                ["search_files", "Search files by name or content"],
                ["focus_window", "Bring an application window to focus"],
                ["get_screen_text", "Extract text visible on screen"],
              ].map(([tool, desc]) => <div className="dm-tool-row" key={tool}><strong>{tool}</strong><span className="dm-helper">{desc}</span></div>)}
            </div></div>
            <div className="dm-setting-section"><h2>custom servers · 2</h2><button className="dm-button">Add server</button><div className="dm-option-grid" style={{ marginTop: 12 }}>
              <div className="dm-option-row"><strong>puppeteer</strong><span className="dm-helper">npx -y @modelcontextprotocol/server-puppeteer</span></div>
              <div className="dm-option-row"><strong>filesystem</strong><span className="dm-helper">npx -y @modelcontextprotocol/server-filesystem</span></div>
            </div></div>
          </section>
        </main>
      </div>
    </div>
  );
}
