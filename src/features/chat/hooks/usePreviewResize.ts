import { useCallback, useEffect, useRef, useState } from 'react';
import type { ComponentProps, PointerEvent as ReactPointerEvent } from 'react';

/**
 * The divider between the transcript and the artifact preview is draggable, and
 * where the reader leaves it is remembered per browser. The width lives in a CSS
 * variable on the pane container rather than in the panel's props: the panel
 * already knows how to shrink (its toolbar and page rendering are both fluid),
 * and the split needs to move while the transcript is re-rendering every frame
 * of a stream — writing the variable by hand keeps a stale React render from
 * snapping the pane back mid-drag.
 */

const STORAGE_KEY = 'chat.previewWidth';
/** Narrower than this and the preview's own toolbar stops fitting on one row. */
const MIN_PREVIEW = 300;
/** The transcript keeps a readable column even if the panel is dragged wide. */
const MIN_TRANSCRIPT = 420;
const KEY_STEP = 24;
/** Mirrors the CSS fallback `min(42vw, 640px)` for keyboard moves before any drag. */
const DEFAULT_RATIO = 0.42;
const DEFAULT_MAX = 640;

function clampWidth(width: number, available: number) {
  const max = Math.max(MIN_PREVIEW, available - MIN_TRANSCRIPT);
  return Math.round(Math.min(Math.max(width, MIN_PREVIEW), max));
}

function readStoredWidth(): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const value = raw ? Number(raw) : NaN;
    return Number.isFinite(value) && value >= MIN_PREVIEW ? Math.round(value) : null;
  } catch {
    return null;
  }
}

export function usePreviewResize(label: string) {
  const panelsRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState<number | null>(readStoredWidth);
  const [resizing, setResizing] = useState(false);
  const drag = useRef<{ pointerId: number; right: number; available: number; last: number } | null>(null);

  const apply = useCallback((value: number | null) => {
    const box = panelsRef.current;
    if (!box) return;
    if (value == null) box.style.removeProperty('--preview-width');
    else box.style.setProperty('--preview-width', `${value}px`);
  }, []);

  useEffect(() => { apply(width); }, [width, apply]);

  // A window that shrinks has to take the panel with it, or the transcript
  // disappears and the only way back is a hard reload.
  useEffect(() => {
    const box = panelsRef.current;
    if (!box || width == null) return;
    const observer = new ResizeObserver(() => {
      const next = clampWidth(width, box.clientWidth);
      if (next !== width) setWidth(next);
    });
    observer.observe(box);
    return () => observer.disconnect();
  }, [width]);

  useEffect(() => () => { document.body.classList.remove('is-resizing-preview'); }, []);

  const commit = (value: number) => {
    setWidth(value);
    try { localStorage.setItem(STORAGE_KEY, String(value)); } catch { /* private mode */ }
  };

  const fallbackWidth = () => {
    const box = panelsRef.current;
    if (!box) return DEFAULT_MAX;
    return Math.min(box.clientWidth * DEFAULT_RATIO, DEFAULT_MAX);
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !panelsRef.current) return;
    const rect = panelsRef.current.getBoundingClientRect();
    const start = clampWidth(width ?? fallbackWidth(), rect.width);
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { pointerId: event.pointerId, right: rect.right, available: rect.width, last: start };
    setResizing(true);
    document.body.classList.add('is-resizing-preview');
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const next = clampWidth(state.right - event.clientX, state.available);
    if (next === state.last) return;
    state.last = next;
    apply(next);
  };

  const finish = (event: ReactPointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    if (!state || state.pointerId !== event.pointerId) return;
    drag.current = null;
    setResizing(false);
    document.body.classList.remove('is-resizing-preview');
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    commit(state.last);
  };

  const nudge = (delta: number) => {
    const box = panelsRef.current;
    if (!box) return;
    commit(clampWidth((width ?? fallbackWidth()) + delta, box.clientWidth));
  };

  const reset = () => {
    setWidth(null);
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* private mode */ }
  };

  // Arrow left widens the panel: the separator moves left, and the pane it
  // bounds is the one on its right.
  const splitterProps: ComponentProps<'div'> = {
    role: 'separator',
    'aria-orientation': 'vertical',
    'aria-label': label,
    tabIndex: 0,
    className: `preview-splitter${resizing ? ' is-active' : ''}`,
    onPointerDown,
    onPointerMove,
    onPointerUp: finish,
    onPointerCancel: finish,
    onDoubleClick: reset,
    onKeyDown: (event) => {
      if (event.key === 'ArrowLeft') { event.preventDefault(); nudge(KEY_STEP); }
      else if (event.key === 'ArrowRight') { event.preventDefault(); nudge(-KEY_STEP); }
      else if (event.key === 'Home') { event.preventDefault(); reset(); }
    },
  };

  return { panelsRef, splitterProps };
}
