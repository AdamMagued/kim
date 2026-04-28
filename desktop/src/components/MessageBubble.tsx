import { useEffect, useRef, useState } from 'react';
import type { KimMessage, ContentBlock, ToolUseBlock, ToolResultBlock, TypingAnimation } from '../types';
import { ToolUseCard, ToolResultCard, SignalCard } from './ToolCallCard';
import { friendlyError } from './ChatView';

function isToolUse(b: ContentBlock): b is ToolUseBlock { return b.type === 'tool_use'; }
function isToolResult(b: ContentBlock): b is ToolResultBlock { return b.type === 'tool_result'; }

// ── Minimal markdown renderer ─────────────────────────────────────────────────

function renderText(text: string) {
  const paragraphs = text.split(/\n\n+/);
  return (
    <div className="prose">
      {paragraphs.map((para, i) => {
        if (para.startsWith('```')) {
          const lines = para.split('\n');
          const code = lines
            .slice(1, lines.lastIndexOf('```') === -1 ? undefined : lines.lastIndexOf('```'))
            .join('\n');
          return <pre key={i}><code>{code}</code></pre>;
        }
        const lines = para.split('\n');
        return (
          <p key={i}>
            {lines.map((line, j) => (
              <span key={j}>{line}{j < lines.length - 1 && <br />}</span>
            ))}
          </p>
        );
      })}
    </div>
  );
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
    return <>{renderText(text)}</>;
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
}

export function MessageBubble({ message, animate = false, typingAnimation = 'none', onRetry, retries = 0 }: Props) {
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
  if (stripped.startsWith('[Tool result:') || stripped === 'Current screen. What is your next action?') {
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
    
    return (
      <div className="kim-msg-row kim-msg-row--user">
        <div className="kim-bubble kim-bubble--user">{text}</div>
      </div>
    );
  }

  if (message.role === 'tool') {
    const text =
      typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content, null, 2);
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
    let cleanContent = content.trim().replace(/^(?:Gemini said|Claude said|Assistant said):\s*/i, '');
    
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
      </div>
    );
  }

  const textBlocks    = content.filter(b => b.type === 'text') as Array<{ type: 'text'; text: string }>;
  const toolUseBlocks = content.filter(isToolUse);
  const toolResultBlocks = content.filter(isToolResult);
  const hasText  = textBlocks.length > 0;
  const hasTools = toolUseBlocks.length > 0 || toolResultBlocks.length > 0;

  if (!hasText && !hasTools) return null;

  return (
    <div className="kim-msg-row kim-msg-row--assistant">
      <div style={{ maxWidth: '78%', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {hasText && (
          <div className={`kim-bubble kim-bubble--assistant${hasTools ? ' kim-bubble--assistant-group-top' : ''}`}>
            {textBlocks.map((b, i) => (
              <div key={i}>
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
        {toolResultBlocks.map((b, i) => <ToolResultCard key={i} block={b} />)}
      </div>
    </div>
  );
}
