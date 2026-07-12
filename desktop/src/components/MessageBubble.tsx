import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { KimMessage, ContentBlock, ToolUseBlock, ToolResultBlock, TypingAnimation } from '../types';
import { ToolUseCard, ToolResultCard, SignalCard } from './ToolCallCard';
import { friendlyError } from './chat/utils';
import { toast } from './Toast';

// ── Inline action buttons (copy / edit) ───────────────────────────────────────

function CopyIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="5" width="9" height="9" rx="1.5" />
      <path d="M3 11V3.5A1.5 1.5 0 0 1 4.5 2H11" />
    </svg>
  );
}
function EditIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11.5 2.5l2 2L5 13H3v-2l8.5-8.5z" />
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8.5l3.5 3.5L13 5" />
    </svg>
  );
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied', 'success', 1400);
  } catch {
    toast('Could not copy', 'error', 2000);
  }
}

// User bubble with copy + (optional) inline edit. When `onEdit` is provided,
// hovering reveals a pencil; clicking it swaps the bubble for a textarea.
function UserBubble({ text, onEdit }: { text: string; onEdit?: (newText: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!editing) return;
    const ta = taRef.current;
    if (!ta) return;
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 240) + 'px';
  }, [editing]);

  function startEdit() {
    setDraft(text);
    setEditing(true);
  }
  function cancelEdit() {
    setEditing(false);
    setDraft(text);
  }
  function confirmEdit() {
    const next = draft.trim();
    if (!next || !onEdit) { cancelEdit(); return; }
    if (next === text) { cancelEdit(); return; }
    setEditing(false);
    onEdit(next);
  }

  if (editing) {
    return (
      <div className="kim-msg-row kim-msg-row--user">
        <div className="kim-bubble kim-bubble--user kim-bubble--user-stream kim-bubble--editing">
          <textarea
            ref={taRef}
            className="kim-bubble__edit-textarea"
            aria-label="Edit message"
            value={draft}
            onChange={e => {
              setDraft(e.target.value);
              const el = e.target;
              el.style.height = 'auto';
              el.style.height = Math.min(el.scrollHeight, 240) + 'px';
            }}
            onKeyDown={e => {
              if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
              else if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); confirmEdit(); }
            }}
          />
          <div className="kim-bubble__edit-actions">
            <button type="button" className="kim-bubble__edit-btn" onClick={cancelEdit}>Cancel</button>
            <button type="button" className="kim-bubble__edit-btn kim-bubble__edit-btn--primary" onClick={confirmEdit}>
              Send
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="kim-msg-row kim-msg-row--user">
      <div className="kim-bubble-wrap kim-bubble-wrap--user">
        <div className="kim-bubble kim-bubble--user kim-bubble--user-stream">{text}</div>
        <div className="kim-bubble-actions kim-bubble-actions--user">
          <button type="button" className="kim-bubble-action" title="Copy" aria-label="Copy" onClick={() => void copyText(text)}>
            <CopyIcon />
          </button>
          {onEdit && (
            <button type="button" className="kim-bubble-action" title="Edit and resend" aria-label="Edit and resend" onClick={startEdit}>
              <EditIcon />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// Wraps an assistant bubble with a hover-revealed Copy button.
function AssistantBubbleActions({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  if (!text.trim()) return null;
  return (
    <div className="kim-bubble-actions kim-bubble-actions--assistant">
      <button
        type="button"
        className="kim-bubble-action"
        title={copied ? 'Copied' : 'Copy'}
        aria-label="Copy"
        onClick={async () => {
          await copyText(text);
          setCopied(true);
          if (timerRef.current !== null) clearTimeout(timerRef.current);
          timerRef.current = setTimeout(() => setCopied(false), 1400);
        }}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
      </button>
    </div>
  );
}

function isToolUse(b: ContentBlock): b is ToolUseBlock {
  return Boolean(b && typeof b === 'object' && b.type === 'tool_use');
}
function isToolResult(b: ContentBlock): b is ToolResultBlock {
  return Boolean(b && typeof b === 'object' && b.type === 'tool_result');
}

function isBridgeFillerText(text: string): boolean {
  return /^Calling\s+[A-Za-z_][\w-]*\.$/.test(text.trim());
}

// ── Minimal markdown renderer ─────────────────────────────────────────────────

/**
 * F2: split text into fenced-code and prose segments by scanning for ``` fence
 * PAIRS across the whole string FIRST. This preserves blank lines inside code
 * blocks (the old code paragraph-split on /\n\n+/ before detecting fences, so a
 * code block with a blank line was torn apart) and handles fences mid-paragraph.
 */
export function splitFences(text: string): { type: 'code' | 'text'; content: string }[] {
  const out: { type: 'code' | 'text'; content: string }[] = [];
  const fence = /```[^\n]*\n?([\s\S]*?)```/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = fence.exec(text)) !== null) {
    if (m.index > last) out.push({ type: 'text', content: text.slice(last, m.index) });
    out.push({ type: 'code', content: m[1].replace(/\n$/, '') });
    last = fence.lastIndex;
  }
  if (last < text.length) out.push({ type: 'text', content: text.slice(last) });
  return out;
}

/** F3: links are only followed for http/https/mailto; anything else (javascript:,
 *  data:, file:, …) renders as plain text. */
export function isSafeLinkUrl(url: string): boolean {
  const u = url.trim().toLowerCase();
  return u.startsWith('http://') || u.startsWith('https://') || u.startsWith('mailto:');
}

/** F3: classify an image src — inline-safe (data:/asset:/same-origin), a remote
 *  https image (click-to-load), or blocked (any other scheme). */
export function classifyImageSrc(src: string): 'inline' | 'remote' | 'blocked' {
  const s = src.trim();
  const u = s.toLowerCase();
  if (u.startsWith('data:') || u.startsWith('asset:') || s.startsWith('/') || s.startsWith('./') || s.startsWith('../')) {
    return 'inline';
  }
  if (u.startsWith('https://')) return 'remote';
  return 'blocked';
}

/** Remote https image rendered behind a click-to-load placeholder so model/tool
 *  output can't silently phone home (exfil/tracking channel). (F3) */
function RemoteImage({ src, alt }: { src: string; alt: string }) {
  const [load, setLoad] = useState(false);
  if (load) return <img src={src} alt={alt} className="kim-bubble__img" />;
  return (
    <button
      type="button"
      className="kim-bubble__img-placeholder"
      onClick={() => setLoad(true)}
      title={src}
    >
      ▶ Load image{alt ? ` — ${alt}` : ''}
    </button>
  );
}

function renderText(text: string) {
  const segments = splitFences(text);
  return (
    <div className="prose">
      {segments.flatMap((seg, si) => {
        if (seg.type === 'code') {
          return [<pre key={`c${si}`}><code>{seg.content}</code></pre>];
        }
        const paragraphs = seg.content.split(/\n\n+/);
        return paragraphs.map((para, i) => {
          if (!para.trim()) return null;
          // Image: ![alt](url) · Link: [text](url)
          const parts = para.split(/(!?\[[^\]]*\]\([^)]+\))/g);
          return (
            <p key={`t${si}-${i}`}>
              {parts.map((part, j) => {
                const imgMatch = part.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
                if (imgMatch) {
                  const src = imgMatch[2].trim();
                  const alt = imgMatch[1] || 'image';
                  const kind = classifyImageSrc(src);
                  if (kind === 'inline') {
                    return <img key={j} src={src} alt={alt} className="kim-bubble__img" />;
                  }
                  if (kind === 'remote') {
                    return <RemoteImage key={j} src={src} alt={alt} />;
                  }
                  return <span key={j}>{part}</span>; // blocked scheme → plain text
                }
                const linkMatch = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
                if (linkMatch) {
                  if (isSafeLinkUrl(linkMatch[2])) {
                    return <a key={j} href={linkMatch[2]} target="_blank" rel="noopener noreferrer" className="kim-bubble__link">{linkMatch[1]}</a>;
                  }
                  return <span key={j}>{linkMatch[1]}</span>; // unsafe scheme → plain text
                }

                const lines = part.split('\n');
                return (
                  <span key={j}>
                    {lines.map((line, k) => (
                      <span key={k}>{renderInlineMarkdown(line, `${j}-${k}`)}{k < lines.length - 1 && <br />}</span>
                    ))}
                  </span>
                );
              })}
            </p>
          );
        });
      })}
    </div>
  );
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`)/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      nodes.push(text.slice(last, match.index));
    }

    const key = `${keyPrefix}-${match.index}`;
    if (match[2]) {
      nodes.push(<strong key={key}>{match[2]}</strong>);
    } else if (match[3]) {
      nodes.push(<em key={key}>{match[3]}</em>);
    } else if (match[4]) {
      nodes.push(<code key={key}>{match[4]}</code>);
    }
    last = match.index + match[0].length;
  }

  if (last < text.length) {
    nodes.push(text.slice(last));
  }
  return nodes.length ? nodes : [text];
}

// ── Typing animation engines ──────────────────────────────────────────────────

/**
 * AnimatedText — reveals plain text using one of three animations:
 *   typewriter  (01): chars appear one-by-one, variable timing
 *   word-fade   (02): each word fades + slides up with blur
 *   char-blur   (04): chars blur in in small bursts
 *
 * When animation === 'none' OR the text is already shown (reopen), render inline.
 */
export function AnimatedText({
  text,
  animation,
  active,
}: {
  text: string;
  animation: TypingAnimation;
  /** If false, display immediately (no animation). */
  active: boolean;
}) {
  const containerRef = useRef<HTMLSpanElement>(null);
  const [done, setDone] = useState(!active || animation === 'none');
  const renderedText = useMemo(() => renderText(text), [text]);

  useEffect(() => {
    if (!active || animation === 'none') return;
    const el = containerRef.current;
    if (!el) return;

    let cancelled = false;
    const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));
    const raf   = () => new Promise<void>(r => requestAnimationFrame(() => r()));

    async function run() {
      if (!el) return;
      el.innerHTML = '';

      if (animation === 'typewriter') {
        // 01 — typewriter: chars one by one, variable speed
        const span = document.createElement('span');
        const cur  = document.createElement('span');
        cur.className = 'kim-anim-caret';
        el.appendChild(span);
        el.appendChild(cur);
        for (let i = 0; i <= text.length; i++) {
          if (cancelled) return;
          span.textContent = text.slice(0, i);
          const ch = text[i - 1] || '';
          await sleep(/[,.]/.test(ch) ? 160 : 14 + Math.random() * 18);
        }
        cur.remove();

      } else if (animation === 'word-fade') {
        // 02 — word fade: each word fades+blur+slides in
        const wrap = document.createElement('span');
        wrap.className = 'kim-anim-word-wrap';
        el.appendChild(wrap);
        const words = text.split(' ');
        for (const w of words) {
          if (cancelled) return;
          const sp = document.createElement('span');
          sp.className = 'kim-anim-word';
          sp.textContent = w + ' ';
          wrap.appendChild(sp);
          await raf(); await raf();
          sp.classList.add('kim-anim-word--show');
          await sleep(45 + Math.random() * 30);
        }

      } else if (animation === 'char-blur') {
        // 04 — char blur: chars blur in in small random bursts
        const chars: HTMLElement[] = [];
        for (const ch of text) {
          if (ch === ' ') {
            el.appendChild(document.createTextNode(' '));
          } else {
            const sp = document.createElement('span');
            sp.className = 'kim-anim-char';
            sp.textContent = ch;
            el.appendChild(sp);
            chars.push(sp);
          }
        }
        let idx = 0;
        while (idx < chars.length) {
          if (cancelled) return;
          const burst = Math.floor(2 + Math.random() * 3);
          for (let b = 0; b < burst && idx < chars.length; b++, idx++) {
            chars[idx].classList.add('kim-anim-char--show');
          }
          await sleep(18);
        }
      }

      if (!cancelled) setDone(true);
    }

    void run();
    return () => { cancelled = true; };
  }, [text, animation, active]);

  if (done || !active || animation === 'none') {
    return <>{renderedText}</>;
  }

  return <span ref={containerRef} className="kim-anim-root" />;
}

// ── Main component ────────────────────────────────────────────────────────────

interface Props {
  message: KimMessage;
  /** Whether this specific message should animate in (only newest). */
  animate?: boolean;
  typingAnimation?: TypingAnimation;
  onRetry?: () => void;
  retries?: number;
  /** When provided, the user bubble shows an Edit affordance; calling this
   *  cancels the running task (if any) and resends the edited text. */
  onEdit?: (newText: string) => void;
}

export const MessageBubble = React.memo(function MessageBubble({ message, animate = false, typingAnimation = 'none', onRetry, retries = 0, onEdit }: Props) {
  const isUser   = message.role === 'user';
  const isSystem = message.role === 'system';

  let fullText = '';
  if (typeof message.content === 'string') {
    fullText = message.content;
  } else if (Array.isArray(message.content)) {
    fullText = message.content
      .filter(b => b.type === 'text')
      .map(b => (b as { type: 'text'; text: string }).text)
      .join('\n');
  }

  const stripped = fullText.replace(/^(?:\[truncated.*?\]\n)?(?:\[err\]\s*)?(?:\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]?\d*\s+)?/, '').trim();
  
  // Hide internal orchestrator prompts
  if (stripped.startsWith('[Tool result:') || stripped === 'Current screen. What is your next action?' || isBridgeFillerText(stripped)) {
    return null;
  }

  let taskCompleteText = '';
  const taskCompleteMatch = stripped.match(/^TASK_COMPLETE:\s*(.+)$/is);
  if (taskCompleteMatch) {
    taskCompleteText = taskCompleteMatch[1].trim();
  }

  const needHelpMatch = stripped.match(/^NEED_HELP:\s*(.+)$/is);
  if (needHelpMatch) {
    const text = friendlyError(needHelpMatch[1].trim() || 'Kim needs your help to continue.');
    return (
      <div className={`kim-msg-row kim-msg-row--${isUser ? 'user' : isSystem ? 'system' : 'assistant'}`}>
        <div style={{ maxWidth: '78%', minWidth: 0 }}>
          <SignalCard kind="error" text={text} onAction={onRetry} actionLabel="Resend Task" />
        </div>
      </div>
    );
  }


  if (isSystem) {
    return (
      <div className="kim-msg-row kim-msg-row--system">
        <span className="kim-system-note">
          {typeof message.content === 'string' ? message.content : 'System message'}
        </span>
      </div>
    );
  }

  if (isUser) {
    let text =
      typeof message.content === 'string'
        ? message.content
        : message.content
            .filter(b => b.type === 'text')
            .map(b => (b as { type: 'text'; text: string }).text)
            .join('\n');

    if (text.startsWith('Task: ')) {
      text = text.substring(6).trim();
    }

    return <UserBubble text={text} onEdit={onEdit} />;
  }

  if (message.role === 'tool') {
    if (Array.isArray(message.content)) {
      const resultBlocks = message.content.filter(isToolResult);
      if (resultBlocks.length === 0) return null;
      return (
        <div className="kim-msg-row kim-msg-row--assistant">
          <div className="kim-bubble-wrap kim-bubble-wrap--assistant">
            <div className="kim-bubble-wrap__inner">
              {resultBlocks.map((b) => <ToolResultCard key={b.tool_use_id} block={b} />)}
            </div>
          </div>
        </div>
      );
    }
    const text = message.content;
    if (!text.trim()) return null;
    return (
      <div className="kim-tool-result-row">
        <div className="kim-tool-result-inline">
          <span className="kim-tool-result-inline__name">{message.name ?? 'tool'}:</span>{' '}{text}
        </div>
      </div>
    );
  }

  // ── Assistant message ──────────────────────────────────────────────────────
  const content = message.content;

  if (typeof content === 'string') {
    let rawToolCall = null;
    // L7: anchor to the START only — the old global regex stripped these
    // phrases ANYWHERE in the text, mangling legitimate content that quotes
    // them (the utils layer explicitly warns never to brand-scrub raw answers).
    const cleanContent = content.replace(/^\s*(?:(?:Gemini said|Claude said|Assistant said|ChatGPT said|Grok said):?\s*)+/i, '').trim();
    
    if (cleanContent.startsWith('{')) {
      try {
        const parsed = JSON.parse(cleanContent);
        if (parsed) {
          if ((parsed.type === 'tool_call' || parsed.type === 'tool_use') && (parsed.tool || parsed.name)) {
            rawToolCall = parsed;
          } else if (parsed.tool || parsed.name) {
            rawToolCall = parsed;
          } else if (parsed.tool_calls && Array.isArray(parsed.tool_calls) && parsed.tool_calls.length > 0) {
            rawToolCall = parsed.tool_calls[0];
          }
        }
      } catch {
        // Not a JSON tool call
      }
    }

    if (rawToolCall) {
      return (
        <div className="kim-msg-row kim-msg-row--assistant">
          <div style={{ maxWidth: '78%', minWidth: 0 }}>
            <ToolUseCard
              block={{
                type: 'tool_use',
                id: rawToolCall.id || `tc-${Date.now()}`,
                name: rawToolCall.tool || rawToolCall.name || 'unknown',
                input: rawToolCall.args || rawToolCall.input || {},
              }}
            />
            {retries > 0 && (
              <div style={{ fontSize: 11, color: 'var(--kim-text-muted)', marginTop: 4, paddingLeft: 12 }}>
                (retried {retries}x)
              </div>
            )}
          </div>
        </div>
      );
    }

    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-bubble-wrap kim-bubble-wrap--assistant">
          <div className="kim-bubble kim-bubble--assistant">
            <AnimatedText text={taskCompleteText || content} animation={typingAnimation} active={animate} />
            {message.tool_calls && message.tool_calls.length > 0 && (
              <div style={{ marginTop: 10 }}>
                {message.tool_calls.map(tc => (
                  <ToolUseCard
                    key={tc.id}
                    block={{
                      type: 'tool_use',
                      id: tc.id,
                      name: tc.function.name,
                      input: (() => {
                        try { return JSON.parse(tc.function.arguments) as Record<string, unknown>; }
                        catch { return { raw: tc.function.arguments }; }
                      })(),
                    }}
                  />
                ))}
              </div>
            )}
          </div>
          <AssistantBubbleActions text={taskCompleteText || content} />
        </div>
      </div>
    );
  }

  if (!Array.isArray(content)) {
    const fallbackText = typeof content === 'object' && content !== null
      ? JSON.stringify(content, null, 2)
      : String(content ?? '');
    if (!fallbackText.trim() || isBridgeFillerText(fallbackText)) return null;
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-bubble-wrap kim-bubble-wrap--assistant">
          <div className="kim-bubble kim-bubble--assistant">
            <AnimatedText text={fallbackText} animation={typingAnimation} active={animate} />
          </div>
          <AssistantBubbleActions text={fallbackText} />
        </div>
      </div>
    );
  }

  // Retain the original content-array index as a stable key so that the
  // bridge-filler filter removing an item doesn't shift indices and attach
  // AnimatedText animation state to the wrong block.
  const textBlocks = (
    content
      .map((b, origIdx) => ({ b, origIdx }))
      .filter(({ b }) => b && typeof b === 'object' && b.type === 'text') as Array<{ b: { type: 'text'; text: string }; origIdx: number }>
  ).filter(({ b }) => !isBridgeFillerText(b.text));
  const toolUseBlocks = content.filter(isToolUse);
  const toolResultBlocks = content.filter(isToolResult);
  const hasText  = textBlocks.length > 0;
  const hasTools = toolUseBlocks.length > 0 || toolResultBlocks.length > 0;

  if (!hasText && !hasTools) return null;

  const fullAssistantText = textBlocks.map(({ b }) => b.text.replace(/^TASK_COMPLETE:\s*/i, '')).join('\n\n');

  return (
    <div className="kim-msg-row kim-msg-row--assistant">
      <div className="kim-bubble-wrap kim-bubble-wrap--assistant">
        <div className="kim-bubble-wrap__inner">
          {hasText && (
            <div className={`kim-bubble kim-bubble--assistant${hasTools ? ' kim-bubble--assistant-group-top' : ''}`}>
              {textBlocks.map(({ b, origIdx }, i) => (
                <div key={origIdx}>
                  <AnimatedText
                    text={b.text.replace(/^TASK_COMPLETE:\s*/i, '')}
                    animation={typingAnimation}
                    active={animate && i === textBlocks.length - 1}
                  />
                </div>
              ))}
            </div>
          )}
          {toolUseBlocks.map(b => <ToolUseCard key={b.id} block={b} />)}
          {toolResultBlocks.map((b) => <ToolResultCard key={b.tool_use_id} block={b} />)}
        </div>
        {hasText && <AssistantBubbleActions text={fullAssistantText} />}
      </div>
    </div>
  );
});
