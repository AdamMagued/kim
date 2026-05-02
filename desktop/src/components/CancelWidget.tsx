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
          gap: '8px',
          padding: '8px 16px',
          background: 'var(--bg-layer-2)',
          color: 'var(--text-1)',
          border: '1px solid var(--border-subtle)',
          borderRadius: '20px',
          fontSize: '13px',
          fontWeight: 500,
          cursor: 'pointer',
          boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15), 0 0 0 1px rgba(255,255,255,0.05)',
          WebkitAppRegion: 'no-drag', // Button itself should be clickable, not draggable
          transition: 'transform 0.1s ease, background 0.2s ease',
        } as React.CSSProperties}
        onMouseOver={(e) => (e.currentTarget.style.background = 'var(--bg-layer-3)')}
        onMouseOut={(e) => (e.currentTarget.style.background = 'var(--bg-layer-2)')}
        onMouseDown={(e) => (e.currentTarget.style.transform = 'scale(0.96)')}
        onMouseUp={(e) => (e.currentTarget.style.transform = 'scale(1)')}
      >
        <span className="kim-pulse-dot" style={{ background: 'var(--error)' }} />
        Cancel Task
      </button>
    </div>
  );
}
