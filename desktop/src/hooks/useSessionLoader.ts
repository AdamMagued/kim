import { useState, useEffect, useRef } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, KimMessage, Settings } from '../types';
import type { CodexRunGroup } from '../components/chat/types';
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
    const isSelfTransition =
      isSessionChange && prevId === null && session.session_id === stream.currentTaskRef.current?.id.toString();
    const isSeamlessTransition = isSelfTransition || isCodexContinuation;
    lastLoadedSessionIdRef.current = session.session_id;
    setLoadError(null);

    if (isSessionChange && !isSeamlessTransition) {
      setLoadingMessages(true);
      stream.setLiveHistory([]);
      stream.setRunHistory([]);
      setCodexRuns([]);

      invoke<any[]>('load_run_history', {
        sessionId: session.session_id,
        sessionDate: session.date || null,
        kimDir: settings.kim_sessions_dir || null,
        codexDir:
          session.session_type === 'codex' && session.project_path
            ? `${session.project_path}/.codex/sessions`
            : settings.codex_sessions_dir || null,
      })
        .then(runs => {
          if (runs && Array.isArray(runs)) {
            stream.setRunHistory(runs);
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
        .catch(() => {});
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
        const prev = prevMsgCountRef.current.get(session.session_id) ?? 0;
        const displayMsgs = msgs.filter(m => m.role !== 'compact_summary');
        const lastAssistantIdx = displayMsgs.reduceRight(
          (found, m, i) => (found === -1 && m.role === 'assistant' ? i : found),
          -1
        );
        if (
          isSessionChange &&
          !isSeamlessTransition &&
          prev > 0 &&
          displayMsgs.length > prev &&
          lastAssistantIdx >= prev
        ) {
          setNewestMsgIdx(lastAssistantIdx);
        } else {
          setNewestMsgIdx(null);
        }
        prevMsgCountRef.current.set(session.session_id, displayMsgs.length);
        setMessages(displayMsgs);

        if (session.session_type === 'codex') {
          setCodexRuns(groupCodexMessages(msgs));
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
        // B12: don't silently swallow — surface so the user isn't left with a
        // bare "No messages" with no explanation.
        setMessages([]);
        setLoadError("Couldn't read this session file.");
        toast("Couldn't read this session file.", 'error', 4000);
      })
      .finally(() => {
        if (isSessionChange && !isSeamlessTransition) setLoadingMessages(false);
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
