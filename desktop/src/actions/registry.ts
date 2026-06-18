/**
 * K8: central action registry.
 *
 * Single source of truth for app commands so the command palette (⌘K), the
 * global-shortcut quick-ask, and the tray menu all share ONE implementation
 * instead of duplicating `invoke(...)` calls.
 */

export type ActionGroup =
  | 'session'
  | 'provider'
  | 'mode'
  | 'run'
  | 'settings'
  | 'privacy';

export interface Action {
  id: string;
  title: string;
  /** Extra search terms beyond the title (synonyms, abbreviations). */
  keywords: string[];
  group: ActionGroup;
  run: () => void;
  /** Optional: hide from the palette when not currently applicable. */
  enabled?: boolean;
}

/** Callbacks the host app supplies; any may be omitted (action then skipped). */
export interface ActionContext {
  newChat?: () => void;
  newCodeSession?: () => void;
  switchProvider?: (provider: string) => void;
  toggleMode?: () => void;
  cancelRun?: () => void;
  openSettings?: (pane?: string) => void;
  togglePrivacyPause?: () => void;
  isRunning?: boolean;
  providers?: string[];
}

const noop = () => {};

/** Build the action list from the host context. Omitted callbacks drop the
 *  corresponding action so the palette never lists dead commands. */
export function buildActions(ctx: ActionContext): Action[] {
  const out: Action[] = [];
  const push = (a: Action | null) => { if (a) out.push(a); };

  if (ctx.newChat) {
    push({ id: 'new-chat', title: 'New chat', keywords: ['chat', 'create', 'start'], group: 'session', run: ctx.newChat });
  }
  if (ctx.newCodeSession) {
    push({ id: 'new-code', title: 'New code session', keywords: ['code', 'create', 'repo'], group: 'session', run: ctx.newCodeSession });
  }
  if (ctx.toggleMode) {
    push({ id: 'toggle-mode', title: 'Toggle chat / code mode', keywords: ['mode', 'switch', 'tab'], group: 'mode', run: ctx.toggleMode });
  }
  if (ctx.switchProvider && ctx.providers) {
    for (const p of ctx.providers) {
      push({
        id: `provider-${p}`,
        title: `Switch provider: ${p}`,
        keywords: ['provider', 'model', 'switch', p],
        group: 'provider',
        run: () => ctx.switchProvider!(p),
      });
    }
  }
  if (ctx.cancelRun) {
    push({
      id: 'cancel-run',
      title: 'Cancel current run',
      keywords: ['cancel', 'stop', 'abort'],
      group: 'run',
      run: ctx.cancelRun,
      enabled: ctx.isRunning !== false,
    });
  }
  if (ctx.togglePrivacyPause) {
    push({ id: 'privacy-pause', title: 'Toggle privacy pause', keywords: ['privacy', 'pause', 'screen', 'screenshot'], group: 'privacy', run: ctx.togglePrivacyPause });
  }
  if (ctx.openSettings) {
    push({ id: 'settings', title: 'Open settings', keywords: ['settings', 'preferences', 'config'], group: 'settings', run: () => ctx.openSettings!() });
    push({ id: 'settings-ai', title: 'Open settings: AI', keywords: ['settings', 'ai', 'provider', 'model'], group: 'settings', run: () => ctx.openSettings!('ai') });
    push({ id: 'settings-system', title: 'Open settings: System', keywords: ['settings', 'system', 'hotkey', 'shortcut'], group: 'settings', run: () => ctx.openSettings!('system') });
  }
  return out;
}

/**
 * Fuzzy/substring filter + ranking. Empty query returns all (in registry order).
 * Ranking: title prefix > title substring > keyword match > subsequence match.
 */
export function filterActions(actions: Action[], query: string): Action[] {
  const q = query.trim().toLowerCase();
  if (!q) return actions.filter(a => a.enabled !== false);

  const scored: { action: Action; score: number }[] = [];
  for (const a of actions) {
    if (a.enabled === false) continue;
    const title = a.title.toLowerCase();
    let score = -1;
    if (title.startsWith(q)) score = 100;
    else if (title.includes(q)) score = 70;
    else if (a.keywords.some(k => k.toLowerCase().includes(q))) score = 50;
    else if (isSubsequence(q, title)) score = 20;
    if (score >= 0) scored.push({ action: a, score });
  }
  scored.sort((x, y) => y.score - x.score || x.action.title.localeCompare(y.action.title));
  return scored.map(s => s.action);
}

/** True if `needle` chars appear in order within `haystack`. */
export function isSubsequence(needle: string, haystack: string): boolean {
  let i = 0;
  for (let j = 0; j < haystack.length && i < needle.length; j++) {
    if (haystack[j] === needle[i]) i++;
  }
  return i === needle.length;
}

export { noop };
