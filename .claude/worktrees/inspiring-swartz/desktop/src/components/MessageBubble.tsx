import type { KimMessage, ContentBlock, ToolUseBlock, ToolResultBlock } from '../types';
import { ToolUseCard, ToolResultCard } from './ToolCallCard';

function isToolUse(b: ContentBlock): b is ToolUseBlock {
  return b.type === 'tool_use';
}

function isToolResult(b: ContentBlock): b is ToolResultBlock {
  return b.type === 'tool_result';
}

// Minimal markdown-to-text: preserve newlines, bold, inline code
function renderText(text: string) {
  // Split into paragraphs
  const paragraphs = text.split(/\n\n+/);
  return (
    <div className="prose">
      {paragraphs.map((para, i) => {
        // Handle code blocks
        if (para.startsWith('```')) {
          const lines = para.split('\n');
          const code = lines.slice(1, lines.lastIndexOf('```') === -1 ? undefined : lines.lastIndexOf('```')).join('\n');
          return <pre key={i}><code>{code}</code></pre>;
        }
        // Inline: split on newlines within paragraph
        const lines = para.split('\n');
        return (
          <p key={i}>
            {lines.map((line, j) => (
              <span key={j}>
                {line}
                {j < lines.length - 1 && <br />}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

interface Props {
  message: KimMessage;
}

export function MessageBubble({ message }: Props) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';

  // System messages: show as a dim note
  if (isSystem) {
    return (
      <div
        style={{
          display: 'flex',
          justifyContent: 'center',
          padding: '6px 16px',
        }}
      >
        <span
          style={{
            fontSize: '11px',
            color: 'var(--text-muted)',
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '12px',
            padding: '3px 12px',
          }}
        >
          {typeof message.content === 'string'
            ? message.content
            : 'System message'}
        </span>
      </div>
    );
  }

  // User message
  if (isUser) {
    const text =
      typeof message.content === 'string'
        ? message.content
        : message.content
            .filter(b => b.type === 'text')
            .map(b => (b as { type: 'text'; text: string }).text)
            .join('\n');

    return (
      <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '4px 16px' }}>
        <div
          style={{
            maxWidth: '70%',
            background: 'var(--bubble-user-bg)',
            color: 'var(--bubble-user-text)',
            borderRadius: '18px 18px 4px 18px',
            padding: '10px 16px',
            fontSize: '14px',
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {text}
        </div>
      </div>
    );
  }

  // Tool-result message (role === 'tool')
  if (message.role === 'tool') {
    const text =
      typeof message.content === 'string'
        ? message.content
        : JSON.stringify(message.content, null, 2);

    return (
      <div style={{ padding: '4px 16px' }}>
        <div
          style={{
            background: 'var(--bg-card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            fontFamily: 'monospace',
            color: 'var(--text-muted)',
            maxHeight: '150px',
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
          }}
        >
          <span style={{ fontWeight: 600, color: 'var(--accent)' }}>
            {message.name ?? 'tool'}:{' '}
          </span>
          {text}
        </div>
      </div>
    );
  }

  // Assistant message — can contain text + tool_use blocks
  const content = message.content;

  if (typeof content === 'string') {
    return (
      <div style={{ display: 'flex', justifyContent: 'flex-start', padding: '4px 16px' }}>
        <div
          style={{
            maxWidth: '75%',
            background: 'var(--bubble-ai-bg)',
            color: 'var(--bubble-ai-text)',
            borderRadius: '18px 18px 18px 4px',
            padding: '10px 16px',
            fontSize: '14px',
          }}
        >
          {renderText(content)}
          {message.tool_calls && message.tool_calls.length > 0 && (
            <div style={{ marginTop: '8px' }}>
              {message.tool_calls.map(tc => (
                <ToolUseCard
                  key={tc.id}
                  block={{
                    type: 'tool_use',
                    id: tc.id,
                    name: tc.function.name,
                    input: (() => {
                      try {
                        return JSON.parse(tc.function.arguments) as Record<string, unknown>;
                      } catch {
                        return { raw: tc.function.arguments };
                      }
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

  // Array content
  const textBlocks = content.filter(b => b.type === 'text') as Array<{ type: 'text'; text: string }>;
  const toolUseBlocks = content.filter(isToolUse);
  const toolResultBlocks = content.filter(isToolResult);

  const hasText = textBlocks.length > 0;
  const hasTools = toolUseBlocks.length > 0 || toolResultBlocks.length > 0;

  if (!hasText && !hasTools) {
    return null;
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', padding: '4px 16px' }}>
      <div style={{ maxWidth: '75%' }}>
        {hasText && (
          <div
            style={{
              background: 'var(--bubble-ai-bg)',
              color: 'var(--bubble-ai-text)',
              borderRadius: hasTools ? '18px 18px 4px 4px' : '18px 18px 18px 4px',
              padding: '10px 16px',
              fontSize: '14px',
              marginBottom: hasTools ? '2px' : 0,
            }}
          >
            {textBlocks.map((b, i) => (
              <div key={i}>{renderText(b.text)}</div>
            ))}
          </div>
        )}

        {toolUseBlocks.map(b => (
          <ToolUseCard key={b.id} block={b} />
        ))}

        {toolResultBlocks.map((b, i) => (
          <ToolResultCard key={i} block={b} />
        ))}
      </div>
    </div>
  );
}
