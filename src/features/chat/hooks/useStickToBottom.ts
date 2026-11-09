import { useCallback, useEffect, useRef } from 'react';

/**
 * Distance from the bottom (px) that still counts as "following the stream".
 * Anything closer re-arms the follow; anything further only stops it when the
 * reader is the one who scrolled.
 */
export const STICK_THRESHOLD = 72;

/** How far the reader has to detour before a way back is worth offering. */
export const SCROLL_BTN_DISTANCE = STICK_THRESHOLD * 2;

const UPWARD_KEYS = new Set(['ArrowUp', 'PageUp', 'Home']);

/**
 * Keeps a scroll container pinned to the bottom while content grows, but only
 * when the reader is already at the bottom. Setting `scrollTop` directly avoids
 * restarting a smooth-scroll animation on every streaming frame, which is what
 * makes naive `scrollIntoView` loops look like they are stuttering.
 *
 * The follow is released by *input*, not by scroll distance. Pinning the
 * container to `scrollHeight` also emits a scroll event, and that event is
 * dispatched after the next frame — by which time a tool step or an artifact
 * card may have added hundreds of pixels. Judging by distance alone therefore
 * read our own pin as "the reader scrolled away" and turned the follow off for
 * good, which is why a 38-step agent run ended up stranded mid-transcript with
 * no way back to the bottom.
 */
export function useStickToBottom() {
  const containerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  /** Set once the reader scrolls by hand; cleared when the view is back at the bottom. */
  const tookOverRef = useRef(false);

  const distanceToBottom = useCallback(() => {
    const element = containerRef.current;
    if (!element) return 0;
    return element.scrollHeight - element.scrollTop - element.clientHeight;
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const element = containerRef.current;
    if (!element) return;
    stickRef.current = true;
    tookOverRef.current = false;
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

    const atBottom = () => element.scrollHeight - element.scrollTop - element.clientHeight <= STICK_THRESHOLD;

    const onScroll = () => {
      if (atBottom()) {
        stickRef.current = true;
        tookOverRef.current = false;
        return;
      }
      // Not at the bottom: only the reader can be responsible. If the follow is
      // still armed, this scroll came from our own pin and has to be corrected
      // rather than treated as an exit.
      if (tookOverRef.current) stickRef.current = false;
      else if (stickRef.current) element.scrollTop = element.scrollHeight;
    };

    const takeOver = () => { tookOverRef.current = true; };

    const onWheel = (event: WheelEvent) => { if (event.deltaY < 0) takeOver(); };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.target instanceof Node && element.contains(event.target) && UPWARD_KEYS.has(event.key)) takeOver();
    };
    // The scrollbar lives inside the padding box but outside the content box,
    // so a press there is a drag, not a click on a message.
    const onPointerDown = (event: PointerEvent) => {
      const rect = element.getBoundingClientRect();
      if (event.clientX - rect.left > element.clientWidth) takeOver();
    };

    element.addEventListener('scroll', onScroll, { passive: true });
    element.addEventListener('wheel', onWheel, { passive: true });
    element.addEventListener('touchmove', takeOver, { passive: true });
    element.addEventListener('pointerdown', onPointerDown, { passive: true });
    document.addEventListener('keydown', onKeyDown);

    // Height can change without a new message: a markdown table reflows when the
    // preview pane narrows, an artifact thumbnail loads, a step group expands.
    // None of those go through `follow`, so watch the content box as well.
    let observed: Element | null = null;
    const observer = new ResizeObserver(() => {
      if (stickRef.current) element.scrollTop = element.scrollHeight;
    });
    const bindContent = () => {
      const target = element.firstElementChild;
      if (!target || target === observed) return;
      if (observed) observer.unobserve(observed);
      observed = target;
      observer.observe(target);
    };
    bindContent();
    const contentWatcher = new MutationObserver(bindContent);
    contentWatcher.observe(element, { childList: true });

    return () => {
      element.removeEventListener('scroll', onScroll);
      element.removeEventListener('wheel', onWheel);
      element.removeEventListener('touchmove', takeOver);
      element.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      contentWatcher.disconnect();
      observer.disconnect();
    };
  }, []);

  return { containerRef, follow, scrollToBottom, isStuck: () => stickRef.current, distanceToBottom };
}
