// V-audit #4: a run that outlives its view never got its trace persisted.
// `kim-agent-done` is a bare boolean with no session envelope, so only the
// view that still "owns" the run (per RUN-IDENTITY in useChatStream.ts) can
// react to it — a view that unmounted mid-run (New Chat / session switch
// while a run is active) loses its accumulated `activity`/`runHistory` refs
// entirely on unmount, and no other view was ever listening for that
// session's events (belongsToView filters them out), so there is nothing
// left to persist once `kim-agent-done` finally arrives for a session
// nothing is displaying anymore.
//
// Fix: park a lightweight snapshot of "what this view had accumulated so
// far" here — a plain module-level map, not React state, so it survives the
// unmount that destroys the owning view's hooks/refs — keyed by the run's
// owning session id. Since `belongsToView` already drops every further
// session-scoped event for a foreign session app-wide, nothing new accrues
// for that session after unmount anyway, so the snapshot captured right at
// unmount time is exactly what the eventual `kim-agent-done` handler would
// have persisted had the view stayed mounted. Whichever ChatView happens to
// be mounted when `kim-agent-done` next fires (there is always exactly one)
// flushes any snapshot that isn't its own session before running its normal
// ownsRun-gated logic — see useChatStream.ts's kim-agent-done listener.
import { invoke } from '@tauri-apps/api/core';
import type { ActivityItem } from '../components/chat/types';
// Re-exported so useChatStream.ts only needs one import line for both of
// this session's V-audit helpers (unrelated concern, bundled for that reason).
export { useCappedState } from './useCappedState';

export interface PendingRunSnapshot {
  sessionId: string;
  sessionDate: string | null;
  kimDir: string | null;
  codexDir: string | null;
  runs: { activity: ActivityItem[]; durationSec: number; provider?: string | null }[];
}

const pendingRunSnapshots = new Map<string, PendingRunSnapshot>();

// Exported so tests can reset module-level state between cases (this map
// otherwise persists across every renderHook() in the same test file/run).
// Not used by production code paths.
export const __testOnlyPendingRunSnapshots = pendingRunSnapshots;

/** Called from a view's unmount cleanup if it still owns an in-flight run. */
export function parkOrphanedRunSnapshot(sessionId: string, snapshot: PendingRunSnapshot): void {
  pendingRunSnapshots.set(sessionId, snapshot);
}

/**
 * Convenience wrapper for the useChatStream unmount cleanup: takes the raw
 * ref values it has on hand, decides whether this view owns an in-flight run
 * worth parking (same "isRunning or has an owner" + "has accumulated
 * activity" checks kim-agent-done itself would apply), resolves the
 * session-dir fields the same way the normal save path does, and calls
 * parkOrphanedRunSnapshot if applicable. No-op otherwise.
 */
export function parkOrphanedRunSnapshotIfOwned(args: {
  isRunning: boolean;
  runOwnerSessionId: string | null;
  fallbackSessionId: string;
  activity: ActivityItem[];
  priorRuns: PendingRunSnapshot['runs'];
  startedAt: number | null;
  provider: string | null;
  completedCodeSession: { date?: string | null; project_path?: string | null } | null | undefined;
  fallbackSessionDate: string | null;
  kimSessionsDir: string | null;
  codexSessionsDir: string | null;
}): void {
  const sid = args.isRunning || args.runOwnerSessionId
    ? args.runOwnerSessionId ?? args.fallbackSessionId
    : null;
  if (!sid || args.activity.length === 0) return;
  const durationSec = args.startedAt ? Math.max(1, Math.round((Date.now() - args.startedAt) / 1000)) : 0;
  parkOrphanedRunSnapshot(sid, {
    sessionId: sid,
    sessionDate: args.completedCodeSession?.date ?? args.fallbackSessionDate ?? null,
    kimDir: args.kimSessionsDir,
    codexDir: args.completedCodeSession?.project_path
      ? `${args.completedCodeSession.project_path}/.codex/sessions`
      : args.codexSessionsDir,
    runs: [...args.priorRuns, { activity: args.activity, durationSec, provider: args.provider }],
  });
}

/**
 * Called at the top of every kim-agent-done handler, unconditionally (before
 * that view's own ownsRun-gated logic). Persists every parked snapshot that
 * ISN'T the caller's own session (that one is left for the normal ownsRun
 * path, which has fresher, more complete data — flushing it here too would
 * race two save_run_history calls for the same session and the loser would
 * clobber, since save_run_history overwrites the file).
 */
export function flushOrphanedRunSnapshots(exceptSessionId: string | null): void {
  if (pendingRunSnapshots.size === 0) return;
  for (const [sid, snap] of pendingRunSnapshots) {
    pendingRunSnapshots.delete(sid);
    if (sid === exceptSessionId) continue;
    invoke('save_run_history', {
      sessionId: snap.sessionId,
      sessionDate: snap.sessionDate,
      kimDir: snap.kimDir,
      codexDir: snap.codexDir,
      runs: snap.runs,
    }).catch(() => {});
  }
}
