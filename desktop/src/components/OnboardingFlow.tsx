import { useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { KimAccount } from '../types';

interface Props {
  onComplete: (account: KimAccount) => void;
}

function OrbitLogo() {
  return (
    <svg width="52" height="52" viewBox="0 0 52 52" fill="none" className="kim-logo-mark" style={{ width: 52, height: 52 }}>
      {/* Central node */}
      <circle cx="26" cy="26" r="6" fill="currentColor" />
      {/* Orbit arc — top-right */}
      <path
        d="M26 8 A18 18 0 0 1 44 26"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
        opacity="0.9"
      />
      {/* Orbit arc — bottom-left */}
      <path
        d="M26 44 A18 18 0 0 1 8 26"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        fill="none"
        opacity="0.9"
      />
      {/* Orbit dot top-right */}
      <circle cx="44" cy="26" r="3" fill="currentColor" opacity="0.7" />
      {/* Orbit dot bottom-left */}
      <circle cx="8" cy="26" r="3" fill="currentColor" opacity="0.7" />
    </svg>
  );
}

function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}

function CheckCircleIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="10" cy="10" r="8" />
      <path d="M6.5 10.5l2.5 2.5 4.5-5" />
    </svg>
  );
}

export function OnboardingFlow({ onComplete }: Props) {
  const [name, setName] = useState('');
  const [token, setToken] = useState('');
  const [tokenStep, setTokenStep] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [githubUser, setGithubUser] = useState<{ login: string; name: string | null; avatar_url: string } | null>(null);
  const [tokenError, setTokenError] = useState('');
  const [saving, setSaving] = useState(false);

  const canContinue = name.trim().length >= 1;

  async function verifyToken() {
    if (!token.trim()) return;
    setVerifying(true);
    setTokenError('');
    try {
      const user = await invoke<{ login: string; name: string | null; avatar_url: string }>(
        'verify_github_pat',
        { token: token.trim() }
      );
      setGithubUser(user);
    } catch (err) {
      setTokenError(String(err));
    } finally {
      setVerifying(false);
    }
  }

  async function handleFinish() {
    if (!canContinue) return;
    setSaving(true);

    const account: KimAccount = {
      display_name: name.trim(),
      github_username: githubUser?.login,
      github_token: githubUser ? token.trim() : undefined,
      github_avatar_url: githubUser?.avatar_url,
      gist_id: undefined,
      created_at: new Date().toISOString(),
    };

    try {
      await invoke('save_account', { account });
      onComplete(account);
    } catch {
      setSaving(false);
    }
  }

  return (
    <div className="kim-onboarding">
      <div className="kim-onboarding__card">
        <div className="kim-onboarding__logo">
          <OrbitLogo />
        </div>

        <div className="kim-onboarding__title">Welcome to Kim</div>
        <div className="kim-onboarding__subtitle">
          A local AI agent that works on your machine.<br />
          No account required — your data stays on device.
        </div>

        {/* Step 1: Name */}
        <div className="kim-onboarding__step">
          <div className="kim-onboarding__step-label">Your name</div>
          <input
            type="text"
            className="kim-input"
            placeholder="What should Kim call you?"
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && canContinue) setTokenStep(true); }}
            autoFocus
            style={{ width: '100%' }}
          />
        </div>

        {/* Step 2: GitHub token (optional, collapsible) */}
        {!tokenStep ? (
          <>
            <div className="kim-onboarding__divider">optional</div>
            <button
              className="kim-onboarding__github"
              onClick={() => setTokenStep(true)}
              disabled={!canContinue}
            >
              <GitHubIcon />
              <span>Connect GitHub for backup sync</span>
              <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" style={{ marginLeft: 'auto', opacity: 0.5 }}>
                <path d="M5 3l5 5-5 5" />
              </svg>
            </button>
            <button
              className="kim-btn kim-btn--primary"
              onClick={handleFinish}
              disabled={!canContinue || saving}
              style={{ width: '100%', marginTop: 16 }}
            >
              {saving ? 'Setting up…' : 'Get started'}
            </button>
          </>
        ) : (
          <div className="kim-onboarding__step">
            <div className="kim-onboarding__step-label">GitHub personal access token</div>
            <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 10, lineHeight: 1.5 }}>
              Create a token at{' '}
              <strong style={{ color: 'var(--text)' }}>github.com/settings/tokens</strong>
              {' '}with <code style={{ fontSize: 11 }}>gist</code> and <code style={{ fontSize: 11 }}>read:user</code> scopes.
              This lets Kim back up your account data to a private Gist.
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                type="password"
                className="kim-input"
                placeholder="ghp_..."
                value={token}
                onChange={e => { setToken(e.target.value); setGithubUser(null); setTokenError(''); }}
                style={{ flex: 1 }}
              />
              <button
                className="kim-btn kim-btn--secondary"
                onClick={verifyToken}
                disabled={!token.trim() || verifying || !!githubUser}
              >
                {verifying ? 'Checking…' : githubUser ? 'Connected' : 'Verify'}
              </button>
            </div>

            {tokenError && <div className="kim-onboarding__error">{tokenError}</div>}

            {githubUser && (
              <div className="kim-onboarding__success">
                <CheckCircleIcon />
                Connected as <strong>{githubUser.name ?? githubUser.login}</strong>
              </div>
            )}

            <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
              <button
                className="kim-btn kim-btn--secondary"
                onClick={() => { setTokenStep(false); setToken(''); setGithubUser(null); setTokenError(''); }}
                style={{ flex: 1 }}
              >
                Skip
              </button>
              <button
                className="kim-btn kim-btn--primary"
                onClick={handleFinish}
                disabled={!canContinue || saving}
                style={{ flex: 2 }}
              >
                {saving ? 'Setting up…' : 'Get started'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
