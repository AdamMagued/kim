interface Props {
  currentVersion: string;
  latestVersion: string;
  releaseNotes: string;
  downloadUrl: string;
  onDismiss: () => void;
}

export function UpdateModal({
  currentVersion,
  latestVersion,
  releaseNotes,
  downloadUrl,
  onDismiss,
}: Props) {
  return (
    <div
      className="kim-modal-backdrop"
      onClick={e => {
        if (e.target === e.currentTarget) onDismiss();
      }}
    >
      <div className="kim-modal kim-modal--update" role="dialog" aria-labelledby="update-title">
        <div className="kim-update__hero">
          <div className="kim-update__icon" aria-hidden>
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 3v12M6 9l6-6 6 6" />
              <path d="M5 21h14" />
            </svg>
          </div>
          <div style={{ minWidth: 0 }}>
            <div id="update-title" className="kim-update__title">
              Update available
            </div>
            <div className="kim-update__version">
              <span>v{currentVersion}</span>
              <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}>
                <path d="M5 3l5 5-5 5" />
              </svg>
              <span style={{ fontWeight: 600, color: 'var(--accent)' }}>v{latestVersion}</span>
            </div>
          </div>
        </div>

        {releaseNotes && (
          <div className="kim-update__notes">{releaseNotes}</div>
        )}

        <div className="kim-update__actions">
          <button onClick={onDismiss} className="kim-btn kim-btn--secondary">
            Later
          </button>
          <a
            href={downloadUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onDismiss}
            className="kim-btn kim-btn--primary"
          >
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 2v9M4 7l4 4 4-4M3 14h10" />
            </svg>
            <span>Download</span>
          </a>
        </div>
      </div>
    </div>
  );
}
