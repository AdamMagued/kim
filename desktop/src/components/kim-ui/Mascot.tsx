import type { CSSProperties } from 'react';

export type MascotVariant = 'blob' | 'monogram' | 'ring' | 'dots' | 'none';

interface Props {
  size?: number;
  variant?: MascotVariant;
  style?: CSSProperties;
}

export function Mascot({ size = 36, variant = 'none', style }: Props) {
  if (variant === 'none') return null;

  if (variant === 'monogram') {
    return (
      <div
        style={{
          width: size,
          height: size,
          borderRadius: Math.round(size * 0.28),
          background: 'linear-gradient(135deg, var(--kim-accent) 0%, #c98968 100%)',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--kim-on-accent)',
          fontWeight: 700,
          fontSize: Math.round(size * 0.52),
          boxShadow: '0 8px 24px rgba(216,141,98,0.18)',
          ...style,
        }}
      >
        K
      </div>
    );
  }

  if (variant === 'ring') {
    const stroke = Math.max(2, size * 0.05);
    return (
      <svg width={size} height={size} viewBox="0 0 64 64" style={{ display: 'block', ...style }}>
        <defs>
          <radialGradient id="kr-ring" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#ffe6d2" stopOpacity="0.5" />
            <stop offset="100%" stopColor="#e8b89a" stopOpacity="0" />
          </radialGradient>
        </defs>
        <circle cx="32" cy="32" r="28" fill="url(#kr-ring)" />
        <circle cx="32" cy="32" r="22" stroke="var(--kim-accent)" strokeWidth={stroke * 0.6} fill="none" opacity="0.6" />
        <circle cx="32" cy="32" r="13" stroke="var(--kim-accent)" strokeWidth={stroke} fill="none" />
        <circle cx="32" cy="32" r="3" fill="var(--kim-accent)" />
      </svg>
    );
  }

  if (variant === 'dots') {
    return (
      <div
        style={{
          width: size * 1.6,
          height: size,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: size * 0.18,
          ...style,
        }}
      >
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            style={{
              width: size * 0.22,
              height: size * 0.22,
              borderRadius: 999,
              background: 'var(--kim-accent)',
              opacity: 0.35 + i * 0.25,
              boxShadow: i === 2 ? '0 0 18px rgba(216,141,98,0.55)' : 'none',
            }}
          />
        ))}
      </div>
    );
  }

  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: 999,
        background: 'radial-gradient(circle at 35% 30%, #fff4e8, #e8b89a 60%, #c89572 100%)',
        boxShadow: '0 0 18px rgba(232,184,154,0.35), inset -2px -3px 6px rgba(0,0,0,0.15)',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        position: 'relative',
        ...style,
      }}
    />
  );
}
