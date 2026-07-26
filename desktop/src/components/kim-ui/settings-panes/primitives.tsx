import type { ReactNode } from 'react';

// Shared layout primitives for the settings panes.
// Extracted from RevampSettings.tsx (file-split restructure).

function PaneHeader({ title, subtitle, onClose }: { title: string; subtitle?: string; onClose?: () => void }) {
  return (
    <div className="kim-settings-pane__header">
      <div>
        <h2 className="kim-settings-pane__title">{title}</h2>
        {subtitle && <p className="kim-settings-pane__subtitle">{subtitle}</p>}
      </div>
      {onClose && (
        <button type="button" className="kr-icon-btn kim-settings-pane__close" onClick={onClose} aria-label="Close">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return <div className="kr-eyebrow kim-settings-pane__section-label">{children}</div>;
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
      className={`kim-settings-row${danger ? ' kim-settings-row--danger' : ''}`}
    >
      <div className="kim-settings-row__copy">
        <div className="kim-settings-row__title">{title}</div>
        {subtitle && <div className="kim-settings-row__subtitle">{subtitle}</div>}
      </div>
      <div className="kim-settings-row__control">{children}</div>
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
