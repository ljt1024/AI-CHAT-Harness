import { useCallback, useEffect, useRef } from 'react';

/** Distance from the bottom (px) that still counts as "following the stream". */
const STICK_THRESHOLD = 72;

/**
 * Keeps a scroll container pinned to the bottom while content grows, but only
 * when the reader is already at the bottom. Setting `scrollTop` directly avoids
 * restarting a smooth-scroll animation on every streaming frame, which is what
 * makes naive `scrollIntoView` loops look like they are stuttering.
 */
export function useStickToBottom() {
  const containerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const distanceToBottom = useCallback(() => {
    const element = containerRef.current;
    if (!element) return 0;
    return element.scrollHeight - element.scrollTop - element.clientHeight;
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const element = containerRef.current;
    if (!element) return;
    stickRef.current = true;
    element.scrollTo({ top: element.scrollHeight, behavior });
  }, []);

  /** Call after every content update; a no-op once the reader scrolled away. */
  const follow = useCallback(() => {
    if (!stickRef.current) return;
    const element = containerRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, []);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const onScroll = () => {
      stickRef.current = distanceToBottom() <= STICK_THRESHOLD;
    };
    element.addEventListener('scroll', onScroll, { passive: true });
    return () => element.removeEventListener('scroll', onScroll);
  }, [distanceToBottom]);

  return { containerRef, follow, scrollToBottom, isStuck: () => stickRef.current, distanceToBottom };
}
