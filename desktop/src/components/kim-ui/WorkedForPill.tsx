import { useState, useMemo, type ReactNode } from "react";

export type WorkedForToolKind =
  | "read"
  | "edit"
  | "write"
  | "run"
  | "screenshot"
  | "search"
  | "fetch"
  | "ls"
  | "grep";

export type WorkedForTraceItem =
  | {
      kind: "think";
      text: string;
      emphasis?: string[];
    }
  | {
      kind: WorkedForToolKind;
      target: string;
      detail?: string;
    };

export interface WorkedForPillProps {
  trace: WorkedForTraceItem[];
  duration: string;
  defaultOpen?: boolean;
  label?: string;
  sessionLabel?: string;
  productTag?: string;
  className?: string;
}

const tokens = {
  "--wf-bg":          "var(--kim-bg)",
  "--wf-bg-2":        "var(--kim-bg-2)",
  "--wf-surface":     "var(--kim-surface)",
  "--wf-surface-2":   "var(--kim-surface-2)",
  "--wf-border":      "var(--kim-border)",
  "--wf-text":        "var(--kim-text)",
  "--wf-text-2":      "var(--kim-text-2)",
  "--wf-text-3":      "var(--kim-text-3)",
  "--wf-text-4":      "var(--kim-text-4)",
  "--wf-accent":      "var(--kim-accent)",
  "--wf-accent-soft": "var(--kim-accent-soft)",
  "--wf-accent-line": "var(--kim-accent-line)",
  "--wf-mono":        '"JetBrains Mono", "SF Mono", ui-monospace, monospace',
  "--wf-sans":        'Inter, -apple-system, BlinkMacSystemFont, system-ui, sans-serif',
} as React.CSSProperties;

type IconName =
  | WorkedForToolKind
  | "think"
  | "chevron-down"
  | "sparkle"
  | "clock"
  | "tool"
  | "close";

const Icon = ({ name, size = 13 }: { name: IconName; size?: number }) => {
  const s = { width: size, height: size, display: "block" as const };
  switch (name) {
    case "read":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M3 2.5h6l3 3v8a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-10A.5.5 0 0 1 3 2.5Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
          <path d="M9 2.5v3h3" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        </svg>
      );
    case "edit":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M2.5 13.5 4 10l6.5-6.5 2 2L6 12l-3.5 1.5Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        </svg>
      );
    case "write":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <rect x="2.5" y="2.5" width="9" height="11" rx="1" stroke="currentColor" strokeWidth="1.3" />
          <path d="M5 6h4M5 8.5h4M5 11h2.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          <path d="M11 11.5l2 2 1.5-1.5-2-2-1.5 1.5Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" fill="var(--wf-bg)" />
        </svg>
      );
    case "run":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <rect x="1.5" y="3" width="13" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M4 6.5 6 8l-2 1.5M7.5 10h3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "screenshot":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <rect x="1.5" y="4" width="13" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="8" cy="8.5" r="2.2" stroke="currentColor" strokeWidth="1.3" />
          <path d="M6 4l1-1.5h2L10 4" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        </svg>
      );
    case "search":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="m10.5 10.5 3 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      );
    case "fetch":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M2.5 8h11M8 2.5a8 8 0 0 1 0 11M8 2.5a8 8 0 0 0 0 11" stroke="currentColor" strokeWidth="1.3" />
        </svg>
      );
    case "ls":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M2 4.5h12M2 8h12M2 11.5h7" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      );
    case "grep":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <circle cx="7" cy="7" r="4" stroke="currentColor" strokeWidth="1.3" />
          <path d="m10 10 3.5 3.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          <path d="M5.5 7h3M7 5.5v3" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
        </svg>
      );
    case "think":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M8 2.5a4.5 4.5 0 0 1 2.6 8.2v1.3a.5.5 0 0 1-.5.5H5.9a.5.5 0 0 1-.5-.5v-1.3A4.5 4.5 0 0 1 8 2.5Z" stroke="currentColor" strokeWidth="1.3" />
          <path d="M6.5 14h3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      );
    case "chevron-down":
      return (
        <svg style={s} viewBox="0 0 12 12" fill="none">
          <path d="m3 4.5 3 3 3-3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "sparkle":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M8 2v3M8 11v3M2 8h3M11 8h3M4.5 4.5l2 2M9.5 9.5l2 2M4.5 11.5l2-2M9.5 6.5l2-2" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
        </svg>
      );
    case "clock":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="1.3" />
          <path d="M8 5v3l2 1.5" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
        </svg>
      );
    case "tool":
      return (
        <svg style={s} viewBox="0 0 16 16" fill="none">
          <path d="M10.5 2.5a3 3 0 0 1 1 4.8L13 9l-3.7 3.7-1.7-1.7L10.4 8 7.3 4.5a3 3 0 0 1 3.2-2Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
        </svg>
      );
    case "close":
      return (
        <svg style={s} viewBox="0 0 10 10" fill="none">
          <path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      );
  }
};

