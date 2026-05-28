import { useCallback, useEffect, useRef } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, Settings, BrowserRestoreResult } from '../types';
import { toast } from '../components/Toast';
import {
  normalizeBrowserSite,
  browserProviderFromSession,
  browserSiteFromProvider,
  friendlyError,
  providerLabel,
} from '../components/chat/utils';

interface UseBrowserRestoreProps {
  session: SessionInfo | null;
  newChatMode: boolean;
  settings: Settings;
  localProvider: string | null;
  browserProvider: string;
  resolveProvider: () => string;
  browserCommandArgs: (targetSession?: SessionInfo | null, overrideSessionId?: string | null) => any;
}

export function useBrowserRestore({
  session,
  newChatMode,
  settings,
  localProvider,
  browserProvider,
  resolveProvider,
  browserCommandArgs,
}: UseBrowserRestoreProps) {
  const restoreSeqRef = useRef(0);
  const lastRestoreKeyRef = useRef<string | null>(null);

  const restoreBrowserForSession = useCallback(
    async (targetSession: SessionInfo, preferredSite?: string | null) => {
      const site =
        normalizeBrowserSite(preferredSite) ??
        browserProviderFromSession(targetSession) ??
        browserSiteFromProvider(resolveProvider()) ??
        browserProvider;
      if (!site) return null;
      try {
        return await invoke<BrowserRestoreResult>('restore_browser_for_session', {
          ...browserCommandArgs(targetSession),
          preferredSite: site,
        });
      } catch (err) {
        toast(`Could not restore the provider browser: ${friendlyError(String(err))}`, 'warning', 5000);
        return null;
      }
    },
    [browserCommandArgs, resolveProvider, browserProvider]
  );

  // Restore the provider browser for every concrete session entry path
  useEffect(() => {
    if (!session || newChatMode) return;

    const restoreKey = `${session.session_id}:${resolveProvider()}:${browserProvider}`;
    if (lastRestoreKeyRef.current === restoreKey) return;
    lastRestoreKeyRef.current = restoreKey;

    const seq = restoreSeqRef.current + 1;
    restoreSeqRef.current = seq;

    void (async () => {
      const result = await restoreBrowserForSession(session);
      if (restoreSeqRef.current !== seq) return;
      if (!result) return;

      if (result.restored) {
        toast(`Restored ${providerLabel('browser:' + result.site)} for this session.`, 'info', 2500);
      } else if (result.reason === 'stored_url_rejected') {
        toast(
          result.message ?? 'Saved browser URL was invalid, so Kim opened a fresh provider page.',
          'warning',
          5000
        );
      }
    })();
  }, [
    session?.session_key,
    session?.session_id,
    session?.date,
    session?.session_type,
    newChatMode,
    localProvider,
    settings.provider,
    browserProvider,
    restoreBrowserForSession,
  ]);

  return {
    restoreBrowserForSession,
    restoreSeqRef,
    lastRestoreKeyRef,
  };
}
