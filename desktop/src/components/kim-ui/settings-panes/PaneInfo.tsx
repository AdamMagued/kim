import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { Settings } from '../../../types';
import { toast } from '../../Toast';
import { PairingModal } from '../../PairingModal';
import { SectionLabel, Row } from './primitives';

const BUILT_IN_TOOLS: [string, string][] = [
  ['take_screenshot', 'Capture the current screen'],
  ['read_file', 'Read a file from the filesystem'],
  ['write_file', 'Write or create a file'],
  ['run_command', 'Execute a shell command'],
  ['click', 'Click at screen coordinates'],
  ['type_text', 'Type using the keyboard'],
  ['browser_navigate', 'Navigate a browser to a URL'],
  ['search_files', 'Search files by name or content'],
  ['focus_window', 'Bring an application window to focus'],
  ['get_screen_text', 'Extract text visible on screen'],
];

function PaneMCP() {
  return (
    <>
      <div
        style={{
          display: 'flex',
          gap: 14,
          marginBottom: 22,
          padding: 16,
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 14, marginBottom: 4 }}>What is MCP?</div>
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', lineHeight: 1.55 }}>
            Model Context Protocol — a standard for AI agents to talk to external tools. Each server is a plugin that gives Kim new abilities.
          </div>
        </div>
      </div>

      <div style={{ marginBottom: 12 }}>
        <SectionLabel>built-in tools · {BUILT_IN_TOOLS.length}</SectionLabel>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 22 }}>
        {BUILT_IN_TOOLS.map(([n, d]) => (
          <div
            key={n}
            style={{
              background: 'var(--kim-bg-2)',
              border: '1px solid var(--kim-border)',
              borderRadius: 9,
              padding: '10px 13px',
            }}
          >
            <div style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 12.5, color: 'var(--kim-accent)' }}>
              {n}
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', marginTop: 2 }}>{d}</div>
          </div>
        ))}
      </div>

      <div style={{ marginBottom: 10 }}>
        <SectionLabel>custom servers</SectionLabel>
      </div>
      <div
        style={{
          fontSize: 12.5,
          color: 'var(--kim-text-3)',
          padding: '12px 14px',
          background: 'var(--kim-surface)',
          border: '1px dashed var(--kim-border)',
          borderRadius: 11,
        }}
      >
        No custom MCP servers configured. Add one to expose external tools to Kim.
      </div>
    </>
  );
}

