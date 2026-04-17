import { useState } from 'react';
import { WebviewWindow } from '@tauri-apps/api/webviewWindow';
import { toast } from './Toast';

interface BrowserProvider {
  id: string;
  name: string;
  url: string | null; // null = custom (user provides URL)
  hint: string;
  warning?: string;
}

const BROWSER_PROVIDERS: BrowserProvider[] = [
  {
    id: 'claude',
    name: 'Claude',
    url: 'https://claude.ai',
    hint: 'Sign in with your Anthropic account',
  },
  {
    id: 'chatgpt',
    name: 'ChatGPT',
    url: 'https://chatgpt.com',
    hint: 'Sign in with your OpenAI account',
  },
  {
    id: 'gemini',
    name: 'Gemini',
    url: 'https://gemini.google.com',
    hint: 'Sign in with your Google account',
  },
  {
    id: 'grok',
    name: 'Grok',
    url: 'https://grok.com',
    hint: 'Sign in with your X (Twitter) account',
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    url: 'https://chat.deepseek.com',
    hint: 'Sign in with your DeepSeek account',
    warning: 'DeepSeek cannot see screenshots — screen control tasks will not work.',
  },
  {
    id: 'custom',
    name: 'Custom',
    url: null,
    hint: 'Open any AI chat website inside Kim',
  },
];

// ── Icons ─────────────────────────────────────────────────────────────────────

function ClaudeIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 14H9V8h2v8zm4 0h-2V8h2v8z" />
    </svg>
  );
}
function ChatGPTIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
      <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zm-9.022 12.338a4.476 4.476 0 0 1-2.866-1.032l.141-.081 4.756-2.748a.78.78 0 0 0 .392-.681v-6.706l2.01 1.16a.07.07 0 0 1 .039.053v5.56a4.494 4.494 0 0 1-4.472 4.474z" />
    </svg>
  );
}
function GeminiIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
      <path d="M12 0C5.372 0 0 5.372 0 12s5.372 12 12 12 12-5.372 12-12S18.628 0 12 0zm0 2.4a9.6 9.6 0 1 1 0 19.2A9.6 9.6 0 0 1 12 2.4zm0 3.6L8.4 12 12 18l3.6-6L12 6z" />
    </svg>
  );
}
function GrokIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
    </svg>
  );
}
function DeepSeekIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M8 12h8M12 8v8" />
    </svg>
  );
}
function CustomIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 8v4l3 3" />
    </svg>
  );
}
function WarnTriangle() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2L1 14h14L8 2z" /><path d="M8 6v4M8 11.5v.5" />
    </svg>
  );
}
function ExternalIcon() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 3h3v3M13 3L7 9M6 4H4a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1v-2" />
    </svg>
  );
}

const ICONS: Record<string, React.ReactNode> = {
  claude:   <ClaudeIcon />,
  chatgpt:  <ChatGPTIcon />,
  gemini:   <GeminiIcon />,
  grok:     <GrokIcon />,
  deepseek: <DeepSeekIcon />,
  custom:   <CustomIcon />,
};

interface Props {
  selected: string;
  onSelect: (providerId: string) => void;
}

export function BrowserProviderPicker({ selected, onSelect }: Props) {
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [customUrl, setCustomUrl] = useState('https://');

  const selectedProvider = BROWSER_PROVIDERS.find(p => p.id === selected);

  async function openInKim(provider: BrowserProvider) {
    const targetUrl = provider.url ?? customUrl.trim();
    if (!targetUrl || targetUrl === 'https://') {
      toast('Please enter a URL for your custom AI provider.', 'warning');
      return;
    }

    setOpeningId(provider.id);
    try {
      const label = `browser-signin-${provider.id}`;
      const existing = await WebviewWindow.getByLabel(label);
      if (existing) {
        await existing.setFocus();
        setOpeningId(null);
        return;
      }

      const w = new WebviewWindow(label, {
        url: targetUrl,
        title: `Sign in to ${provider.name}`,
        width: 1100,
        height: 760,
        center: true,
        resizable: true,
        decorations: true,
      });

      w.once('tauri://created', () => {
        toast(`${provider.name} opened — sign in, then come back to Kim.`, 'info', 5000);
        setOpeningId(null);
      });
      w.once('tauri://error', (e) => {
        toast(`Failed to open ${provider.name}: ${String(e)}`, 'error');
        setOpeningId(null);
      });
    } catch (err) {
      toast(`Could not open browser window: ${String(err)}`, 'error');
      setOpeningId(null);
    }
  }

  return (
    <div className="kim-browser-picker">
      <div className="kim-browser-picker__label">Select browser AI</div>
      <div className="kim-browser-picker__grid">
        {BROWSER_PROVIDERS.map(p => (
          <div
            key={p.id}
            className={`kim-browser-card${selected === p.id ? ' kim-browser-card--active' : ''}`}
            onClick={() => onSelect(p.id)}
          >
            <div className="kim-browser-card__top">
              <span className="kim-browser-card__icon">{ICONS[p.id]}</span>
              <span className="kim-browser-card__name">{p.name}</span>
              {selected === p.id && (
                <svg className="kim-browser-card__check" viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 8l3.5 3.5L13 4" />
                </svg>
              )}
            </div>
            <div className="kim-browser-card__hint">{p.hint}</div>
            {p.warning && (
              <div className="kim-browser-card__warning">
                <WarnTriangle />
                <span>{p.warning}</span>
              </div>
            )}
            {/* Custom URL input */}
            {selected === p.id && p.id === 'custom' && (
              <input
                className="kim-browser-card__url-input"
                type="url"
                placeholder="https://your-ai-provider.com"
                value={customUrl}
                onChange={e => setCustomUrl(e.target.value)}
                onClick={e => e.stopPropagation()}
              />
            )}
            {selected === p.id && (
              <button
                className="kim-browser-card__open-btn"
                onClick={e => { e.stopPropagation(); void openInKim(p); }}
                disabled={openingId === p.id}
              >
                <ExternalIcon />
                <span>{openingId === p.id ? 'Opening…' : `Open ${p.name} in Kim`}</span>
              </button>
            )}
          </div>
        ))}
      </div>
      <div className="kim-browser-picker__info">
        {selectedProvider?.id === 'custom'
          ? 'Enter any AI chat URL above, open it in Kim, sign in, then send your task as normal.'
          : 'Sign into the AI provider above, then send your task from Kim as normal. Kim will relay your message through that browser session.'}
      </div>
    </div>
  );
}
