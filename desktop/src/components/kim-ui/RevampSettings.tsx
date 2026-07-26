import { useEffect, useRef, useState } from 'react';
import type { Settings, KimAccount } from '../../types';
import { PaneSchedule } from '../settings/SchedulePane';
import { PaneHeader } from './settings-panes/primitives';
import { PaneAI } from './settings-panes/PaneAI';
import { PaneAccount } from './settings-panes/PaneAccount';
import { PaneAppearance, PanePaths, PaneData } from './settings-panes/PaneSystem';
import { PaneMCP, PaneFeedback, PaneAbout } from './settings-panes/PaneInfo';

// ── Types ─────────────────────────────────────────────────────────────────────

type PaneId =
  | 'appearance'
  | 'ai'
  | 'paths'
  | 'data'
  | 'schedule'
  | 'account'
  | 'mcp'
  | 'feedback'
  | 'about';

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  onClose: () => void;
  appVersion: string;
  onCheckUpdate: () => void;
  account: KimAccount;
  onAccountChange: (account: KimAccount) => Promise<void>;
  initialPane?: PaneId;
}

// ── Static config ─────────────────────────────────────────────────────────────

const NAV: { id: PaneId; label: string; icon: PaneId }[] = [
  { id: 'appearance', label: 'Appearance', icon: 'appearance' },
  { id: 'ai', label: 'AI', icon: 'ai' },
  { id: 'paths', label: 'Paths', icon: 'paths' },
  { id: 'data', label: 'Data', icon: 'data' },
  { id: 'schedule', label: 'Schedules', icon: 'schedule' },
  { id: 'account', label: 'Account', icon: 'account' },
  { id: 'mcp', label: 'MCP', icon: 'mcp' },
  { id: 'feedback', label: 'Feedback', icon: 'feedback' },
  { id: 'about', label: 'About', icon: 'about' },
];

// ── Icons ─────────────────────────────────────────────────────────────────────

function NavIcon({ name }: { name: PaneId }) {
  const stroke = {
    stroke: 'currentColor',
    strokeWidth: 1.3,
    fill: 'none',
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };
  switch (name) {
    case 'appearance':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path
            {...stroke}
            d="M8 1.5c3.5 0 6.5 2.5 6.5 5.5 0 2-1.5 3-3 3h-1c-.7 0-1 .5-1 1s.3 1 .7 1.3c.5.3.8.7.8 1.2 0 .8-.7 1.5-1.5 1.5C5.5 14.5 1.5 11 1.5 7 1.5 4 4.5 1.5 8 1.5z"
          />
          <circle {...stroke} cx="5" cy="6.5" r=".8" />
          <circle {...stroke} cx="8" cy="4.5" r=".8" />
          <circle {...stroke} cx="11" cy="6.5" r=".8" />
        </svg>
      );
    case 'ai':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3M3.5 3.5l2 2M10.5 10.5l2 2M3.5 12.5l2-2M10.5 5.5l2-2" />
        </svg>
      );
    case 'paths':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M1.5 4.5a1 1 0 011-1h3l1.5 1.5h6a1 1 0 011 1v6a1 1 0 01-1 1h-10.5a1 1 0 01-1-1v-7.5z" />
        </svg>
      );
    case 'data':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <ellipse {...stroke} cx="8" cy="3.5" rx="5.5" ry="2" />
          <path {...stroke} d="M2.5 3.5v9c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2v-9M2.5 8c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2" />
        </svg>
      );
    case 'schedule':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <rect {...stroke} x="2.5" y="3" width="11" height="11" rx="1.5" />
          <path {...stroke} d="M5 1.8v3M11 1.8v3M2.5 6h11M5.2 9h2M5.2 11.5h4.5" />
        </svg>
      );
    case 'account':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <circle {...stroke} cx="8" cy="5.5" r="2.5" />
          <path {...stroke} d="M2.5 14c0-2.5 2.5-4.5 5.5-4.5s5.5 2 5.5 4.5" />
        </svg>
      );
    case 'mcp':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M5.5 1.5v3M10.5 1.5v3M3.5 4.5h9v3a4.5 4.5 0 01-9 0v-3zM8 12v2.5" />
        </svg>
      );
    case 'feedback':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <path {...stroke} d="M2 4.5a2 2 0 012-2h8a2 2 0 012 2V10a2 2 0 01-2 2H6l-3 2.5V12h-1a1 1 0 010-2V4.5z" />
        </svg>
      );
    case 'about':
      return (
        <svg width="15" height="15" viewBox="0 0 16 16">
          <circle {...stroke} cx="8" cy="8" r="6.5" />
          <path {...stroke} d="M8 7v4M8 5v.5" />
        </svg>
      );
  }
}

