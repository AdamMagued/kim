import { useState, useRef, useEffect } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { useAuthStatus } from '../hooks/useAuthStatus';
import { toast } from './Toast';

interface AuthIndicatorProps {
  /** The resolved provider string, e.g. "browser:chatgpt", "browser:gemini",
   *  or a plain "openai" / "claude" / "gemini". Anything outside the browser
   *  provider family causes the indicator to render nothing. */
  provider: string | null | undefined;
}

function providerLabel(p: string): string {
  switch (p) {
    case 'chatgpt':
    case 'openai':
      return 'ChatGPT';
    case 'claude':
    case 'anthropic':
      return 'Claude';
    case 'gemini':
    case 'google':
      return 'Gemini';
    case 'deepseek':
      return 'DeepSeek';
    case 'grok':
      return 'Grok';
    default:
      return p.charAt(0).toUpperCase() + p.slice(1);
  }
}

/**
 * Chip rendered just below the chat composer. Shows the current sign-in
 * status for the active browser provider:
 *   - "Not signed in — click to sign in" (gray dot)
 *   - "Signed in as email@x.com" (green dot)
 *   - "Checking…" (during the first probe after mount)
 *
 * Clicking the chip:
 *   - When signed out: triggers `provider_signin` → popup window
 *   - When signed in:  opens a menu with "Show provider window" /
 *     "Sign out" / "Re-check sign-in"
 */
export function AuthIndicator({ provider }: AuthIndicatorProps) {
  const normalized = (provider ?? '').toLowerCase().replace(/^browser:/, '').trim();
  const isBrowser = (provider ?? '').toLowerCase().startsWith('browser:');
  const { status, loading, probed, refresh, signIn, signOut, supported } = useAuthStatus(normalized);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Close the menu on outside-click.
  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [menuOpen]);

  // Only render for browser providers. API-key providers don't have a
  // signed-in concept worth showing here.
  if (!isBrowser || !supported) return null;

  const label = providerLabel(normalized);
  const email = status?.email || '';
  const signedIn = !!status?.signed_in;
  const errored = !signedIn && !!status?.error;

  const dotColor = loading
    ? 'var(--kim-text-3)'
    : signedIn
    ? '#22c55e'  // green
    : errored
    ? '#eab308'  // amber — probe failed, treat as "unknown"
    : 'var(--kim-text-3)';   // gray — confirmed signed-out

  const handleClick = async () => {
    if (signedIn) {
      setMenuOpen(v => !v);
      return;
    }
    try {
      await signIn();
      toast(`Opening ${label} sign-in…`, 'info', 2500);
    } catch (e) {
      toast(`Could not open sign-in: ${e}`, 'error', 5000);
    }
  };

  const handleShowProvider = async () => {
    setMenuOpen(false);
    try {
      await invoke('show_browser_window');
    } catch (e) {
      toast(`Could not show provider window: ${e}`, 'warning', 4000);
    }
  };

  const handleSignOut = async () => {
    setMenuOpen(false);
    try {
      await signOut();
      toast(`Signed out of ${label}`, 'info', 2500);
    } catch (e) {
      toast(`Sign out failed: ${e}`, 'error', 4000);
    }
  };

  const handleRecheck = async () => {
    setMenuOpen(false);
    await refresh();
  };

  return (
    <div className="kim-auth-indicator" ref={menuRef}>
      <button
        type="button"
        className={
          'kim-auth-indicator__chip' +
          (signedIn ? ' kim-auth-indicator__chip--ok' : errored ? ' kim-auth-indicator__chip--warn' : '')
        }
        onClick={handleClick}
        title={
          signedIn
            ? `Signed in to ${label}${email ? ' as ' + email : ''} — click to manage`
            : errored
            ? `Could not verify ${label} sign-in (${status?.error || 'unknown'}) — click to sign in`
            : `Not signed in to ${label} — click to sign in`
        }
      >
        <span
          className="kim-auth-indicator__dot"
          style={{ background: dotColor }}
          aria-hidden="true"
        />
        <span className="kim-auth-indicator__label">
          {loading && !probed
            ? `Checking ${label}…`
            : signedIn
            ? email
              ? `${label} · ${email}`
              : `Signed in to ${label}`
            : errored
            ? `${label} · click to sign in`
            : `Sign in with ${label}`}
        </span>
      </button>

      {menuOpen && signedIn && (
        <div className="kim-auth-indicator__menu" role="menu">
          <button type="button" className="kim-auth-indicator__menu-item" onClick={handleShowProvider}>
            Show provider window
          </button>
          <button type="button" className="kim-auth-indicator__menu-item" onClick={handleRecheck}>
            Re-check sign-in
          </button>
          <div className="kim-auth-indicator__menu-sep" />
          <button
            type="button"
            className="kim-auth-indicator__menu-item kim-auth-indicator__menu-item--danger"
            onClick={handleSignOut}
          >
            Sign out of {label}
          </button>
        </div>
      )}
    </div>
  );
}
