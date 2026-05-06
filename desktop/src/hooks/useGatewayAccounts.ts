import { useState, useEffect, useCallback } from 'react';
import { invoke } from '@tauri-apps/api/core';

export interface GatewayAccount {
  id: number;
  label: string;
  provider: string;
}

interface UseGatewayAccountsReturn {
  accounts: GatewayAccount[];
  selectedAccountId: number | null;
  setSelectedAccountId: (id: number | null) => Promise<void>;
  refresh: () => void;
}

const GATEWAY_URL = 'http://localhost:8046';

export function useGatewayAccounts(projectRoot?: string | null): UseGatewayAccountsReturn {
  const [accounts, setAccounts] = useState<GatewayAccount[]>([]);
  const [selectedAccountId, setSelectedAccountIdState] = useState<number | null>(null);

  const fetchAccounts = useCallback(async () => {
    try {
      const res = await fetch(`${GATEWAY_URL}/api/accounts/active`, { signal: AbortSignal.timeout(3000) });
      if (res.ok) {
        const data = await res.json() as GatewayAccount[];
        setAccounts(data);
      }
    } catch {
      // gateway not running — silently ignore
    }
  }, []);

  useEffect(() => {
    invoke<string | null>('read_config_key', { key: 'selected_account_id', projectRoot: projectRoot ?? null })
      .then(val => {
        if (val) {
          const n = parseInt(val, 10);
          if (!isNaN(n)) setSelectedAccountIdState(n);
        }
      })
      .catch(() => {});
  }, [projectRoot]);

  useEffect(() => {
    fetchAccounts();
    const interval = setInterval(fetchAccounts, 30000);
    return () => clearInterval(interval);
  }, [fetchAccounts]);

  const setSelectedAccountId = useCallback(async (id: number | null) => {
    await invoke('write_config_key', {
      key: 'selected_account_id',
      value: id !== null ? String(id) : null,
      projectRoot: projectRoot ?? null,
    });
    setSelectedAccountIdState(id);
  }, [projectRoot]);

  return { accounts, selectedAccountId, setSelectedAccountId, refresh: fetchAccounts };
}
