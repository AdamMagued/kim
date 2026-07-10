import { useCallback, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';

/**
 * V-audit #6: useState variant for arrays that grow unboundedly across a
 * long-running session — caps at `max` entries, dropping the oldest first
 * (same trim-oldest behavior as the activity feed's MAX_ACTIVITY_ITEMS cap
 * in useChatStream.ts). Accepts both updater-function and direct-value
 * setState calls, same as the raw setter it wraps.
 */
export function useCappedState<T>(max: number, initial: T[] = []): [T[], Dispatch<SetStateAction<T[]>>] {
  const [value, setValueRaw] = useState<T[]>(initial);
  const setValue = useCallback<Dispatch<SetStateAction<T[]>>>(
    updater => {
      setValueRaw(prev => {
        const next = typeof updater === 'function' ? (updater as (p: T[]) => T[])(prev) : updater;
        return next.length > max ? next.slice(next.length - max) : next;
      });
    },
    [max]
  );
  return [value, setValue];
}
