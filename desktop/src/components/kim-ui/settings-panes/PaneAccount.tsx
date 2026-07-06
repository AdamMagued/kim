import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { confirm } from '@tauri-apps/plugin-dialog';
import type { KimAccount, GoogleAccount, GoogleApiAccount } from '../../../types';
import { toast } from '../../Toast';
import { SectionLabel, Row } from './primitives';

interface GoogleApiStatus extends GoogleApiAccount {
  connected: boolean;
  email: string;
  needs_reauth?: boolean;
  error?: string;
}

function PaneAccount({
  account,
  onAccountChange,
}: {
  account: KimAccount;
  onAccountChange: (a: KimAccount) => Promise<void>;
}) {
  const [nameVal, setNameVal] = useState(account.display_name);
  const [editingName, setEditingName] = useState(false);
  // finding #4: separate boolean for form visibility so the secret value never
  // doubles as a UI sentinel (previously `token === 'show'` triggered the form).
  const [showTokenForm, setShowTokenForm] = useState(false);
  const [token, setToken] = useState('');
  const [verifying, setVerifying] = useState(false);
  const [tokenError, setTokenError] = useState('');
  const [saving, setSaving] = useState(false);
  const [googleEmail, setGoogleEmail] = useState('');
  const [googleAuthuser, setGoogleAuthuser] = useState('0');
  const [googleError, setGoogleError] = useState('');
  const [googleApiStatus, setGoogleApiStatus] = useState<GoogleApiStatus | null>(account.google_api_account ? { ...account.google_api_account } : null);
  const [googleApiBusy, setGoogleApiBusy] = useState(false);
  const [googleApiError, setGoogleApiError] = useState('');

  const googleAccounts = account.google_accounts ?? [];
  const activeGoogleEmail = account.google_active_account
    ?? googleAccounts[0]?.email
    ?? '';
  const activeGoogleAccount = googleAccounts.find(a => a.email === activeGoogleEmail) ?? googleAccounts[0] ?? null;

  useEffect(() => {
    let cancelled = false;
    invoke<GoogleApiStatus>('google_oauth_status')
      .then(status => {
        if (!cancelled) {
          setGoogleApiStatus(status);
          setGoogleApiError(status.error ?? '');
        }
      })
      .catch(() => {
        if (!cancelled) setGoogleApiStatus(account.google_api_account ? { ...account.google_api_account } : null);
      });
    return () => { cancelled = true; };
  }, [account.google_api_account]);

  async function connectGoogleApi() {
    setGoogleApiBusy(true);
    setGoogleApiError('');
    try {
      const status = await invoke<GoogleApiStatus>('google_oauth_start');
      setGoogleApiStatus(status);
      await onAccountChange({ ...account, google_api_account: status });
      toast(status.email ? `Google connected as ${status.email}.` : 'Google connected for Kim.', 'success', 3000);
    } catch (err) {
      const message = String(err);
      setGoogleApiError(message);
      toast(message || 'Could not connect Google.', 'error', 6000);
    } finally {
      setGoogleApiBusy(false);
    }
  }

  async function disconnectGoogleApi() {
    setGoogleApiBusy(true);
    setGoogleApiError('');
    try {
      await invoke('google_oauth_disconnect');
      const next = { ...account, google_api_account: undefined };
      setGoogleApiStatus({ connected: false, email: '' });
      await onAccountChange(next);
      toast('Google disconnected from Kim.', 'success', 2500);
    } catch (err) {
      const message = String(err);
      setGoogleApiError(message);
      toast(message || 'Could not disconnect Google.', 'error', 5000);
    } finally {
      setGoogleApiBusy(false);
    }
  }

  async function testGoogleApi() {
    setGoogleApiBusy(true);
    setGoogleApiError('');
    try {
      const status = await invoke<GoogleApiStatus>('google_oauth_test');
      setGoogleApiStatus(status);
      await onAccountChange({ ...account, google_api_account: status });
      toast('Google for Kim is working.', 'success', 2500);
    } catch (err) {
      const message = String(err);
      setGoogleApiError(message);
      toast(message || 'Google test failed.', 'error', 6000);
    } finally {
      setGoogleApiBusy(false);
    }
  }

  async function saveGoogleAccountPatch(
    google_accounts: GoogleAccount[],
    google_active_account: string | undefined,
    successMessage?: string,
  ) {
    setSaving(true);
    try {
      await onAccountChange({
        ...account,
        google_accounts,
        google_active_account,
      });
      if (successMessage) toast(successMessage, 'success', 2500);
    } catch (err) {
      // M11: was try/finally only — a save failure surfaced as an unhandled
      // rejection with no user feedback.
      toast(`Could not save Google accounts: ${String(err)}`, 'error', 4000);
    } finally {
      setSaving(false);
    }
  }

  async function addGoogleAccount() {
    const email = googleEmail.trim().toLowerCase();
    const index = Number.parseInt(googleAuthuser.trim(), 10);
    setGoogleError('');
    if (!/^\S+@\S+\.\S+$/.test(email)) {
      setGoogleError('Enter the Google email address you use in the Gemini WebView.');
      return;
    }
    if (!Number.isInteger(index) || index < 0) {
      setGoogleError('authuser must be 0 or a positive whole number.');
      return;
    }
    const nextAccounts = [
      ...googleAccounts.filter(a => a.email.trim().toLowerCase() !== email),
      { email, authuser_index: index },
    ].sort((a, b) => a.authuser_index - b.authuser_index || a.email.localeCompare(b.email));
    await saveGoogleAccountPatch(nextAccounts, email, 'Google account linked for Gemini.');
    setGoogleEmail('');
    setGoogleAuthuser(String(index + 1));
  }

  async function setActiveGoogle(email: string) {
    const selected = googleAccounts.find(a => a.email === email);
    await saveGoogleAccountPatch(googleAccounts, selected?.email, selected ? `Gemini will use authuser=${selected.authuser_index}.` : undefined);
  }

  async function removeGoogleAccount(email: string) {
    const nextAccounts = googleAccounts.filter(a => a.email !== email);
    const nextActive = activeGoogleEmail === email ? nextAccounts[0]?.email : activeGoogleEmail;
    await saveGoogleAccountPatch(nextAccounts, nextActive, 'Google account removed.');
  }

  async function openActiveGemini() {
    const authuser = activeGoogleAccount?.authuser_index;
    const url = authuser === undefined
      ? 'https://gemini.google.com/app'
      : `https://gemini.google.com/app?authuser=${authuser}`;
    try {
      await invoke<string>('open_browser_signin_window', {
        url,
        providerName: 'Gemini',
      });
      await invoke('show_browser_window').catch(() => {});
      toast('Gemini opened in Kim. Complete Google sign-in there if needed.', 'info', 6000);
    } catch (err) {
      toast(typeof err === 'string' ? err : 'Could not open Gemini.', 'error', 5000);
    }
  }

  useEffect(() => {
    setNameVal(account.display_name);
  }, [account.display_name]);

  async function saveName() {
    if (nameVal.trim() && nameVal !== account.display_name) {
      setSaving(true);
      try {
        await onAccountChange({ ...account, display_name: nameVal.trim() });
        toast('Name updated', 'success', 1800);
      } finally {
        setSaving(false);
      }
    } else {
      setNameVal(account.display_name);
    }
    setEditingName(false);
  }

  async function verifyAndConnect() {
    if (!token.trim()) return;
    setVerifying(true);
    setTokenError('');
    try {
      const user = await invoke<{ login: string; name: string | null; avatar_url: string }>('verify_github_pat', {
        token: token.trim(),
      });
      // Store PAT in OS keychain before updating account state.
      // save_account (called by onAccountChange) also strips github_token from
      // account.json, so the secret never touches disk.
      await invoke('store_github_token', { token: token.trim() });
      await onAccountChange({
        ...account,
        github_token: token.trim(),
        github_username: user.login,
        github_avatar_url: user.avatar_url,
      });
      setToken('');
      setShowTokenForm(false);
      toast(`Connected as ${user.login}`, 'success', 2500);
    } catch (err) {
      setTokenError(String(err));
    } finally {
      setVerifying(false);
    }
  }

  // M11: these three used the synchronous browser `confirm()` — Tauri webviews
  // can return falsy from it, silently no-oping destructive actions. Use the
  // async plugin-dialog confirm (same as RevampSidebar's remove-project flow).
  async function disconnectGitHub() {
    const ok = await confirm('Disconnect GitHub? Gist sync will stop working.', {
      title: 'Disconnect GitHub?',
      kind: 'warning',
    });
    if (!ok) return;
    try {
      // M11: save the account state FIRST, then remove the PAT from the
      // keychain. The old order wiped the keychain before an unguarded save —
      // a save failure left the token gone while account.json still said
      // "connected" (plus an unhandled rejection).
      await onAccountChange({
        ...account,
        github_token: undefined,
        github_username: undefined,
        github_avatar_url: undefined,
        gist_id: undefined,
      });
      await invoke('delete_github_token').catch(() => {});
      toast('GitHub disconnected', 'success', 2000);
    } catch (err) {
      toast(`Could not disconnect GitHub: ${String(err)}`, 'error', 4000);
    }
  }

  async function resetOnboarding() {
    const ok = await confirm(
      'Reset onboarding? Kim will forget your account and re-run the welcome flow next launch.',
      { title: 'Reset onboarding?', kind: 'warning' },
    );
    if (!ok) return;
    try {
      await invoke('reset_onboarding');
      toast('Onboarding reset — restart Kim to see the welcome flow.', 'success', 4000);
    } catch (err) {
      toast(`Reset failed: ${String(err)}`, 'error', 4000);
    }
  }

  async function deleteAllSessions() {
    const ok = await confirm('Permanently delete ALL local session files? This cannot be undone.', {
      title: 'Delete all sessions?',
      kind: 'warning',
    });
    if (!ok) return;
    try {
      await invoke('delete_all_sessions');
      toast('All sessions deleted.', 'success', 3000);
    } catch (err) {
      toast(`Delete failed: ${String(err)}`, 'error', 4000);
    }
  }

  const initial = (account.display_name?.trim()[0] || 'A').toUpperCase();

  return (
    <>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 18,
          padding: 18,
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-border)',
          borderRadius: 12,
          marginBottom: 22,
        }}
      >
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: 999,
            background: 'linear-gradient(135deg, var(--kim-accent), #c98968)',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--kim-on-accent)',
            fontWeight: 700,
            fontSize: 22,
            overflow: 'hidden',
          }}
        >
          {account.github_avatar_url ? (
            <img src={account.github_avatar_url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : (
            initial
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          {editingName ? (
            <input
              autoFocus
              id="account-display-name"
              aria-label="Display name"
              value={nameVal}
              onChange={(e) => setNameVal(e.target.value)}
              onBlur={() => void saveName()}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void saveName();
                if (e.key === 'Escape') {
                  setNameVal(account.display_name);
                  setEditingName(false);
                }
              }}
              className="kr-input"
              style={{ fontSize: 15 }}
            />
          ) : (
            <div style={{ fontSize: 16, marginBottom: 3 }}>{account.display_name}</div>
          )}
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
            {account.github_username ? `github · ${account.github_username}` : 'local'}
          </div>
        </div>
        <button type="button" className="kr-btn" onClick={() => setEditingName(true)} disabled={saving}>
          {editingName ? 'Saving…' : 'Edit name'}
        </button>
      </div>

      <SectionLabel>integrations</SectionLabel>
      <Row
        title="GitHub"
        subtitle="Required for Gist backup sync. Use a personal access token with gist and read:user scopes."
      >
        {account.github_token ? (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <span
              style={{
                fontSize: 12,
                color: 'var(--kim-green)',
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
              }}
            >
              ● connected
            </span>
            <button type="button" className="kr-btn" onClick={disconnectGitHub}>
              Disconnect
            </button>
          </div>
        ) : (
          <button type="button" className="kr-btn kr-btn-primary" onClick={() => setShowTokenForm(true)}>
            Connect
          </button>
        )}
      </Row>

      {!account.github_token && showTokenForm && (
        <div
          style={{
            background: 'var(--kim-surface)',
            border: '1px solid var(--kim-border)',
            borderRadius: 12,
            padding: '14px 16px',
            marginBottom: 10,
          }}
        >
          <div style={{ fontSize: 12.5, color: 'var(--kim-text-2)', marginBottom: 10, lineHeight: 1.5 }}>
            Paste a GitHub personal access token (scopes: <code>gist</code>, <code>read:user</code>). Create one at{' '}
            <span style={{ color: 'var(--kim-accent)' }}>github.com/settings/tokens</span>.
          </div>
          <input
            id="account-github-pat"
            type="password"
            aria-label="GitHub personal access token"
            className="kr-input"
            placeholder="ghp_…"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            style={{ marginBottom: 10 }}
          />
          {tokenError && (
            <div style={{ fontSize: 12, color: 'var(--kim-red)', marginBottom: 10 }}>{tokenError}</div>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button type="button" className="kr-btn" onClick={() => { setShowTokenForm(false); setToken(''); setTokenError(''); }}>
              Cancel
            </button>
            <button type="button" className="kr-btn kr-btn-primary" onClick={verifyAndConnect} disabled={verifying || !token.trim()}>
              {verifying ? 'Verifying…' : 'Connect'}
            </button>
          </div>
        </div>
      )}

      <SectionLabel>Google for Kim (API)</SectionLabel>
      <Row
        title={googleApiStatus?.connected ? 'Connected as Google' : 'Use Gemini through your Google account'}
        subtitle={googleApiStatus?.connected
          ? (googleApiStatus.email ? `${googleApiStatus.email}${googleApiStatus.needs_reauth ? ' · reconnect required' : ''}` : 'Connected')
          : 'Continue with Google once, then chat normally. No API key setup in Kim.'}
      >
        {googleApiStatus?.connected ? (
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            <button type="button" className="kr-btn" onClick={testGoogleApi} disabled={googleApiBusy}>
              {googleApiBusy ? 'Checking…' : 'Test'}
            </button>
            <button type="button" className="kr-btn" onClick={disconnectGoogleApi} disabled={googleApiBusy}>
              Disconnect
            </button>
          </div>
        ) : (
          <button type="button" className="kr-btn kr-btn-primary" onClick={connectGoogleApi} disabled={googleApiBusy}>
            {googleApiBusy ? 'Opening Google…' : 'Continue with Google'}
          </button>
        )}
      </Row>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: -6, marginBottom: 18, lineHeight: 1.5 }}>
        This powers the <code>gemini</code> API provider. Refresh tokens stay in OS secure storage; Kim only passes short-lived access to the Python agent.
      </div>
      {googleApiError && <div style={{ color: 'var(--kim-red)', fontSize: 12, marginBottom: 12 }}>{googleApiError}</div>}

      <SectionLabel>Google for Browser: Gemini</SectionLabel>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 12, lineHeight: 1.5 }}>
        Kim routes Gemini with Google's <code>authuser</code> index and the cookies already in the in-app WebView. It does not save or auto-fill Google passwords.
      </div>

      {googleAccounts.length > 0 && (
        <div style={{ display: 'grid', gap: 8, marginBottom: 12 }}>
          {googleAccounts.map(g => {
            const active = g.email === activeGoogleEmail;
            return (
              <div
                key={g.email}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '10px 12px',
                  background: 'var(--kim-surface)',
                  borderRadius: 12,
                  border: active ? '1px solid var(--kim-accent)' : '1px solid var(--kim-border)',
                }}
              >
                <div style={{
                  width: 28,
                  height: 28,
                  borderRadius: '50%',
                  background: active ? 'var(--kim-accent)' : 'var(--kim-surface-raised)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: active ? 'var(--kim-on-accent)' : 'var(--kim-text-2)',
                  fontSize: 12,
                  fontWeight: 600,
                }}>
                  G
                </div>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--kim-text)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{g.email}</div>
                  <div style={{ fontSize: 11, color: 'var(--kim-text-3)' }}>authuser={g.authuser_index}{active ? ' · active' : ''}</div>
                </div>
                {!active && (
                  <button type="button" className="kr-btn" onClick={() => setActiveGoogle(g.email)} disabled={saving}>
                    Use
                  </button>
                )}
                <button type="button" className="kr-btn" onClick={() => removeGoogleAccount(g.email)} disabled={saving}>
                  Remove
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        <input
          id="account-google-email"
          type="email"
          aria-label="Google account email"
          className="kr-input"
          placeholder="you@gmail.com"
          value={googleEmail}
          onChange={e => { setGoogleEmail(e.target.value); setGoogleError(''); }}
          style={{ flex: 1 }}
        />
        <input
          type="number"
          min={0}
          step={1}
          className="kr-input"
          aria-label="Google authuser index"
          value={googleAuthuser}
          onChange={e => { setGoogleAuthuser(e.target.value); setGoogleError(''); }}
          style={{ width: 80 }}
        />
        <button type="button" className="kr-btn kr-btn-primary" onClick={addGoogleAccount} disabled={saving || !googleEmail.trim()}>
          Link
        </button>
      </div>
      <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginBottom: 12, lineHeight: 1.5 }}>
        Use <code>0</code> for the first Google account in this WebView profile, <code>1</code> for the second, and so on. Test with “Open active Gemini”.
      </div>
      {googleError && <div style={{ color: 'var(--kim-red)', fontSize: 12, marginBottom: 10 }}>{googleError}</div>}
      <button type="button" className="kr-btn" onClick={openActiveGemini} style={{ width: '100%', justifyContent: 'center', marginBottom: 20 }}>
        Open active Gemini{activeGoogleAccount ? ` (${activeGoogleAccount.email}, authuser=${activeGoogleAccount.authuser_index})` : ''}
      </button>

      <SectionLabel>danger zone</SectionLabel>
      <Row danger title="Reset onboarding" subtitle="Forget your account and re-run the welcome flow on next launch.">
        <button
          type="button"
          className="kr-btn"
          onClick={resetOnboarding}
          style={{ color: 'var(--kim-red)', borderColor: 'rgba(212,148,151,0.3)' }}
        >
          Reset…
        </button>
      </Row>
      <Row danger title="Delete all sessions" subtitle="Permanently wipe local JSONL files. This cannot be undone.">
        <button
          type="button"
          className="kr-btn"
          onClick={deleteAllSessions}
          style={{ color: 'var(--kim-red)', borderColor: 'rgba(212,148,151,0.3)' }}
        >
          Delete…
        </button>
      </Row>
    </>
  );
}

export { PaneAccount };
