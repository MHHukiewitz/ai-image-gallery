import type { ImagePage } from "../../lib/api";
import { LIVE_POLL_INTERVAL_MS } from "../../lib/realtimeStatus";

/**
 * How often the grid refetches while an image is still being analyzed (architecture §4), or while
 * a thumbnail has no URL yet. It is also the fallback pace when the Realtime channel is down.
 */
export const POLL_INTERVAL_MS = 3000;

/** The slice of an infinite query's data that polling looks at. */
interface LoadedPages {
  pages: ImagePage[];
}

/**
 * The grid's `refetchInterval`. Polls only while a loaded image is `pending` or `processing`, or
 * has an empty `thumbnail_url` (the server couldn't sign it, and a later poll may), so an idle
 * gallery makes no requests. `failed` is final until the user retries, so it doesn't count.
 *
 * While the Realtime channel is live, a pending analysis announces itself, so polling only backs
 * it up and slows to `LIVE_POLL_INTERVAL_MS`. An empty URL is not announced by anyone, so it
 * keeps the fast pace.
 */
export function refetchIntervalFor(
  data: LoadedPages | undefined,
  realtimeLive = false,
): number | false {
  const items = data?.pages.flatMap((page) => page.items) ?? [];
  if (items.some(({ thumbnail_url }) => thumbnail_url === "")) return POLL_INTERVAL_MS;

  const analyzing = items.some(
    ({ analysis }) => analysis.status === "pending" || analysis.status === "processing",
  );
  if (!analyzing) return false;
  return realtimeLive ? LIVE_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
}
