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
  const paragraphs = text.split(/\n\n+/);
  return (
    <div className="prose">
      {paragraphs.map((para, i) => {
        if (para.startsWith('```')) {
          const lines = para.split('\n');
          const code = lines
            .slice(1, lines.lastIndexOf('```') === -1 ? undefined : lines.lastIndexOf('```'))
            .join('\n');
          return (
            <pre key={i}>
              <code>{code}</code>
            </pre>
          );
        }
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

  // System messages
  if (isSystem) {
    return (
      <div className="kim-msg-row kim-msg-row--system">
        <span className="kim-system-note">
          {typeof message.content === 'string' ? message.content : 'System message'}
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
      <div className="kim-msg-row kim-msg-row--user">
        <div className="kim-bubble kim-bubble--user">{text}</div>
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
      <div className="kim-tool-result-row">
        <div className="kim-tool-result-inline">
          <span className="kim-tool-result-inline__name">
            {message.name ?? 'tool'}:
          </span>{' '}
          {text}
        </div>
      </div>
    );
  }

  // Assistant message
  const content = message.content;

  if (typeof content === 'string') {
    return (
      <div className="kim-msg-row kim-msg-row--assistant">
        <div className="kim-bubble kim-bubble--assistant">
          {renderText(content)}
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
    <div className="kim-msg-row kim-msg-row--assistant">
      <div style={{ maxWidth: '78%', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {hasText && (
          <div
            className={`kim-bubble kim-bubble--assistant${
              hasTools ? ' kim-bubble--assistant-group-top' : ''
            }`}
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
