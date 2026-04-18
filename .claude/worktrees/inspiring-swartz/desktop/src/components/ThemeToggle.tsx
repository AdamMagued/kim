import type { Theme } from '../types';

interface Props {
  theme: Theme;
  onChange: (theme: Theme) => void;
}

const OPTIONS: { value: Theme; label: string; icon: string }[] = [
  { value: 'light', label: 'Light', icon: '☀️' },
  { value: 'system', label: 'System', icon: '💻' },
  { value: 'dark', label: 'Dark', icon: '🌙' },
];

export function ThemeToggle({ theme, onChange }: Props) {
  return (
    <div
      style={{
        display: 'flex',
        gap: '2px',
        background: 'var(--bg-card)',
        border: '1px solid var(--border)',
        borderRadius: '8px',
        padding: '2px',
      }}
    >
      {OPTIONS.map(opt => (
        <button
          key={opt.value}
          title={opt.label}
          onClick={() => onChange(opt.value)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            padding: '4px 10px',
            borderRadius: '6px',
            border: 'none',
            cursor: 'pointer',
            fontSize: '12px',
            fontWeight: theme === opt.value ? 600 : 400,
            background: theme === opt.value ? 'var(--accent)' : 'transparent',
            color: theme === opt.value ? '#fff' : 'var(--text-muted)',
            transition: 'all 0.15s ease',
          }}
        >
          <span>{opt.icon}</span>
          <span>{opt.label}</span>
        </button>
      ))}
    </div>
  );
}