const TOOL_META: Record<WorkedForToolKind, { verb: string; color: string }> = {
  read:       { verb: "Read",       color: "#a9c8e8" },
  edit:       { verb: "Edited",     color: "#e8b89a" },
  write:      { verb: "Wrote",      color: "#e8b89a" },
  run:        { verb: "Ran",        color: "#d8b8e8" },
  screenshot: { verb: "Screenshot", color: "#c9c4eb" },
  search:     { verb: "Searched",   color: "#9ad3c3" },
  fetch:      { verb: "Fetched",    color: "#9ad3c3" },
  ls:         { verb: "Listed",     color: "#b8b6b0" },
  grep:       { verb: "Searched",   color: "#b6dab1" },
};

const Row = ({ item, i }: { item: WorkedForTraceItem; i: number }) => {
  const baseStyle: React.CSSProperties = {
    animationDelay: `${i * 30}ms`,
    position: "relative",
    display: "flex",
    gap: 12,
  };

  if (item.kind === "think") {
    let content: ReactNode = item.text;
    if (item.emphasis && item.emphasis.length) {
      const parts: ReactNode[] = [];
      let cursor = 0;
      item.emphasis.forEach((needle, ei) => {
        const idx = item.text.indexOf(needle, cursor);
        if (idx >= 0) {
          if (idx > cursor) parts.push(<span key={`t${ei}`}>{item.text.slice(cursor, idx)}</span>);
          parts.push(<em key={`e${ei}`} className="wf-em">{needle}</em>);
          cursor = idx + needle.length;
        }
      });
      if (cursor < item.text.length) parts.push(<span key="tail">{item.text.slice(cursor)}</span>);
      content = parts;
    }
    return (
      <div className="wf-row" style={{ ...baseStyle, padding: "6px 16px 6px 0" }}>
        <span className="wf-dot wf-dot--thought" title="thought">
          <Icon name="think" size={11} />
        </span>
        <div className="wf-thought" style={{ paddingTop: 3 }}>{content}</div>
      </div>
    );
  }

  const meta = TOOL_META[item.kind];
  const isCommand = item.kind === "run";
  const isUrl = item.kind === "fetch";

  return (
    <div className="wf-row" style={{ ...baseStyle, alignItems: "center", padding: "5px 16px 5px 0" }}>
      <span className="wf-dot wf-dot--tool" style={{ color: meta.color }}>
        <Icon name={item.kind} size={12} />
      </span>
      <span className="wf-verb">{meta.verb}</span>
      <span className="wf-code wf-code--target">
        {isCommand && <span style={{ color: "var(--wf-text-3)" }}>$</span>}
        <span className="wf-target-text">
          {isUrl ? item.target.replace(/^https?:\/\//, "") : item.target}
        </span>
      </span>
      {item.detail && <span className="wf-row-meta">{item.detail}</span>}
    </div>
  );
};

export function WorkedForPill({
  trace,
  duration,
  defaultOpen = false,
  label = "Worked for",
  sessionLabel,
  productTag,
  className,
}: WorkedForPillProps) {
  const [open, setOpen] = useState(defaultOpen);

  const stats = useMemo(() => {
    const thoughts = trace.filter(t => t.kind === "think").length;
    return { thoughts, tools: trace.length - thoughts };
  }, [trace]);

  return (
    <div
      className={"wf-root" + (className ? " " + className : "")}
      style={{ ...tokens, fontFamily: "var(--wf-sans)", maxWidth: 720 }}
    >
      <style>{CSS}</style>

      <button
        className="wf-pill"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        <span style={{ color: "var(--wf-text)" }}>{label}</span>
        <span className="wf-pill__duration">{duration}</span>
        <span className="wf-pill__sep">·</span>
        <span className="wf-pill__actions">{stats.tools} actions</span>
        <span className="wf-chev"><Icon name="chevron-down" size={11} /></span>
      </button>

      <div className={"wf-panel " + (open ? "wf-panel--open" : "wf-panel--closed")}>
        <div className="wf-inner">
          <div className="wf-card">
            <div className="wf-header">
              <div className="wf-header__titles">
                <span className="wf-header__title">Reasoning trace</span>
                {sessionLabel && <span className="wf-header__subtitle">{sessionLabel}</span>}
              </div>
              <div className="wf-header__right">
                <span className="wf-code">
                  <Icon name="clock" size={10} />
                  {duration}
                </span>
                <button
                  className="wf-icon-btn"
                  onClick={() => setOpen(false)}
                  title="Collapse"
                  aria-label="Collapse"
                >
                  <Icon name="close" size={10} />
                </button>
              </div>
            </div>

            <div className="wf-body stream">
              <div style={{ position: "relative" }}>
                <span className="wf-rail" />
                {trace.map((item, i) => (
                  <Row key={i} item={item} i={i} />
                ))}
              </div>
            </div>

            <div className="wf-footer">
              <span className="wf-stat">
                <Icon name="think" size={11} />
                {stats.thoughts} thoughts
              </span>
              <span className="wf-dot-sep" />
              <span className="wf-stat">
                <Icon name="tool" size={11} />
                {stats.tools} tool calls
              </span>
              <span className="wf-dot-sep" />
              <span className="wf-stat">
                <Icon name="clock" size={11} />
                {duration}
              </span>
              {productTag && <span className="wf-footer__tag">{productTag}</span>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const CSS = `
.wf-pill {
  background: var(--wf-surface);
  border: 1px solid var(--wf-border);
  color: var(--wf-text-2);
  padding: 7px 12px 7px 10px;
  border-radius: 999px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  font: inherit;
  font-size: 13px;
  transition: border-color .18s ease, background .18s ease, transform .18s ease;
}
.wf-pill:hover { border-color: var(--wf-accent-line); background: var(--wf-surface-2); }
.wf-pill:active { transform: translateY(0.5px); }
.wf-pill__duration { font-family: var(--wf-mono); font-size: 12.5px; color: var(--wf-accent); }
.wf-pill__sep      { color: var(--wf-text-4); font-family: var(--wf-mono); font-size: 11px; }
.wf-pill__actions  { font-family: var(--wf-mono); font-size: 11.5px; color: var(--wf-text-3); }

.wf-chev { color: var(--wf-text-3); margin-left: 2px; display: inline-flex; transition: transform .25s cubic-bezier(.2,.7,.2,1); }
.wf-pill[aria-expanded="true"] .wf-chev { transform: rotate(180deg); }

.wf-panel {
  overflow: hidden;
  display: grid;
  transition: grid-template-rows .35s cubic-bezier(.2,.7,.2,1), opacity .25s ease, margin-top .35s ease;
}
.wf-panel .wf-inner { min-height: 0; overflow: hidden; }
.wf-panel--closed { grid-template-rows: 0fr; opacity: 0; margin-top: 0; }
.wf-panel--open   { grid-template-rows: 1fr; opacity: 1; margin-top: 10px; }

.wf-card {
  background: var(--wf-bg-2);
  border: 1px solid var(--wf-border);
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 30px 80px -40px rgba(0,0,0,0.6);
}

.wf-header {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px dashed var(--wf-border);
}
.wf-header__icon {
  width: 22px; height: 22px; border-radius: 6px;
  background: var(--wf-accent-soft); color: var(--wf-accent);
  display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.wf-header__titles { display: flex; flex-direction: column; line-height: 1.2; }
.wf-header__title    { font-size: 13px; color: var(--wf-text); font-weight: 500; }
.wf-header__subtitle { font-size: 11px; color: var(--wf-text-3); font-family: var(--wf-mono); }
.wf-header__right    { margin-left: auto; display: flex; align-items: center; gap: 8px; }

.wf-icon-btn {
  width: 24px; height: 24px; border-radius: 6px;
  background: transparent; border: 1px solid transparent;
  color: var(--wf-text-3); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.wf-icon-btn:hover {
  background: var(--wf-surface);
  border-color: var(--wf-border);
  color: var(--wf-text);
}

.wf-body {
  padding: 14px 16px;
  position: relative;
  max-height: 460px;
  overflow: auto;
}
.wf-body::-webkit-scrollbar { width: 6px; }
.wf-body::-webkit-scrollbar-thumb { background: var(--wf-border); border-radius: 3px; }
.wf-body::-webkit-scrollbar-track { background: transparent; }

.wf-rail {
  position: absolute;
  left: 11.5px;
  top: 0;
  bottom: 0;
  width: 1px;
  background: linear-gradient(180deg, transparent, var(--wf-border) 16px, var(--wf-border) calc(100% - 16px), transparent);
}

@keyframes wf-row-in {
  from { opacity: 0; transform: translateY(2px); }
  to   { opacity: 1; transform: translateY(0); }
}
.wf-row { animation: wf-row-in .32s ease both; }

.wf-dot {
  width: 24px; height: 24px;
  border-radius: 999px;
  background: var(--wf-bg);
  border: 1px solid var(--wf-border);
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--wf-text-2);
  flex-shrink: 0;
  position: relative;
  z-index: 1;
}
.wf-dot--tool { background: var(--wf-surface); }

.wf-thought {
  color: var(--wf-text-2);
  font-size: 14px;
  line-height: 1.6;
  font-family: var(--wf-sans);
  text-wrap: pretty;
}
.wf-em { color: var(--wf-text); font-style: normal; font-weight: 500; }

.wf-verb {
  font-family: var(--wf-sans);
  font-size: 13.5px;
  color: var(--wf-text-2);
  flex-shrink: 0;
}

.wf-code {
  font-family: var(--wf-mono);
  font-size: 12.5px;
  background: var(--wf-bg-2);
  border: 1px solid var(--wf-border);
  border-radius: 6px;
  padding: 1.5px 7px;
  color: var(--wf-text);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  line-height: 1.5;
}
.wf-code--target {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-target-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.wf-row-meta {
  margin-left: auto;
  font-family: var(--wf-mono);
  font-size: 10.5px;
  color: var(--wf-text-4);
  flex-shrink: 0;
  padding-left: 12px;
}

.wf-footer {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px 12px;
  border-top: 1px dashed var(--wf-border);
  font-family: var(--wf-mono);
  font-size: 11px;
  color: var(--wf-text-3);
}
.wf-stat { display: inline-flex; align-items: center; gap: 6px; }
.wf-stat svg { opacity: .75; }
.wf-dot-sep { width: 3px; height: 3px; border-radius: 999px; background: var(--wf-text-4); }
.wf-footer__tag { margin-left: auto; color: var(--wf-text-4); }
`;

export default WorkedForPill;
