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

interface UseOsNotificationsOptions {
  /** Set to false to suppress all notifications (e.g. when window is focused). */
  enabled?: boolean;
}

export function useOsNotifications({ enabled = true }: UseOsNotificationsOptions = {}) {
  const enabledRef = useRef(enabled);
  useEffect(() => { enabledRef.current = enabled; }, [enabled]);

  useEffect(() => {
    const unlistenPromises: Promise<() => void>[] = [];

    unlistenPromises.push(
      listen<{ termination: string; success: boolean }>('kim:run-done', (e) => {
        if (!enabledRef.current) return;
        const { termination, success } = e.payload;
        if (success) {
          notify('Kim', 'Task completed successfully.').catch(() => {});
        } else {
          notify('Kim', `Task ended: ${termination}`).catch(() => {});
        }
      }),
    );

    unlistenPromises.push(
      listen<{ reason: string; recoverable: boolean }>('kim:run-failed', (e) => {
        if (!enabledRef.current) return;
        const { reason } = e.payload;
        notify('Kim — task failed', reason.slice(0, 80)).catch(() => {});
      }),
    );

    return () => {
      unlistenPromises.forEach(p => p.then(fn => fn()).catch(() => {}));
    };
  }, []);
}
