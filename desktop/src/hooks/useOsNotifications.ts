import { useEffect, useRef } from 'react';
import { listen } from '@tauri-apps/api/event';

// Notification permission state is obtained lazily on first use.
let _permissionGranted: boolean | null = null;

async function checkPermission(): Promise<boolean> {
  if (_permissionGranted !== null) return _permissionGranted;
  try {
    const { isPermissionGranted, requestPermission } = await import(
      '@tauri-apps/plugin-notification'
    );
    let granted = await isPermissionGranted();
    if (!granted) {
      const permission = await requestPermission();
      granted = permission === 'granted';
    }
    _permissionGranted = granted;
    return granted;
  } catch {
    _permissionGranted = false;
    return false;
  }
}

async function notify(title: string, body: string): Promise<void> {
  const ok = await checkPermission();
  if (!ok) return;
  try {
    const { sendNotification } = await import('@tauri-apps/plugin-notification');
    sendNotification({ title, body });
  } catch {
    // Notification is best-effort; never let it crash the app.
  }
}

/** Request notification permission once, eagerly (call at onboarding / first run
 *  rather than at first task completion). (B8) */
export function primeNotificationPermission(): void {
  void checkPermission();
}

function windowIsFocused(): boolean {
  try {
    return document.visibilityState === 'visible' && document.hasFocus();
  } catch {
    return false;
  }
}

interface UseOsNotificationsOptions {
  /** Set to false to suppress all notifications. */
  enabled?: boolean;
}

export function useOsNotifications({ enabled = true }: UseOsNotificationsOptions = {}) {
  const enabledRef = useRef(enabled);
  useEffect(() => { enabledRef.current = enabled; }, [enabled]);

  useEffect(() => {
    // B8: ask for permission up front, not at the first completed task.
    primeNotificationPermission();

    const unlistenPromises: Promise<() => void>[] = [];

    // B8: notify ONLY from kim:run-done (single source of truth) so a failed run
    // no longer fires twice (run-done success=false + run-failed). Suppress while
    // the window is focused — the user is already looking.
    unlistenPromises.push(
      listen<{ termination: string; success: boolean }>('kim:run-done', (e) => {
        if (!enabledRef.current || windowIsFocused()) return;
        const { termination, success } = e.payload;
        if (success) {
          notify('Kim', 'Task completed successfully.').catch(() => {});
        } else {
          notify('Kim', `Task ended: ${termination}`).catch(() => {});
        }
      }),
    );

    return () => {
      unlistenPromises.forEach(p => p.then(fn => fn()).catch(() => {}));
    };
  }, []);
}
