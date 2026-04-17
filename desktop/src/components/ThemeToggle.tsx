import type { ReactElement } from 'react';
import type { Theme } from '../types';

interface Props {
  theme: Theme;
  onChange: (theme: Theme) => void;
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}
function SystemIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

const OPTIONS: { value: Theme; label: string; icon: ReactElement }[] = [
  { value: 'light', label: 'Light', icon: <SunIcon /> },
  { value: 'system', label: 'Auto', icon: <SystemIcon /> },
  { value: 'dark', label: 'Dark', icon: <MoonIcon /> },
];

export function ThemeToggle({ theme, onChange }: Props) {
  return (
    <div className="kim-theme-toggle" role="group" aria-label="Theme">
      {OPTIONS.map(opt => (
        <button
          key={opt.value}
          title={opt.label}
          onClick={() => onChange(opt.value)}
          className={`kim-theme-toggle__btn${
            theme === opt.value ? ' kim-theme-toggle__btn--active' : ''
          }`}
          aria-pressed={theme === opt.value}
        >
          {opt.icon}
          <span>{opt.label}</span>
        </button>
      ))}
    </div>
  );
}
