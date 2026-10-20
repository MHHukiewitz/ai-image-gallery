import type { ImagePage } from "../../lib/api";

/**
 * How often the grid refetches while an image is still being analyzed (architecture §4), or while
 * a thumbnail has no URL yet.
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
 */
export function refetchIntervalFor(data: LoadedPages | undefined): number | false {
  const waiting = data?.pages.some((page) =>
    page.items.some(
      ({ analysis, thumbnail_url }) =>
        analysis.status === "pending" || analysis.status === "processing" || thumbnail_url === "",
    ),
  );
  return waiting ? POLL_INTERVAL_MS : false;
}
