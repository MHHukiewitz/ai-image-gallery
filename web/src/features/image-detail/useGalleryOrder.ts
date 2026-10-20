import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { listImages } from "../../lib/api";
import { useGalleryQuery } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { orderOf } from "./imageNavigation";

/** The same staleness as the grid's query, so both observers agree on when pages go stale. */
const STALE_TIME_MS = 50 * 60_000;

/**
 * The grid's images in the order it shows them, read from the infinite query the grid fills. The
 * modal steps through exactly what is on screen this way.
 *
 * `enabled: false` stops this observer from fetching on its own: the grid owns the first page. It
 * still pages on when the modal asks, through `loadMore`.
 */
export function useGalleryOrder() {
  const [query] = useGalleryQuery();
  const pages = useInfiniteQuery({
    queryKey: queryKeys.images.list(query),
    queryFn: ({ pageParam, signal }) => listImages(query, pageParam, { signal }),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    staleTime: STALE_TIME_MS,
    enabled: false,
  });
  const ids = useMemo(() => orderOf(pages.data), [pages.data]);

  return {
    ids,
    hasMore: pages.hasNextPage,
    loadingMore: pages.isFetchingNextPage,
    loadMore: pages.fetchNextPage,
  };
}
