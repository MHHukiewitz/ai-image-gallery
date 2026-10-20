import { useEffect, useRef, useState } from "react";

/** Start loading while the sentinel is still this far below the viewport, so scrolling never waits. */
const ROOT_MARGIN = "0px 0px 600px 0px";

/**
 * Returns a ref for a sentinel element below the list. `onReach` runs when the sentinel comes
 * within view (plus a margin). While `enabled` is false, nothing is observed.
 *
 * The observer is rebuilt whenever `enabled` flips back on: it reports the sentinel's current
 * state on `observe()`, so a sentinel that is still on screen after a page loaded (a tall
 * monitor, a short page) triggers the next page instead of waiting for a scroll that can't happen.
 */
export function useLoadMoreSentinel(onReach: () => void, enabled: boolean) {
  const [sentinel, setSentinel] = useState<HTMLElement | null>(null);
  const latestOnReach = useRef(onReach);

  useEffect(() => {
    latestOnReach.current = onReach;
  });

  useEffect(() => {
    // Without IntersectionObserver the visible "Load more" button is the only way forward.
    if (!sentinel || !enabled || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) latestOnReach.current();
      },
      { rootMargin: ROOT_MARGIN },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [sentinel, enabled]);

  return setSentinel;
}
