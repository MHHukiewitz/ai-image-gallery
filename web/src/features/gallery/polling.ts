import type { ImagePage } from "../../lib/api";

/** How often the grid refetches while an image is still being analyzed (architecture §4). */
export const POLL_INTERVAL_MS = 2000;

/** The slice of an infinite query's data that polling looks at. */
interface LoadedPages {
  pages: ImagePage[];
}

/**
 * The grid's `refetchInterval`. Polls only while a loaded image is `pending` or `processing`, so
 * an idle gallery makes no requests. `failed` is final until the user retries, so it doesn't count.
 */
export function refetchIntervalFor(data: LoadedPages | undefined): number | false {
  const analyzing = data?.pages.some((page) =>
    page.items.some(
      ({ analysis }) => analysis.status === "pending" || analysis.status === "processing",
    ),
  );
  return analyzing ? POLL_INTERVAL_MS : false;
}
