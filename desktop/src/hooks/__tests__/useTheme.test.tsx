import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useTheme } from '../useTheme';

// ---------------------------------------------------------------------------
// localStorage stub — Node v25 exposes a global `localStorage` that shadows
// jsdom's implementation and lacks clear/removeItem, so we replace the global
// with a simple in-memory Map-backed stub for these tests.
// ---------------------------------------------------------------------------
function makeLocalStorageStub() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => { store.set(key, value); },
    removeItem: (key: string) => { store.delete(key); },
    clear: () => { store.clear(); },
    get length() { return store.size; },
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    _store: store,
  };
}

// Helper: build a matchMedia mock that lets tests fire the 'change' event.
function makeMatchMediaMock(prefersDark: boolean) {
  const listeners: Array<() => void> = [];
  const mq = {
    get matches() {
      return prefersDark;
    },
    addEventListener(_event: string, cb: () => void) {
      listeners.push(cb);
    },
    removeEventListener(_event: string, cb: () => void) {
      const idx = listeners.indexOf(cb);
      if (idx !== -1) listeners.splice(idx, 1);
    },
    fireChange() {
      listeners.forEach(cb => cb());
    },
  };
  return mq;
}

describe('useTheme', () => {
  let localStorageStub: ReturnType<typeof makeLocalStorageStub>;

  beforeEach(() => {
    localStorageStub = makeLocalStorageStub();
    vi.stubGlobal('localStorage', localStorageStub);
    document.documentElement.classList.remove('dark');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // -----------------------------------------------------------------------
  // 1. resolved_theme_is_state
  // -----------------------------------------------------------------------
  it('resolved_theme_is_state — resolvedTheme starts as the initial theme and updates on setTheme', () => {
    // No localStorage entry, so initial='dark' wins.
    const { result } = renderHook(() => useTheme('dark'));

    expect(result.current.resolvedTheme).toBe('dark');
    expect(result.current.theme).toBe('dark');

    act(() => {
      result.current.setTheme('light');
    });

    expect(result.current.theme).toBe('light');
    expect(result.current.resolvedTheme).toBe('light');
  });

  // -----------------------------------------------------------------------
  // 2. system_resolves_via_matchmedia
  // -----------------------------------------------------------------------
  it('system_resolves_via_matchmedia — resolvedTheme is "dark" when system prefers dark and applyTheme runs', () => {
    const mq = makeMatchMediaMock(true /* prefersDark */);
    vi.stubGlobal('matchMedia', () => mq);

    const { result } = renderHook(() => useTheme('system'));

    // Initial state is resolved from matchMedia immediately.
    expect(result.current.resolvedTheme).toBe('dark');

    // applyTheme should have added the 'dark' class to <html>.
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('system_resolves_via_matchmedia — resolvedTheme is "light" when system prefers light', () => {
    const mq = makeMatchMediaMock(false /* prefersDark */);
    vi.stubGlobal('matchMedia', () => mq);

    const { result } = renderHook(() => useTheme('system'));

    expect(result.current.resolvedTheme).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  // -----------------------------------------------------------------------
  // 3. reacts_to_os_change
  // -----------------------------------------------------------------------
  it('reacts_to_os_change — firing the matchMedia change event updates resolvedTheme', () => {
    // Start: OS prefers light.
    let prefersDark = false;
    const listeners: Array<() => void> = [];
    const mq = {
      get matches() {
        return prefersDark;
      },
      addEventListener(_event: string, cb: () => void) {
        listeners.push(cb);
      },
      removeEventListener(_event: string, cb: () => void) {
        const idx = listeners.indexOf(cb);
        if (idx !== -1) listeners.splice(idx, 1);
      },
    };
    vi.stubGlobal('matchMedia', () => mq);

    const { result } = renderHook(() => useTheme('system'));
    expect(result.current.resolvedTheme).toBe('light');

    // OS switches to dark — update mock then fire the registered handler.
    act(() => {
      prefersDark = true;
      listeners.forEach(cb => cb());
    });

    expect(result.current.resolvedTheme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('reacts_to_os_change — switching away from "system" stops reacting to OS changes', () => {
    let prefersDark = false;
    const listeners: Array<() => void> = [];
    const mq = {
      get matches() {
        return prefersDark;
      },
      addEventListener(_event: string, cb: () => void) {
        listeners.push(cb);
      },
      removeEventListener(_event: string, cb: () => void) {
        const idx = listeners.indexOf(cb);
        if (idx !== -1) listeners.splice(idx, 1);
      },
    };
    vi.stubGlobal('matchMedia', () => mq);

    const { result } = renderHook(() => useTheme('system'));
    expect(result.current.resolvedTheme).toBe('light');

    // Switch to an explicit theme — the OS listener should be torn down.
    act(() => {
      result.current.setTheme('dark');
    });
    expect(result.current.resolvedTheme).toBe('dark');

    // Fire OS change — should NOT flip resolvedTheme back.
    act(() => {
      prefersDark = false;
      listeners.forEach(cb => cb());
    });

    // Still 'dark' because we are no longer in 'system' mode.
    expect(result.current.resolvedTheme).toBe('dark');
  });
});
