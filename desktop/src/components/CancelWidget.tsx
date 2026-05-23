import { invoke } from '@tauri-apps/api/core';
import '../index.css';

export function CancelWidget() {
  const handleCancel = () => {
    // Invoke the existing cancel_task command
    invoke('cancel_task').catch(console.error);
    // Let the backend handle closing this window and showing main via the agent completion hook
  };

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        height: '100vh',
        width: '100vw',
        background: 'transparent',
      }}
      data-tauri-drag-region
    >
      <button
        onClick={handleCancel}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '9px',
          padding: '9px 20px',
          background: 'var(--accent-gradient)',
          color: '#fff',
          border: 'none',
          borderRadius: '24px',
          fontSize: '13px',
          fontWeight: 600,
          cursor: 'pointer',
          letterSpacing: '0.01em',
          animation: 'kim-cancel-breathe 2.6s ease-in-out infinite',
          WebkitAppRegion: 'no-drag',
          transition: 'opacity 0.15s ease',
        } as React.CSSProperties}
        onMouseOver={(e) => (e.currentTarget.style.opacity = '0.88')}
        onMouseOut={(e) => (e.currentTarget.style.opacity = '1')}
        onMouseDown={(e) => (e.currentTarget.style.transform = 'scale(0.95)')}
        onMouseUp={(e) => (e.currentTarget.style.transform = '')}
      >
        <span className="kim-pulse-dot" style={{ background: 'rgba(255,255,255,0.85)' }} />
        Stop
      </button>
    </div>
  );
}
