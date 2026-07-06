import { useState, useEffect, useRef } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, KimMessage, Settings } from '../types';
import type { CodexRunGroup, ActivityItem } from '../components/chat/types';
import { collapseMessages, groupCodexMessages, isIntermediateToolCall } from '../components/chat/utils';
import { toast } from '../components/Toast';
import type { useChatStream } from './useChatStream';

interface UseSessionLoaderProps {
  session: SessionInfo | null;
  settings: Settings;
  messageReloadNonce: number;
  stream: ReturnType<typeof useChatStream>;
}

export function useSessionLoader({
  session,
  settings,
  messageReloadNonce,
  stream,
}: UseSessionLoaderProps) {
  const [messages, setMessages] = useState<KimMessage[]>([]);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [newestMsgIdx, setNewestMsgIdx] = useState<number | null>(null);
  const [codexRuns, setCodexRuns] = useState<CodexRunGroup[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  // B12: key the previous-count by session id so the "newest message" animation
  // heuristic doesn't compare against a different session's count after a switch.
  const prevMsgCountRef = useRef<Map<string, number>>(new Map());
  const lastLoadedSessionIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!session) {
      setMessages([]);
      lastLoadedSessionIdRef.current = null;
      return;
    }
    const prevId = lastLoadedSessionIdRef.current;
    const isSessionChange = prevId !== session.session_id;

    const isCodexContinuation =
      isSessionChange && prevId !== null && session.session_type === 'codex' && stream.hasSentMessageRef.current;
    // L6 (documented, not changed): this compares a backend session id against
    // the numeric task counter (PendingTask.id is 1,2,3…), so it is almost
    // certainly never true. Left as-is because changing the comparison would
    // alter session-transition semantics; the isCodexContinuation branch above
    // covers the real seamless-transition case.
    const isSelfTransition =
      isSessionChange && prevId === null && session.session_id === stream.currentTaskRef.current?.id.toString();
    const isSeamlessTransition = isSelfTransition || isCodexContinuation;
    lastLoadedSessionIdRef.current = session.session_id;
    setLoadError(null);

    // Codex run durations come from load_run_history; the run groups come from
    // load_session_messages. The two invokes race: if load_run_history resolves first
    // it patches an empty codexRuns, then the groupCodexMessages(...) overwrite clobbers
    // it (durations lost → "…" pills). Capture the runs so the group build can merge
    // durations in regardless of which resolves first.
    let capturedCodexRuns: Array<{ durationSec: number }> | null = null;

    if (isSessionChange && !isSeamlessTransition) {
      setLoadingMessages(true);
      stream.setLiveHistory([]);
      stream.setRunHistory([]);
      setCodexRuns([]);

      invoke<Array<{ activity: ActivityItem[]; durationSec: number; provider?: string | null }>>('load_run_history', {
        sessionId: session.session_id,
        sessionDate: session.date || null,
        kimDir: settings.kim_sessions_dir || null,
        codexDir:
          session.session_type === 'codex' && session.project_path
            ? `${session.project_path}/.codex/sessions`
            : settings.codex_sessions_dir || null,
      })
        .then(runs => {
          if (lastLoadedSessionIdRef.current !== session.session_id) return;
          if (runs && Array.isArray(runs)) {
            stream.setRunHistory(runs);
            capturedCodexRuns = runs;
            if (runs.length > 0) {
              setCodexRuns(prev =>
                prev.map((run, i) =>
                  i < runs.length && runs[i].durationSec > 0
                    ? { ...run, durationSec: runs[i].durationSec }
                    : run
                )
              );
            }
          }
        })
        .catch(() => {
          // M17: don't swallow silently — durations/traces will be missing, so
          // leave a trace for debugging without spamming the user with a toast
          // for auxiliary data.
          console.warn('load_run_history failed for session', session.session_id);
        });
    }

    invoke<KimMessage[]>('load_session_messages', {
      sessionId: session.session_id,
      sessionDate: session.date || null,
      kimDir: settings.kim_sessions_dir || null,
      codexDir:
        session.session_type === 'codex' && session.project_path
          ? `${session.project_path}/.codex/sessions`
          : settings.codex_sessions_dir || null,
    })
      .then(msgs => {
        if (lastLoadedSessionIdRef.current !== session.session_id) return;
        const prev = prevMsgCountRef.current.get(session.session_id) ?? 0;
        const displayMsgs = msgs.filter(m => m.role !== 'compact_summary');
        // Detection uses RAW indices (prevMsgCount is a raw message count). But
        // newestMsgIdx is consumed against collapseMessages(...) in StreamRenderer, so the
        // OUTPUT index must be in COLLAPSED space — otherwise the "newest" highlight/
        // animation lands on the wrong bubble whenever a retry collapses earlier messages.
        const lastAssistantIdxRaw = displayMsgs.reduceRight(
          (found, m, i) => (found === -1 && m.role === 'assistant' ? i : found),
          -1
        );
        const collapsedDisplay = collapseMessages(displayMsgs);
        const lastAssistantIdxCollapsed = collapsedDisplay.reduceRight(
          (found, c, i) => (found === -1 && c.msg.role === 'assistant' ? i : found),
          -1
        );
        if (
          isSessionChange &&
          !isSeamlessTransition &&
          prev > 0 &&
          displayMsgs.length > prev &&
          lastAssistantIdxRaw >= prev
        ) {
          setNewestMsgIdx(lastAssistantIdxCollapsed);
        } else {
          setNewestMsgIdx(null);
        }
        prevMsgCountRef.current.set(session.session_id, displayMsgs.length);
        setMessages(displayMsgs);

        if (session.session_type === 'codex') {
          const grouped = groupCodexMessages(msgs);
          // Merge durations captured from load_run_history (if it already resolved),
          // so the codex "Worked for …" pills show the real duration, not 0.
          // H9: on a same-session reload (messageReloadNonce bump after a run),
          // load_run_history is NOT re-invoked, so capturedCodexRuns stays null
          // and the pills reverted to "…". Fall back to the already-loaded
          // stream.runHistory durations in that case.
          const durationSource: Array<{ durationSec: number }> | null =
            capturedCodexRuns ?? (isSessionChange ? null : stream.runHistory);
          setCodexRuns(
            durationSource
              ? grouped.map((run, i) =>
                  durationSource[i]?.durationSec > 0
                    ? { ...run, durationSec: durationSource[i].durationSec }
                    : run
                )
              : grouped
          );
        } else {
          setCodexRuns([]);
        }

        if (isSessionChange && !isSeamlessTransition) {
          stream.setLiveHistory([]);
        } else {
          const liveAsstCount = collapseMessages(stream.liveHistory).filter(
            ({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)
          ).length;
          if (liveAsstCount > 0) {
            const savedAsstCount = collapseMessages(displayMsgs).filter(
              ({ msg }) => msg.role === 'assistant' && !isIntermediateToolCall(msg)
            ).length;
            if (savedAsstCount >= liveAsstCount) {
              stream.setLiveHistory([]);
            }
          }
        }
      })
      .catch(() => {
        if (lastLoadedSessionIdRef.current !== session.session_id) return;
        // B12: don't silently swallow — surface so the user isn't left with a
        // bare "No messages" with no explanation.
        setMessages([]);
        setLoadError("Couldn't read this session file.");
        toast("Couldn't read this session file.", 'error', 4000);
      })
      .finally(() => {
        if (isSessionChange && !isSeamlessTransition && lastLoadedSessionIdRef.current === session.session_id) setLoadingMessages(false);
      });
  }, [session, settings.kim_sessions_dir, settings.codex_sessions_dir, messageReloadNonce]);

  return {
    messages,
    setMessages,
    loadingMessages,
    newestMsgIdx,
    codexRuns,
    setCodexRuns,
    loadError,
  };
}
