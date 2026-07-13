import { useCallback, useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';

// Mirrors `ProviderAuthStatus` in desktop/src-tauri/src/lib.rs.
// `signed_in === false` with no `error` means "we probed and the user really
// isn't signed in". `error` set means the probe itself failed (timeout,
// bridge not ready) and the caller should retry rather than show a sign-out
// state to the user.
export interface ProviderAuthStatus {
  provider: string;
  signed_in: boolean;
  email?: string | null;
  name?: string | null;
  avatar?: string | null;
  error?: string | null;
}

export interface AuthState {
  status: ProviderAuthStatus | null;
  loading: boolean;
  /** True after the first probe completes, regardless of result. Lets the UI
   *  distinguish "still loading on app startup" from "probe ran and found no
   *  signed-in user". */
  probed: boolean;
}

/**
 * Hook that tracks the sign-in status for a single browser provider
 * (chatgpt / claude / gemini / etc.). Re-probes on:
 *   - provider change
 *   - manual `refresh()` call
 *   - `kim-auth-changed` Tauri event (emitted after sign-in/out)
 */
export function useAuthStatus(provider: string | null | undefined) {
  const [state, setState] = useState<AuthState>({
    status: null,
    loading: false,
    probed: false,
  });
  const lastProviderRef = useRef<string | null>(null);
  // H5: key the in-flight guard by provider so switching providers can start a
  // new probe while the old one is still pending, and stamp responses with the
  // provider they were requested for so a slow old probe can't be shown as the
  // new provider's auth state.
  const inFlightProviderRef = useRef<string | null>(null);

  const normalized = (provider ?? '').toLowerCase().replace(/^browser:/, '').trim();
  const supported = ['chatgpt', 'openai', 'claude', 'anthropic', 'gemini', 'google', 'deepseek', 'grok'].includes(normalized);

  const refresh = useCallback(async () => {
    if (!normalized || !supported) {
      setState({ status: null, loading: false, probed: true });
      return;
    }
    // Same-provider probe already pending — don't stack a duplicate.
    if (inFlightProviderRef.current === normalized) return;
    const target = normalized;
    inFlightProviderRef.current = target;
    setState(prev => ({ ...prev, loading: true }));
    // Discard results that land after the user switched to another provider.
    const isStale = () => lastProviderRef.current !== null && lastProviderRef.current !== target;
    try {
      const result = await invoke<ProviderAuthStatus>('provider_check_auth', {
        provider: target,
      });
      if (isStale()) return;
      setState({ status: result, loading: false, probed: true });
    } catch (err) {
      if (isStale()) return;
      setState({
        status: {
          provider: target,
          signed_in: false,
          error: String(err),
        },
        loading: false,
        probed: true,
      });
    } finally {
      if (inFlightProviderRef.current === target) inFlightProviderRef.current = null;
    }
  }, [normalized, supported]);

  // Probe whenever the provider changes.
  useEffect(() => {
    if (normalized !== lastProviderRef.current) {
      lastProviderRef.current = normalized;
      void refresh();
    }
  }, [normalized, refresh]);

  // Re-probe on auth-changed events emitted by provider_signin /
  // provider_signout / the post-signin watcher in Rust.
  useEffect(() => {
    let unlisten: UnlistenFn | null = null;
    let cancelled = false;
    void (async () => {
      try {
        unlisten = await listen('kim-auth-changed', () => {
          // Small delay so cookies are fully settled after the redirect.
          setTimeout(() => { if (!cancelled) void refresh(); }, 400);
        });
      } catch {
        // listen() can fail in non-Tauri test environments; harmless.
      }
    })();
    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, [refresh]);

  const signIn = useCallback(async () => {
    if (!supported) return;
    // The popup may already be open or a task may be running; let the rejection
    // propagate so the caller can surface it via a toast (no local handling).
    await invoke('provider_signin', { provider: normalized });
  }, [normalized, supported]);

  const signOut = useCallback(async () => {
    if (!supported) return;
    try {
      await invoke('provider_signout', { provider: normalized });
    } finally {
      // Force-refresh shortly after — the signout navigation invalidates
      // cookies and we want the indicator to flip immediately.
      setTimeout(() => { void refresh(); }, 800);
    }
  }, [normalized, supported, refresh]);

  return { ...state, refresh, signIn, signOut, supported };
}
