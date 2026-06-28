import { useState } from 'react';

export type ConnectorState = 'connected' | 'available' | 'soon';

export interface ConnectorActivity {
  text: string;
  time: string;
}

export interface Connector {
  id: string;
  name: string;
  brand: string;
  initials: string;
  category: string;
  desc: string;
  tools?: number;
  scopes?: string[];
  lastUsed?: string;
  state: ConnectorState;
  activity?: ConnectorActivity[];
}

interface Props {
  connectors: Connector[];
  onClose?: () => void;
  onConnect?: (c: Connector) => void;
  onManage?: (c: Connector) => void;
}

const DEFAULT_CATEGORIES = ['All', 'Productivity', 'Dev', 'School'];

export function ConnectorsPanel({ connectors, onClose, onConnect, onManage }: Props) {
  const [filter, setFilter] = useState('All');
  const [search, setSearch] = useState('');
  const cats = Array.from(new Set([...DEFAULT_CATEGORIES, ...connectors.map((c) => c.category)]));
  const q = search.trim().toLowerCase();
  const filtered = connectors.filter((c) => {
    if (filter !== 'All' && c.category !== filter) return false;
    if (q && !c.name.toLowerCase().includes(q) && !c.desc.toLowerCase().includes(q)) return false;
    return true;
  });
  const connected = filtered.filter((c) => c.state === 'connected');
  const available = filtered.filter((c) => c.state === 'available');
  const soon = filtered.filter((c) => c.state === 'soon');

  return (
    <div
      style={{
        width: 460,
        height: '100%',
        background: 'var(--kim-sidebar-gradient)',
        borderLeft: '1px solid var(--kim-border)',
        display: 'flex',
        flexDirection: 'column',
        boxShadow: '-20px 0 60px rgba(0,0,0,0.4)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 14,
          padding: '18px 20px 14px',
          borderBottom: '1px solid var(--kim-border)',
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 17, fontWeight: 500, letterSpacing: '-0.01em' }}>Connectors</div>
          <div style={{ fontSize: 12, color: 'var(--kim-text-3)', marginTop: 3, lineHeight: 1.5 }}>
            Tool packs Kim can call on your behalf.
          </div>
        </div>
        <button type="button" className="kr-icon-btn" onClick={onClose} aria-label="Close connectors">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <div style={{ display: 'flex', padding: '12px 20px', gap: 12, borderBottom: '1px solid var(--kim-border)' }}>
        {[
          { n: connected.length, label: 'connected', color: 'var(--kim-green)' },
          { n: available.length, label: 'available', color: 'var(--kim-accent)' },
          { n: soon.length, label: 'coming soon', color: 'var(--kim-text-3)' },
        ].map((s, i) => (
          <div
            key={i}
            style={{
              flex: 1,
              padding: '10px 12px',
              background: 'var(--kim-surface)',
              border: '1px solid var(--kim-border)',
              borderRadius: 10,
            }}
          >
            <div style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 18, color: s.color, lineHeight: 1 }}>
              {String(s.n).padStart(2, '0')}
            </div>
            <div
              style={{
                fontSize: 10.5,
                color: 'var(--kim-text-3)',
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                letterSpacing: '0.08em',
                marginTop: 4,
                textTransform: 'uppercase',
              }}
            >
              {s.label}
            </div>
          </div>
        ))}
      </div>

      <div style={{ padding: '12px 20px 0' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            padding: '9px 12px',
            marginBottom: 10,
            background: 'var(--kim-bg-2)',
            border: '1px solid var(--kim-border)',
            borderRadius: 10,
          }}
        >
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none" style={{ color: 'var(--kim-text-3)' }}>
            <circle cx="6" cy="6" r="4" stroke="currentColor" strokeWidth="1.3" />
            <path d="M9 9l3 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          </svg>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search connectors…"
            style={{
              flex: 1,
              background: 'transparent',
              border: 0,
              outline: 'none',
              color: 'var(--kim-text)',
              fontSize: 13.5,
              fontFamily: 'inherit',
            }}
          />
        </div>
        <div style={{ display: 'flex', gap: 6, overflowX: 'auto', paddingBottom: 4 }}>
          {cats.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setFilter(c)}
              style={{
                flexShrink: 0,
                background: filter === c ? 'var(--kim-accent-soft)' : 'transparent',
                border: '1px solid',
                borderColor: filter === c ? 'var(--kim-accent-line)' : 'var(--kim-border)',
                color: filter === c ? 'var(--kim-accent)' : 'var(--kim-text-2)',
                padding: '5px 11px',
                borderRadius: 999,
                fontSize: 12,
                cursor: 'pointer',
              }}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      <div className="kr-stream" style={{ flex: 1, overflow: 'auto', padding: '10px 16px 20px' }}>
        {connected.length > 0 && (
          <>
            <div className="kr-eyebrow" style={{ padding: '8px 4px 8px' }}>
              connected
            </div>
            {connected.map((c) => (
              <ConnectorRow key={c.id} c={c} featured onConnect={onConnect} onManage={onManage} />
            ))}
          </>
        )}
        {available.length > 0 && (
          <>
            <div className="kr-eyebrow" style={{ padding: '16px 4px 8px' }}>
              available
            </div>
            {available.map((c) => (
              <ConnectorRow key={c.id} c={c} onConnect={onConnect} onManage={onManage} />
            ))}
          </>
        )}
        {soon.length > 0 && (
          <>
            <div style={{ display: 'flex', alignItems: 'center', padding: '16px 4px 8px' }}>
              <span className="kr-eyebrow">coming soon</span>
              <span
                style={{
                  marginLeft: 'auto',
                  fontSize: 11,
                  color: 'var(--kim-text-3)',
                  cursor: 'pointer',
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                }}
              >
                request →
              </span>
            </div>
            {soon.map((c) => (
              <ConnectorRow key={c.id} c={c} onConnect={onConnect} onManage={onManage} />
            ))}
          </>
        )}
      </div>

      <div
        style={{
          padding: '12px 20px',
          borderTop: '1px solid var(--kim-border)',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          background: 'var(--kim-bg-2)',
        }}
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style={{ color: 'var(--kim-accent)' }}>
          <path
            d="M4 3l-3 4 3 4M10 3l3 4-3 4M8.5 2L5.5 12"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <div style={{ flex: 1, fontSize: 12.5, color: 'var(--kim-text-2)', lineHeight: 1.45 }}>
          Building your own?
          <span style={{ color: 'var(--kim-text-3)' }}> Wire any HTTP API as a custom MCP server.</span>
        </div>
        <button type="button" className="kr-btn" style={{ fontSize: 12, padding: '6px 10px' }}>
          Docs
        </button>
      </div>
    </div>
  );
}