const PANE_META: Record<PaneId, { title: string; subtitle: string }> = {
  appearance: { title: 'Appearance', subtitle: 'How Kim looks on your machine.' },
  ai: { title: 'AI', subtitle: 'Which model runs Kim and how it behaves while working.' },
  paths: { title: 'Paths', subtitle: 'Where Kim reads and writes on disk.' },
  data: { title: 'Data', subtitle: 'Your sessions live on your machine. Nothing leaves unless you sync.' },
  schedule: { title: 'Schedules', subtitle: 'Plan recurring tasks and run due work on demand.' },
  account: { title: 'Account', subtitle: 'How you show up across Kim and your linked services.' },
  mcp: { title: 'MCP', subtitle: 'Tool packs Kim can call — built-in and your own.' },
  feedback: { title: 'Feedback', subtitle: "Tell us what's on your mind. We read every one." },
  about: { title: 'About', subtitle: "What's running, and what's new." },
};

export function RevampSettings(props: Props) {
  const { settings, onChange, onClose, appVersion, onCheckUpdate, account, onAccountChange, initialPane } = props;
  const [active, setActive] = useState<PaneId>(initialPane ?? 'appearance');
  const modalRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Save trigger focus, focus first element on open, restore on close
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    const modal = modalRef.current;
    if (modal) {
      const focusable = modal.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusable.length > 0) focusable[0].focus();
    }
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

  // Close on Escape; trap Tab/Shift+Tab within modal
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose();
        return;
      }
      if (e.key === 'Tab') {
        const modal = modalRef.current;
        if (!modal) return;
        const focusable = Array.from(
          modal.querySelectorAll<HTMLElement>(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const meta = PANE_META[active];

  return (
    <div
      className="kim-settings-revamp"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="kim-revamp-settings-title"
    >
      <div
        ref={modalRef}
        className="kr-glass kim-settings-revamp__dialog"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Left nav */}
        <div className="kim-settings-revamp__nav">
          <div
            className="kim-settings-revamp__brand"
            id="kim-revamp-settings-title"
          >
            <svg className="kim-settings-revamp__brand-icon" width="18" height="18" viewBox="0 0 16 16" fill="none">
              <path d="M8 1v14M1 8h14M3 3l10 10M13 3L3 13" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
            </svg>
            <span className="kim-settings-revamp__brand-label">Settings</span>
          </div>
          {NAV.map((n) => (
            <button
              key={n.id}
              className={`kim-settings-revamp__nav-item${active === n.id ? ' kim-settings-revamp__nav-item--active' : ''}`}
              onClick={() => setActive(n.id)}
              aria-current={active === n.id ? 'page' : undefined}
              title={n.label}
            >
              <NavIcon name={n.icon} />
              <span className="kim-settings-revamp__nav-label">{n.label}</span>
            </button>
          ))}
        </div>

        {/* Body */}
        <div className="kim-settings-revamp__body">
          <PaneHeader title={meta.title} subtitle={meta.subtitle} onClose={onClose} />
          {active === 'appearance' && <PaneAppearance settings={settings} onChange={onChange} />}
          {active === 'ai' && <PaneAI settings={settings} onChange={onChange} />}
          {active === 'paths' && <PanePaths settings={settings} onChange={onChange} />}
          {active === 'data' && <PaneData account={account} onAccountChange={onAccountChange} />}
          {active === 'schedule' && <PaneSchedule settings={settings} onChange={onChange} />}
          {active === 'account' && <PaneAccount account={account} onAccountChange={onAccountChange} />}
          {active === 'mcp' && <PaneMCP />}
          {active === 'feedback' && <PaneFeedback />}
          {active === 'about' && <PaneAbout appVersion={appVersion} onCheckUpdate={onCheckUpdate} />}
        </div>
      </div>
    </div>
  );
}
