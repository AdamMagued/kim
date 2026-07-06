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
  // H1: registered in BOTH modes (was new-chat only — and outputRef was never
  // attached anywhere, so auto-follow was permanently forced on). StreamRenderer
  // now attaches outputRef to `.kim-messages` in both branches; the newChatMode
  // dep re-attaches the listener when the branch (and thus the DOM node) swaps.
  useEffect(() => {
    const scroller = outputRef.current;
    if (!scroller) return;

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
    // H1: gate BOTH branches on autoFollowOutput so a user who scrolled up to
    // read is not yanked back to the bottom on every activity flush / tick.
    // (ChatView resets autoFollowOutput to true on session switch so opening a
    // session still jumps to the latest message.)
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