function ConnectorRow({
  c,
  featured = false,
  onConnect,
  onManage,
}: {
  c: Connector;
  featured?: boolean;
  onConnect?: (c: Connector) => void;
  onManage?: (c: Connector) => void;
}) {
  const tile = (
    <div
      style={{
        width: 38,
        height: 38,
        borderRadius: 10,
        background:
          c.state === 'soon' ? 'var(--kim-bg-2)' : `linear-gradient(135deg, ${c.brand}33, ${c.brand}11)`,
        border: '1px solid',
        borderColor: c.state === 'soon' ? 'var(--kim-border)' : `${c.brand}55`,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: c.state === 'soon' ? 'var(--kim-text-3)' : c.brand,
        fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
        fontSize: 12.5,
        fontWeight: 700,
        letterSpacing: '-0.02em',
        flexShrink: 0,
      }}
    >
      {c.initials}
    </div>
  );

  if (featured && c.state === 'connected') {
    return (
      <div
        style={{
          background: 'var(--kim-surface)',
          border: '1px solid var(--kim-accent-line)',
          borderRadius: 13,
          padding: 14,
          marginBottom: 8,
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        <span
          style={{
            position: 'absolute',
            inset: 0,
            pointerEvents: 'none',
            background: `radial-gradient(circle at 0% 0%, ${c.brand}11, transparent 60%)`,
          }}
        />
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, position: 'relative' }}>
          {tile}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
              <span style={{ fontSize: 14.5, fontWeight: 500 }}>{c.name}</span>
              <span
                style={{
                  width: 5,
                  height: 5,
                  borderRadius: 999,
                  background: 'var(--kim-green)',
                  boxShadow: '0 0 8px var(--kim-green)',
                }}
              />
              {c.lastUsed && (
                <span style={{ fontSize: 11, color: 'var(--kim-text-3)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
                  · {c.lastUsed}
                </span>
              )}
            </div>
            <div style={{ fontSize: 12, color: 'var(--kim-text-3)', lineHeight: 1.5 }}>{c.desc}</div>
          </div>
          <button
            type="button"
            className="kr-btn"
            onClick={() => onManage?.(c)}
            style={{ fontSize: 12, padding: '5px 10px', alignSelf: 'flex-start' }}
          >
            Manage
          </button>
        </div>
        <div style={{ display: 'flex', gap: 6, marginTop: 12, flexWrap: 'wrap', position: 'relative' }}>
          {c.tools != null && (
            <span
              style={{
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                fontSize: 11,
                padding: '3px 8px',
                background: 'var(--kim-bg-2)',
                border: '1px solid var(--kim-border)',
                borderRadius: 5,
                color: 'var(--kim-text-2)',
              }}
            >
              {c.tools} tools
            </span>
          )}
          {(c.scopes ?? []).map((s, i) => (
            <span
              key={i}
              style={{
                fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                fontSize: 11,
                padding: '3px 8px',
                background: 'transparent',
                border: '1px solid var(--kim-border)',
                borderRadius: 5,
                color: 'var(--kim-text-3)',
              }}
            >
              {s}
            </span>
          ))}
        </div>
        {c.activity && c.activity.length > 0 && (
          <div
            style={{
              marginTop: 12,
              paddingTop: 10,
              borderTop: '1px dashed var(--kim-border)',
              display: 'flex',
              flexDirection: 'column',
              gap: 4,
              position: 'relative',
            }}
          >
            <div className="kr-eyebrow" style={{ marginBottom: 4 }}>
              recent activity
            </div>
            {c.activity.map((a, i) => (
              <div
                key={i}
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'baseline',
                  fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace',
                  fontSize: 11.5,
                }}
              >
                <span style={{ color: 'var(--kim-text-4)' }}>›</span>
                <span
                  style={{
                    color: 'var(--kim-text-2)',
                    flex: 1,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {a.text}
                </span>
                <span style={{ color: 'var(--kim-text-4)' }}>{a.time}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      className="kr-row-hover"
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 12,
        padding: '11px 12px',
        borderRadius: 11,
        background: 'transparent',
        border: '1px solid var(--kim-border)',
        marginBottom: 6,
        opacity: c.state === 'soon' ? 0.7 : 1,
        cursor: 'pointer',
      }}
    >
      {tile}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3 }}>
          <span style={{ fontSize: 13.5 }}>{c.name}</span>
          {c.tools != null && c.state !== 'soon' && (
            <span style={{ fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace', fontSize: 10.5, color: 'var(--kim-text-4)' }}>
              {c.tools} tools
            </span>
          )}
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--kim-text-3)', lineHeight: 1.5 }}>{c.desc}</div>
      </div>
      <div style={{ flexShrink: 0, alignSelf: 'center' }}>
        {c.state === 'available' && (
          <button
            type="button"
            className="kr-btn kr-btn-primary"
            onClick={() => onConnect?.(c)}
            style={{ fontSize: 12, padding: '6px 12px' }}
          >
            Connect
          </button>
        )}
        {c.state === 'soon' && (
          <span style={{ fontSize: 11, color: 'var(--kim-text-3)', fontFamily: 'JetBrains Mono, SF Mono, ui-monospace, monospace' }}>
            soon
          </span>
        )}
      </div>
    </div>
  );
}