function PaneFeedback() {
  const [kind, setKind] = useState<'bug' | 'feature' | 'general' | 'praise' | 'other'>('bug');
  const [message, setMessage] = useState('');
  const [contact, setContact] = useState('');
  const [sending, setSending] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!message.trim()) return;
    setSending(true);
    try {
      await invoke('send_feedback', {
        payload: {
          category: kind,
          message: message.trim(),
          contact: contact.trim() || null,
        },
      });
      toast('Feedback sent. Thank you!', 'success', 3000);
      setMessage('');
      setContact('');
    } catch (err) {
      toast(`Send failed: ${String(err)}`, 'error', 4000);
    } finally {
      setSending(false);
    }
  }

  const fbIcon = (id: string, active: boolean) => {
    const sw = {
      stroke: 'currentColor',
      strokeWidth: 1.3,
      fill: 'none',
      strokeLinecap: 'round' as const,
      strokeLinejoin: 'round' as const,
    };
    const color = active ? 'var(--kim-accent)' : 'var(--kim-text-2)';
    switch (id) {
      case 'bug':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <rect {...sw} x="6" y="6" width="8" height="9" rx="3.5" />
            <path {...sw} d="M10 6V4M8 5l-1.5-1.5M12 5l1.5-1.5M6 10H3.5M14 10h2.5M7 14l-2 2M13 14l2 2" />
          </svg>
        );
      case 'feature':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M10 2.5l1.6 4.2 4.4.5-3.3 2.9 1 4.4L10 12.3 6.3 14.5l1-4.4L4 7.2l4.4-.5L10 2.5z" />
          </svg>
        );
      case 'general':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M3.5 6a2 2 0 012-2h9a2 2 0 012 2v6a2 2 0 01-2 2H8l-3.5 3v-3a2 2 0 01-2-2V6z" />
            <path {...sw} d="M7 9h6M7 11.5h4" />
          </svg>
        );
      case 'praise':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <path {...sw} d="M10 16.5c-3-2-7-4.5-7-8a3.5 3.5 0 016-2.4 3.5 3.5 0 016 2.4c0 3.5-4 6-7 8z" />
          </svg>
        );
      case 'other':
        return (
          <svg width="20" height="20" viewBox="0 0 20 20" style={{ color }}>
            <circle {...sw} cx="5" cy="10" r="1" />
            <circle {...sw} cx="10" cy="10" r="1" />
            <circle {...sw} cx="15" cy="10" r="1" />
          </svg>
        );
      default:
        return null;
    }
  };

  return (
    <form onSubmit={submit}>
      <SectionLabel>what kind of feedback?</SectionLabel>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginBottom: 22 }}>
        {(['bug', 'feature', 'general', 'praise', 'other'] as const).map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setKind(id)}
            className={`kr-tile${kind === id ? ' kr-on' : ''}`}
            style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, padding: '16px 6px' }}
          >
            {fbIcon(id, kind === id)}
            <span style={{ fontSize: 12, color: kind === id ? 'var(--kim-text)' : 'var(--kim-text-2)' }}>
              {id[0].toUpperCase() + id.slice(1)}
            </span>
          </button>
        ))}
      </div>

      <SectionLabel>your message</SectionLabel>
      <textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="Tell us what's on your mind…"
        rows={6}
        aria-label="Your feedback message"
        className="kr-input"
        style={{
          fontFamily: 'inherit',
          minHeight: 140,
          resize: 'vertical',
          marginBottom: 12,
        }}
      />

      <SectionLabel>email (optional)</SectionLabel>
      <input
        type="email"
        className="kr-input"
        placeholder="you@example.com"
        aria-label="Email address (optional)"
        value={contact}
        onChange={(e) => setContact(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      <button
        type="submit"
        className="kr-btn kr-btn-primary"
        disabled={!message.trim() || sending}
        style={{ width: '100%', justifyContent: 'center', padding: 12 }}
      >
        {sending ? 'Sending…' : 'Send feedback'}
      </button>

      <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--kim-border)' }}>
        <SectionLabel>attach logs to your report</SectionLabel>
        <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', marginBottom: 10, lineHeight: 1.55 }}>
          Kim writes structured logs to <code style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>logs/kim-YYYY-MM-DD.jsonl</code> (7-day rolling window). Attach them to bug reports for faster diagnosis.
        </div>
        <button
          type="button"
          className="kr-btn"
          onClick={async () => {
            try {
              await invoke('reveal_logs');
            } catch (err) {
              toast(`Could not open logs folder: ${String(err)}`, 'error', 4000);
            }
          }}
          style={{ gap: 7 }}
        >
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
            <path d="M1 4a1 1 0 011-1h3l1.5 1.5H12a1 1 0 011 1V11a1 1 0 01-1 1H2a1 1 0 01-1-1V4z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
          </svg>
          Reveal logs
        </button>
      </div>
    </form>
  );
}

function PaneAbout({ appVersion, onCheckUpdate }: { appVersion: string; onCheckUpdate: () => void }) {
  return (
    <>
      <div
        style={{
          display: 'flex',
          gap: 18,
          padding: 22,
          background: 'linear-gradient(135deg, var(--kim-surface), var(--kim-bg-2))',
          border: '1px solid var(--kim-border)',
          borderRadius: 14,
          marginBottom: 22,
        }}
      >
        <div
          style={{
            width: 64,
            height: 64,
            borderRadius: 14,
            background: 'linear-gradient(135deg, var(--kim-accent), #c98968)',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--kim-on-accent)',
            fontWeight: 700,
            fontSize: 32,
          }}
        >
          K
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 18, marginBottom: 4 }}>Kim Desktop</div>
          <div
            style={{
              fontSize: 12.5,
              color: 'var(--kim-text-3)',
              fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              marginBottom: 8,
            }}
          >
            v{appVersion} · current
          </div>
          <div style={{ fontSize: 13, color: 'var(--kim-text-2)', lineHeight: 1.55, maxWidth: 460 }}>
            Kim is a local AI agent that runs entirely on your machine. No telemetry, no cloud accounts required.
          </div>
        </div>
        <button type="button" className="kr-btn" onClick={onCheckUpdate} style={{ alignSelf: 'flex-start' }}>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path
              d="M2 6a4 4 0 017-2.5M10 6a4 4 0 01-7 2.5M9 1v3h-3M3 11V8h3"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Check for updates
        </button>
      </div>

      <SectionLabel>about kim</SectionLabel>
      <div
        style={{
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          padding: '14px 16px',
          fontSize: 13,
          color: 'var(--kim-text-2)',
          lineHeight: 1.6,
        }}
      >
        Kim is built on the Model Context Protocol and integrates with browsers, files, and shells. Configure providers and tools in the
        sections to the left.
      </div>
    </>
  );
}

