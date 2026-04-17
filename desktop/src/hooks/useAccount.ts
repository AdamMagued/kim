import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';
import type { KimAccount } from '../types';

interface UseAccountReturn {
  account: KimAccount | null;
  loading: boolean;
  setAccount: (account: KimAccount) => Promise<void>;
  clearAccount: () => void;
}

export function useAccount(): UseAccountReturn {
  const [account, setAccountState] = useState<KimAccount | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    invoke<KimAccount | null>('load_account')
      .then(a => setAccountState(a))
      .catch(() => setAccountState(null))
      .finally(() => setLoading(false));
  }, []);

  const setAccount = useCallback(async (next: KimAccount) => {
    await invoke('save_account', { account: next });
    setAccountState(next);
  }, []);

  const clearAccount = useCallback(() => {
    setAccountState(null);
  }, []);

  return { account, loading, setAccount, clearAccount };
}
