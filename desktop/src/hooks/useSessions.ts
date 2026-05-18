import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { SessionInfo, Settings } from '../types';

interface UseSessionsReturn {
  kimSessions: SessionInfo[];
  codexSessions: SessionInfo[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useSessions(settings: Settings): UseSessionsReturn {
  const [kimSessions, setKimSessions] = useState<SessionInfo[]>([]);
  const [codexSessions, setCodexSessions] = useState<SessionInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const all = await invoke<SessionInfo[]>('list_sessions', {
        kimDir: settings.kim_sessions_dir || null,
        codexDir: settings.codex_sessions_dir || null,
      });
      setKimSessions(all.filter(s => s.session_type === 'kim'));
      setCodexSessions(all.filter(s => s.session_type === 'codex'));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [settings.kim_sessions_dir, settings.codex_sessions_dir]);

  useEffect(() => {
    load();
  }, [load]);

  return { kimSessions, codexSessions, loading, error, refresh: load };
}