interface RelayConfigSnapshot {
  url: string;
  pc_key_configured: boolean;
}

function PaneRelay({ settings }: { settings: Settings }) {
  const [cfg, setCfg] = useState<RelayConfigSnapshot | null>(null);
  const [urlDraft, setUrlDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [pairOpen, setPairOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function refresh() {
    try {
      const snap = await invoke<RelayConfigSnapshot>('read_relay_config', {
        projectRoot: settings.project_root || null,
      });
      setCfg(snap);
      setUrlDraft(snap.url);
      setErr(null);
    } catch (e) {
      setErr(String(e));
    }
  }
  useEffect(() => {
    refresh();
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, []);

  async function saveUrl() {
    setSaving(true);
    try {
      await invoke('write_relay_url', {
        url: urlDraft.trim(),
        projectRoot: settings.project_root || null,
      });
      toast('Relay URL saved.', 'success', 2000);
      await refresh();
    } catch (e) {
      toast(`Failed to save: ${String(e)}`, 'error', 4000);
    } finally {
      setSaving(false);
    }
  }

  const urlDirty = (cfg?.url ?? '') !== urlDraft.trim();
  const canPair = !!(cfg?.url && cfg?.pc_key_configured);

  return (
    <>
      <SectionLabel>Status & Info</SectionLabel>
      <div style={{ color: 'var(--kim-text-2)', fontSize: 13, lineHeight: 1.6, marginBottom: 20 }}>
        Send prompts from your phone to this PC. Kim runs the task here (with full
        access to your files, browser, and screen) and streams the result back.
        Pair once with a QR code; after that, prompts route automatically while
        relay mode is on.
      </div>

      <SectionLabel>Relay URL</SectionLabel>
      <div style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
        <input
          className="kr-input"
          placeholder="https://kim-relay.fly.dev"
          value={urlDraft}
          onChange={(e) => setUrlDraft(e.target.value)}
          spellCheck={false}
          autoCapitalize="none"
          autoCorrect="off"
          style={{ flex: 1 }}
        />
        <button
          type="button"
          className="kr-btn kr-btn-primary"
          onClick={saveUrl}
          disabled={!urlDirty || saving}
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: -12, marginBottom: 20 }}>
        The relay server that bridges phone ↔ PC. Self-host or use the one shared with you.
      </div>

      <SectionLabel>PC API key</SectionLabel>
      <Row
        title="API key configuration"
        subtitle="Stored in .env as RELAY_PC_API_KEY. Set this before pairing."
      >
        <span
          style={{
            fontSize: 12,
            color: cfg?.pc_key_configured ? 'var(--kim-green)' : 'var(--kim-red)',
            fontWeight: 500,
          }}
        >
          {cfg?.pc_key_configured ? '✓ Configured' : '⚠ Missing from .env'}
        </span>
      </Row>

      {err && (
        <div style={{ color: 'var(--kim-red)', fontSize: 13, marginBottom: 18 }}>
          {err}
        </div>
      )}

      <SectionLabel>Pairing</SectionLabel>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 10 }}>
        <button
          type="button"
          className="kr-btn kr-btn-primary"
          onClick={() => setPairOpen(true)}
          disabled={!canPair}
          title={
            !cfg?.url
              ? 'Set a relay URL first.'
              : !cfg.pc_key_configured
              ? 'Add RELAY_PC_API_KEY to .env first.'
              : ''
          }
        >
          Pair a phone
        </button>
        <span style={{ color: 'var(--kim-text-3)', fontSize: 12 }}>
          You'll get a QR. Open Kim on the phone → Settings → Phone Relay → paste it.
        </span>
      </div>

      <PairingModal
        open={pairOpen}
        onClose={() => setPairOpen(false)}
        projectRoot={settings.project_root || undefined}
      />
    </>
  );
}

// ── Main shell ────────────────────────────────────────────────────────────────

export { PaneMCP, PaneFeedback, PaneAbout, PaneRelay };
