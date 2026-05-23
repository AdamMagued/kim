import { useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

interface Props {
  currentVersion: string;
  latestVersion: string;
  releaseNotes: string;
  onDismiss: () => void;
}

type Stage = 'idle' | 'updating' | 'done' | 'error';

export function UpdateModal({ currentVersion, latestVersion, releaseNotes, onDismiss }: Props) {
  const [platform, setPlatform] = useState('');
  const [stage, setStage] = useState<Stage>('idle');
  const [progress, setProgress] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState('');
  const progressRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    invoke<string>('get_platform_info').then(setPlatform).catch(() => {});
  }, []);

  useEffect(() => {
    if (progressRef.current) {
      progressRef.current.scrollTop = progressRef.current.scrollHeight;
    }
  }, [progress]);

  async function handleUpdate() {
    setStage('updating');
    setProgress([]);
    setErrorMsg('');

    const unlisten = await listen<string>('kim-update-progress', e => {
      setProgress(prev => [...prev, e.payload]);
    });

    try {
      await invoke('run_update');
      // If we get here without restarting, source was already up to date
      setStage('done');
    } catch (e: unknown) {
      setErrorMsg(String(e));
      setStage('error');
    } finally {
      unlisten();
    }
  }

  return (
    <div
      className="kim-modal-backdrop"
      onClick={e => { if (stage === 'idle' && e.target === e.currentTarget) onDismiss(); }}
    >
      <div className="kim-modal kim-modal--update" role="dialog" aria-labelledby="update-title">

        {/* Header */}
        <div className="kim-update__hero">
          <div className="kim-update__icon" aria-hidden>
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3v12M6 9l6-6 6 6" />
              <path d="M5 21h14" />
            </svg>
          </div>
          <div style={{ minWidth: 0 }}>
            <div id="update-title" className="kim-update__title">Update available</div>
            <div className="kim-update__version">
              <span>v{currentVersion}</span>
              <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}>
                <path d="M5 3l5 5-5 5" />
              </svg>
              <span style={{ fontWeight: 600, color: 'var(--accent)' }}>v{latestVersion}</span>
            </div>
            {platform && (
              <div style={{ fontSize: '0.72rem', opacity: 0.55, marginTop: 2 }}>
                {platform}
              </div>
            )}
          </div>
        </div>

        {/* Release notes — only shown in idle state */}
        {stage === 'idle' && releaseNotes && (
          <div className="kim-update__notes">{releaseNotes}</div>
        )}

        {/* Progress log */}
        {(stage === 'updating' || stage === 'done' || stage === 'error') && (
          <div
            ref={progressRef}
            className="kim-update__progress"
            style={{
              background: 'var(--surface-2, rgba(0,0,0,0.12))',
              borderRadius: 8,
              padding: '10px 12px',
              fontSize: '0.72rem',
              fontFamily: 'monospace',
              maxHeight: 160,
              overflowY: 'auto',
              margin: '12px 0',
              lineHeight: 1.6,
              color: 'var(--text-secondary)',
            }}
          >
            {progress.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
            {stage === 'updating' && (
              <div style={{ opacity: 0.5 }}>
                <span className="kim-update__spinner" /> Working…
              </div>
            )}
            {stage === 'error' && (
              <div style={{ color: 'var(--error, #f87171)', marginTop: 4 }}>
                ✗ {errorMsg}
              </div>
            )}
            {stage === 'done' && (
              <div style={{ color: 'var(--success, #4ade80)', marginTop: 4 }}>
                ✓ {progress.some(l => l.includes('already up to date')) ? 'Already up to date.' : 'Restarting…'}
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="kim-update__actions">
          {stage === 'idle' && (
            <>
              <button onClick={onDismiss} className="kim-btn kim-btn--secondary">
                Later
              </button>
              <button onClick={handleUpdate} className="kim-btn kim-btn--primary">
                <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M8 2v9M4 7l4 4 4-4M3 14h10" />
                </svg>
                <span>Update Now</span>
              </button>
            </>
          )}
          {stage === 'updating' && (
            <button disabled className="kim-btn kim-btn--primary" style={{ opacity: 0.6, cursor: 'not-allowed' }}>
              Updating…
            </button>
          )}
          {stage === 'done' && (
            <button onClick={onDismiss} className="kim-btn kim-btn--primary">
              {progress.some(l => l.toLowerCase().includes('already up to date')) ? 'Close' : 'Restarting…'}
            </button>
          )}
          {stage === 'error' && (
            <>
              <button onClick={onDismiss} className="kim-btn kim-btn--secondary">
                Close
              </button>
              <button onClick={handleUpdate} className="kim-btn kim-btn--primary">
                Retry
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
