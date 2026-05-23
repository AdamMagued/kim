// KimLogo.tsx
// React component for the Kim Ribbon K logo — Soft Pastel Inverted palette
// Drop this into your Tauri/React app, wire up any state you need


interface KimLogoProps {
  /** Size preset: 'sm' | 'md' | 'lg' | 'xl' | number (px) */
  size?: 'sm' | 'md' | 'lg' | 'xl' | number;
  /** Layout: 'horizontal' | 'stacked' | 'icon' */
  layout?: 'horizontal' | 'stacked' | 'icon';
  /** Ribbon fill color (default: soft clay) */
  ribbonColor?: string;
  /** Shadow/fold color (default: warm light) */
  shadowColor?: string;
  /** Shadow opacity 0-1 (default: 0.75) */
  shadowOpacity?: number;
  /** Wordmark text color (inherit if not set) */
  textColor?: string;
  /** Additional classes for the wrapper */
  className?: string;
}

const SIZE_MAP = {
  sm: 80,
  md: 120,
  lg: 180,
  xl: 240,
};

export function KimLogo({
  size = 'md',
  layout = 'horizontal',
  ribbonColor = '#d4a084',
  shadowColor = '#fef9f3',
  shadowOpacity = 0.75,
  textColor,
  className = '',
}: KimLogoProps) {
  const iconSize = typeof size === 'number' ? size : SIZE_MAP[size];
  const wmSize = Math.round(iconSize * 0.53); // ratio from design

  const wrapperClass =
    layout === 'horizontal'
      ? 'flex items-center gap-5'
      : layout === 'stacked'
      ? 'flex flex-col items-center gap-3'
      : 'flex items-center justify-center';

  const showWordmark = layout !== 'icon';

  return (
    <div className={`${wrapperClass} ${className}`}>
      <div
        className="flex items-center justify-center flex-shrink-0"
        style={{ width: iconSize, height: iconSize }}
      >
        <svg
          viewBox="0 0 100 100"
          width="100%"
          height="100%"
          aria-label="Kim logo"
          role="img"
        >
          {/* Ribbon body */}
          <path
            d="M22 14 L36 14 L36 46 L70 14 L86 14 L52 50 L86 86 L70 86 L36 54 L36 86 L22 86 Z"
            fill={ribbonColor}
          />
          {/* Fold/shadow on top arm */}
          <path
            d="M36 46 L70 14 L86 14 L52 50 Z"
            fill={shadowColor}
            opacity={shadowOpacity}
          />
        </svg>
      </div>

      {showWordmark && (
        <span
          className="font-bold tracking-tight leading-none select-none"
          style={{
            fontSize: `${wmSize}px`,
            letterSpacing: '-0.02em',
            fontFamily:
              'Geist, ui-sans-serif, system-ui, -apple-system, sans-serif',
            color: textColor || 'currentColor',
          }}
        >
          Kim
        </span>
      )}
    </div>
  );
}

// ── Usage Examples ───────────────────────────────────────────────────────────

// Horizontal lockup (default)
// <KimLogo size="md" layout="horizontal" />

// Stacked
// <KimLogo size="lg" layout="stacked" />

// Icon-only
// <KimLogo size={64} layout="icon" />

// Custom colors
// <KimLogo
//   ribbonColor="#a8d4c4"  // mint
//   shadowColor="#f3f6fe"  // cool light
//   shadowOpacity={0.7}
//   textColor="#f3f6fe"
// />

// ── Soft Pastel Inverted Palette ─────────────────────────────────────────────
// Use these values for the dark-bg soft inverted look:
//
// Background:  #2b2520  (warm dark)
// Text:        #fef9f3  (warm light)
// Ribbon:      #d4a084  (clay)
// Shadow:      #fef9f3  (warm light)
// Opacity:     0.75
//
// Alternate ribbon colors (all cohesive):
//   Clay:       #d4a084
//   Terracotta: #c88a6e
//   Peach:      #e8b89a
//   Sand:       #d9c0a8
//   Cream:      #f0e0ca
//   Coral:      #e36a4a
//   Sage:       #b5c4a8
//   Mint:       #a8d4c4
//   Sky:        #a8c4d4
//   Lavender:   #c4b5d4
//   Mauve:      #d4b5c4
