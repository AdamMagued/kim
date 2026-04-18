import { useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { toast } from './Toast';

interface BrowserProvider {
  id: string;
  name: string;
  url: string | null;
  hint: string;
  warning?: string;
}

const BROWSER_PROVIDERS: BrowserProvider[] = [
  { id: 'claude',   name: 'Claude',   url: 'https://claude.ai',          hint: 'Sign in with your Anthropic account' },
  { id: 'chatgpt',  name: 'ChatGPT',  url: 'https://chatgpt.com',        hint: 'Sign in with your OpenAI account' },
  { id: 'gemini',   name: 'Gemini',   url: 'https://gemini.google.com',  hint: 'Sign in with your Google account' },
  { id: 'grok',     name: 'Grok',     url: 'https://grok.com',           hint: 'Sign in with your X (Twitter) account' },
  {
    id: 'deepseek', name: 'DeepSeek', url: 'https://chat.deepseek.com',  hint: 'Sign in with your DeepSeek account',
    warning: 'DeepSeek cannot see screenshots — screen control tasks will not work.',
  },
  { id: 'custom',   name: 'Custom',   url: null,                         hint: 'Open any AI chat website inside Kim' },
];

// ── Real brand logos ──────────────────────────────────────────────────────────

function ClaudeIcon() {
  // Anthropic / Claude wordmark "A" shape
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-label="Claude">
      <path d="M13.827 3.52h3.603L24 20.479h-3.603l-6.57-16.96zM8.322 3.52H4.719L.001 20.479h3.603l.85-2.487h5.865l.85 2.487h3.603L9.172 3.52zm-3.086 11.77 1.902-5.566 1.9 5.567H5.236z"/>
    </svg>
  );
}

function ChatGPTIcon() {
  // OpenAI logo
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-label="ChatGPT">
      <path d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.051 6.051 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.866-1.032l.141-.081 4.756-2.748a.78.78 0 0 0 .392-.681v-6.706l2.01 1.16a.07.07 0 0 1 .039.053v5.56a4.494 4.494 0 0 1-4.472 4.474zm-9.722-4.107a4.476 4.476 0 0 1-.535-3.014l.142.085 4.756 2.747a.77.77 0 0 0 .783 0l5.812-3.354v2.318a.07.07 0 0 1-.028.061l-4.808 2.776a4.494 4.494 0 0 1-6.122-1.619zm-1.264-9.642A4.475 4.475 0 0 1 4.61 6.54v5.641a.78.78 0 0 0 .391.681l5.814 3.354-2.01 1.16a.07.07 0 0 1-.069.007L3.927 14.6a4.494 4.494 0 0 1-.653-5.919zm16.556 3.864l-5.813-3.354 2.01-1.16a.07.07 0 0 1 .07-.006l4.808 2.776a4.494 4.494 0 0 1-.691 8.108v-5.64a.77.77 0 0 0-.384-.724zm2.001-3.025-.141-.085-4.755-2.748a.77.77 0 0 0-.785 0L9.34 10.031V7.712a.07.07 0 0 1 .028-.061l4.808-2.775a4.493 4.493 0 0 1 6.676 4.653zm-12.57 4.135l-2.01-1.16a.07.07 0 0 1-.038-.053v-5.56a4.493 4.493 0 0 1 7.363-3.448l-.141.08-4.756 2.748a.78.78 0 0 0-.392.681zm1.092-2.354l2.587-1.495 2.587 1.494v2.99l-2.587 1.494-2.587-1.494z"/>
    </svg>
  );
}

function GeminiIcon() {
  // Google Gemini logo (the four-pointed star)
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-label="Gemini">
      <path d="M12 24A14.304 14.304 0 0 0 0 12 14.304 14.304 0 0 0 12 0a14.305 14.305 0 0 0 12 12 14.305 14.305 0 0 0-12 12" />
    </svg>
  );
}

function GrokIcon() {
  // X (Twitter) logo
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-label="Grok">
      <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
    </svg>
  );
}

function DeepSeekIcon() {
  // DeepSeek whale/fish shape approximation
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-label="DeepSeek">
      <path d="M22.979 10.557c-.024-.053-.071-.092-.128-.103a6.87 6.87 0 0 0-.765-.088c-.459-.033-.918-.045-1.378-.037-.18-.44-.423-.85-.722-1.218 1.09-1.89 1.68-4.01 1.302-5.987-.033-.175-.195-.296-.373-.27C18.37 3.19 16.7 4.35 15.39 5.9a8.32 8.32 0 0 0-3.39-.72 8.32 8.32 0 0 0-3.39.72C7.3 4.35 5.63 3.19 3.086 2.854a.357.357 0 0 0-.373.27c-.379 1.977.212 4.097 1.302 5.987-.3.368-.542.777-.722 1.218-.46-.008-.919.004-1.378.037-.257.018-.513.05-.765.088a.203.203 0 0 0-.128.103.193.193 0 0 0-.003.162c.31.717.752 1.366 1.303 1.91a5.84 5.84 0 0 0 1.793 1.183 9.047 9.047 0 0 0 1.47 3.97C6.83 19.316 9.28 20.4 12 20.4s5.17-1.084 6.415-3.218a9.047 9.047 0 0 0 1.47-3.97 5.84 5.84 0 0 0 1.793-1.184 5.574 5.574 0 0 0 1.303-1.909.193.193 0 0 0-.002-.162zM12 6.14c.774 0 1.53.113 2.25.327-.692.965-1.226 2.04-1.567 3.183a.78.78 0 0 1-.683.54.78.78 0 0 1-.683-.54A10.35 10.35 0 0 0 9.75 6.467c.72-.214 1.476-.327 2.25-.327zm0 12.66c-2.254 0-4.274-.985-5.512-2.676a7.618 7.618 0 0 1-1.33-3.694C5.895 11.065 8.75 9.12 12 9.12s6.105 1.945 6.842 3.31a7.618 7.618 0 0 1-1.33 3.694C16.274 17.815 14.254 18.8 12 18.8z"/>
    </svg>
  );
}

function CustomIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-label="Custom">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 8v8M8 12h8" />
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

// ── Provider picker ───────────────────────────────────────────────────────────

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
      await invoke<string>('open_browser_signin_window', {
        url: targetUrl,
        providerName: provider.name,
      });
      toast(`${provider.name} opened inside Kim. Browser mode runs through this in-app window.`, 'info', 7000);
    } catch (err) {
      const msg = typeof err === 'string' ? err : `Could not open ${provider.name}.`;
      toast(msg, 'error', 5000);
    } finally {
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
          ? 'Enter any AI chat URL and open it inside Kim. Browser mode will use this window for prompt/response execution.'
          : 'Use this in-app sign-in window for browser mode. Kim executes prompt/response directly in this window.'}
      </div>
    </div>
  );
}
