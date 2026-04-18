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
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.5)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={e => { if (e.target === e.currentTarget) onDismiss(); }}
    >
      <div
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: '16px',
          padding: '28px 32px',
          width: '480px',
          maxWidth: '90vw',
          boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' }}>
          <div
            style={{
              width: '40px',
              height: '40px',
              borderRadius: '10px',
              background: 'var(--accent)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '20px',
              flexShrink: 0,
            }}
          >
            🚀
          </div>
          <div>
            <div style={{ fontWeight: 700, fontSize: '16px', color: 'var(--text)' }}>
              Update available
            </div>
            <div style={{ fontSize: '13px', color: 'var(--text-muted)' }}>
              {currentVersion} → {latestVersion}
            </div>
          </div>
        </div>

        {/* Release notes */}
        {releaseNotes && (
          <div
            style={{
              background: 'var(--bg-card)',
              border: '1px solid var(--border)',
              borderRadius: '10px',
              padding: '14px 16px',
              marginBottom: '20px',
              maxHeight: '220px',
              overflowY: 'auto',
              fontSize: '13px',
              color: 'var(--text)',
              lineHeight: 1.6,
              whiteSpace: 'pre-wrap',
            }}
          >
            {releaseNotes}
          </div>
        )}

        {/* Actions */}
        <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
          <button
            onClick={onDismiss}
            style={{
              padding: '9px 20px',
              borderRadius: '8px',
              border: '1px solid var(--border)',
              background: 'transparent',
              color: 'var(--text-muted)',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            Later
          </button>
          <a
            href={downloadUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onDismiss}
            style={{
              padding: '9px 20px',
              borderRadius: '8px',
              border: 'none',
              background: 'var(--accent)',
              color: '#fff',
              cursor: 'pointer',
              fontSize: '14px',
              fontWeight: 600,
              textDecoration: 'none',
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            Download
          </a>
        </div>
      </div>
    </div>
  );
}
