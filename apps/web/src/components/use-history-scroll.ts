"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { historyEntryKey, recallPosition, rememberPosition, type SavedPosition } from "@/lib/navigation";

/**
 * Restores a client-loaded page's scroll position when the browser's Back or
 * Forward returns to the same history entry.  The browser cannot do this by
 * itself because the content arrives after the history traversal.
 *
 * The returned ref holds what was saved for this entry (null on a fresh
 * visit) from the first effect on, so a list can load as many items as were
 * shown before.
 */
export function useHistoryScroll(ready: boolean, count = 0): { readonly current: SavedPosition | null } {
  const saved = useRef<SavedPosition | null>(null);
  const pending = useRef<SavedPosition | null>(null);
  const counted = useRef(count);
  useLayoutEffect(() => {
    counted.current = count;
  }, [count]);

  useLayoutEffect(() => {
    // Read after commit: on a pushed navigation Next.js has written the new entry by now.
    const key = historyEntryKey();
    saved.current = pending.current = recallPosition(key);
    // Layout cleanup runs before the next page commits (and before Next.js scrolls it into view).
    // While a restore is still pending the page has not reached its position yet:
    // keep what was saved (React's development double-mount runs this cleanup early).
    return () => {
      if (!pending.current) rememberPosition(key, { y: window.scrollY, count: counted.current });
    };
  }, []);

  useEffect(() => {
    const position = pending.current;
    if (!ready || !position) return;
    pending.current = null;
    if (position.y > 0) return restoreWhenTallEnough(position.y);
  }, [ready]);

  return saved;
}

const RESTORE_WINDOW_MS = 2000;
const USER_SCROLL_EVENTS = ["wheel", "touchstart", "keydown", "pointerdown"] as const;

/**
 * Scrolls to `y` as soon as the page is tall enough (parts of a page can load
 * after its main data), for at most RESTORE_WINDOW_MS; any user input cancels
 * it so ClipForge never fights the user's own scrolling.
 */
function restoreWhenTallEnough(y: number): () => void {
  let frame = 0;
  const started = performance.now();
  const stop = () => {
    window.cancelAnimationFrame(frame);
    for (const name of USER_SCROLL_EVENTS) window.removeEventListener(name, stop);
  };
  const attempt = () => {
    const reachable = document.documentElement.scrollHeight - window.innerHeight;
    window.scrollTo(0, Math.min(y, Math.max(0, reachable)));
    if (reachable >= y || performance.now() - started > RESTORE_WINDOW_MS) stop();
    else frame = window.requestAnimationFrame(attempt);
  };
  for (const name of USER_SCROLL_EVENTS) window.addEventListener(name, stop, { passive: true });
  frame = window.requestAnimationFrame(attempt);
  return stop;
}
