import type { ReactNode } from 'react';

// Shared layout primitives for the settings panes.
// Extracted from RevampSettings.tsx (file-split restructure).

function PaneHeader({ title, subtitle, onClose }: { title: string; subtitle?: string; onClose?: () => void }) {
  return (
    <div
      style={{
        marginBottom: 26,
        paddingBottom: 18,
        borderBottom: '1px solid var(--kim-border)',
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        gap: 12,
      }}
    >
      <div>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 500, letterSpacing: '-0.015em', color: 'var(--kim-text)' }}>
          {title}
        </h2>
        {subtitle && <p style={{ margin: '4px 0 0', color: 'var(--kim-text-3)', fontSize: 13 }}>{subtitle}</p>}
      </div>
      {onClose && (
        <button type="button" className="kr-icon-btn" onClick={onClose} style={{ width: 28, height: 28 }} aria-label="Close">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="kr-eyebrow" style={{ marginBottom: 12 }}>
      {children}
    </div>
  );
}

function Row({
  title,
  subtitle,
  children,
  danger,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  danger?: boolean;
}) {
  return (
    <div
      className={danger ? 'kr-danger' : ''}
      style={{
        background: 'var(--kim-surface)',
        border: '1px solid var(--kim-border)',
        borderRadius: 12,
        padding: '14px 16px',
        display: 'flex',
        alignItems: 'center',
        gap: 18,
        marginBottom: 10,
        paddingLeft: danger ? 14 : 16,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, color: 'var(--kim-text)', marginBottom: subtitle ? 3 : 0 }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12.5, color: 'var(--kim-text-3)', lineHeight: 1.5 }}>{subtitle}</div>}
      </div>
      <div style={{ flexShrink: 0 }}>{children}</div>
    </div>
  );
}

function Toggle({ on, onClick, ariaLabel, disabled }: { on: boolean; onClick: () => void; ariaLabel?: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      className={`kr-toggle${on ? ' kr-on' : ''}${disabled ? ' kr-toggle--disabled' : ''}`}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      aria-checked={on}
      aria-disabled={disabled}
      aria-label={ariaLabel}
      style={disabled ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
    />
  );
}

export { PaneHeader, SectionLabel, Row, Toggle };
