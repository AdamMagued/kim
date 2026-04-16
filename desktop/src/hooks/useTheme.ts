import { useState, useEffect, useCallback } from 'react';
import type { Theme } from '../types';

function getSystemTheme(): 'dark' | 'light' {
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return 'light';
}

function applyTheme(resolved: 'dark' | 'light') {
  const html = document.documentElement;
  if (resolved === 'dark') {
    html.classList.add('dark');
  } else {
    html.classList.remove('dark');
  }
}

export function useTheme(initial: Theme = 'system') {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = localStorage.getItem('kim-theme') as Theme | null;
    return stored ?? initial;
  });

  const resolvedTheme: 'dark' | 'light' =
    theme === 'system' ? getSystemTheme() : theme;

  useEffect(() => {
    applyTheme(resolvedTheme);
  }, [resolvedTheme]);

  // React to OS-level theme changes when "system" is selected
  useEffect(() => {
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => applyTheme(getSystemTheme());
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    localStorage.setItem('kim-theme', next);
    setThemeState(next);
  }, []);

  return { theme, resolvedTheme, setTheme };
}
