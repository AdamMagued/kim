import { cleanActivityText } from './utils';

/** Raw JSON shape from the Codex CLI JSONL stream. */
interface RawCodexMessage {
  type?: string;
  item?: {
    type?: string;
    text?: string;
    action?: { command?: string };
  };
}

/**
 * Discriminated union for Codex CLI JSONL `item.completed` events.
 * `null` means the JSON object is not a Codex event at all.
 */
export type CodexParsedEvent =
  | { type: 'codex_agent_message'; payload: string }
  | { type: 'codex_reasoning'; payload: string }
  | { type: 'codex_shell_call'; payload: string }
  | { type: 'codex_ignored' }
  | null;

/**
 * Parse a Codex CLI JSONL envelope.
 *
 * Returns `null` if `parsed` is not a Codex lifecycle event.
 * Returns one of the `codex_*` variants when it is.
 */
export function parseCodexItemCompleted(parsed: RawCodexMessage): CodexParsedEvent {
  if (parsed.type === 'item.completed' && parsed.item) {
    const item = parsed.item;
    if (item.type === 'agent_message' && item.text?.trim()) {
      return { type: 'codex_agent_message', payload: item.text.trim() };
    }
    if (item.type === 'reasoning' && item.text?.trim()) {
      return { type: 'codex_reasoning', payload: cleanActivityText(item.text.trim()) };
    }
    if (item.type === 'local_shell_call' && item.action?.command) {
      return { type: 'codex_shell_call', payload: item.action.command };
    }
    return { type: 'codex_ignored' };
  }
  if (
    parsed.type === 'thread.started' ||
    parsed.type === 'turn.started' ||
    parsed.type === 'turn.completed'
  ) {
    return { type: 'codex_ignored' };
  }
  return null;
}
