import { useEffect, useRef, useState } from 'react';

export interface UseSessionScrollOptions {
  newChatMode: boolean;
  activityLength: number;
  messagesLength: number;
}

export function useSessionScroll({ newChatMode, activityLength, messagesLength }: UseSessionScrollOptions) {
  const [autoFollowOutput, setAutoFollowOutput] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const outputRef = useRef<HTMLDivElement>(null);

  // ── Scroll behavior listener (detect user scrolling up) ────────────────────
  useEffect(() => {
    const scroller = outputRef.current;
    if (!newChatMode || !scroller) return;

    const onScroll = () => {
      const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      setAutoFollowOutput(distanceFromBottom < 80);
    };

    onScroll();
    scroller.addEventListener('scroll', onScroll, { passive: true });
    return () => scroller.removeEventListener('scroll', onScroll);
  }, [newChatMode]);

  // ── Auto-scroll on changes ───────────────────────────────────────────────
  useEffect(() => {
    // If we're starting a brand new chat and there's no activity yet,
    // don't scroll to the bottom (otherwise it skips the greeting/examples).
    if (newChatMode && activityLength === 0) {
      return;
    }
    if (!newChatMode) {
      // For existing sessions, or when newChatMode starts generating activity
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      return;
    }
    if (autoFollowOutput) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messagesLength, activityLength, newChatMode, autoFollowOutput]);

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  return {
    bottomRef,
    outputRef,
    autoFollowOutput,
    setAutoFollowOutput,
    scrollToBottom,
  };
}
