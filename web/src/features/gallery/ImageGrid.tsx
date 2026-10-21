import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { useEffect, useMemo, useRef } from "react";

import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { ErrorState } from "../../components/ui/ErrorState";
import { Skeleton } from "../../components/ui/Skeleton";
import { Spinner } from "../../components/ui/Spinner";
import { toast } from "../../components/ui/toast";
import { ApiError, listImages, PAGE_SIZE } from "../../lib/api";
import type { ImageOut } from "../../lib/api";
import { useGalleryQuery } from "../../lib/galleryQuery";
import type { GalleryQuery } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { useRealtimeLive } from "../../lib/realtimeStatus";
import { ImageCard } from "./ImageCard";
import { refetchIntervalFor } from "./polling";
import { useAnalysisAnnouncement } from "./useAnalysisAnnouncement";
import { useDelayedFlag } from "./useDelayedFlag";
import { useLoadMoreSentinel } from "./useLoadMoreSentinel";

/** Signed thumbnail URLs last an hour; refetching earlier would hand out new ones for no reason. */
const STALE_TIME_MS = 50 * 60_000;
const NEXT_PAGE_SKELETONS = 4;
/** A response faster than this shouldn't flash the "updating" dim and spinner. */
const UPDATING_FEEDBACK_DELAY_MS = 200;

const GRID_CLASSES =
  "grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6";

/** One toast slot, so a poll that keeps failing replaces its toast instead of stacking them. */
const NETWORK_TOAST_ID = "gallery-network-error";

function isNetworkError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === "network_error";
}

function SkeletonTiles({ count }: { count: number }) {
  return Array.from({ length: count }, (_, index) => (
    <li key={`skeleton-${index}`} aria-hidden="true">
      <Skeleton className="aspect-square" />
    </li>
  ));
}

/** Pages are fetched by offset, so an upload between two fetches can repeat an image. */
function uniqueById(images: ImageOut[]): ImageOut[] {
  const seen = new Set<number>();
  return images.filter((image) => !seen.has(image.id) && seen.add(image.id));
}

function emptyMessage(query: GalleryQuery): { title: string; description: string } {
  switch (query.kind) {
    case "text":
      return {
        title: `No matches for “${query.q}”`,
        description: "Try a different word, or a shorter search.",
      };
    case "similar":
      return {
        title: `No matches similar to image ${query.imageId}`,
        description: "Nothing else looks like it yet.",
      };
    case "color":
      return {
        title: `No matches for ${query.hex}`,
        description: "Try a color that is closer to one in your images.",
      };
    case "all":
      return {
        title: "Upload your first image",
        description:
          "Drop a JPEG or PNG in the upload area above. A few seconds later it gets tags and a description.",
      };
  }
}

/** The gallery: every image for the URL's query, in pages that load as you scroll. */
export function ImageGrid() {
  const [query, setQuery] = useGalleryQuery();
  const realtimeLive = useRealtimeLive();

  const {
    data,
    error,
    isPending,
    isError,
    isPlaceholderData,
    isFetching,
    isFetchingNextPage,
    isFetchNextPageError,
    hasNextPage,
    fetchNextPage,
    refetch,
  } = useInfiniteQuery({
    queryKey: queryKeys.images.list(query),
    queryFn: ({ pageParam, signal }) => listImages(query, pageParam, { signal }),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    // Keeps the old results on screen while a new query loads, instead of flashing skeletons.
    placeholderData: keepPreviousData,
    staleTime: STALE_TIME_MS,
    refetchInterval: (activeQuery) => refetchIntervalFor(activeQuery.state.data, realtimeLive),
  });

  const images = useMemo(() => uniqueById(data?.pages.flatMap((page) => page.items) ?? []), [data]);
  const announcement = useAnalysisAnnouncement(images);
  const showMatch = query.kind === "similar" || query.kind === "color";

  // A lost connection shows as a toast too, not only in the error state: a background poll can
  // fail while the grid still shows older results.
  useEffect(() => {
    if (isNetworkError(error)) toast.error(error.message, { id: NETWORK_TOAST_ID });
  }, [error]);

  // A new search starts at the top; otherwise it would open wherever the old results were scrolled.
  const shownQuery = useRef(query);
  useEffect(() => {
    if (shownQuery.current === query) return;
    shownQuery.current = query;
    window.scrollTo({ top: 0 });
  }, [query]);

  // The placeholder rows belong to the previous query, so they can't ask for a next page.
  const canLoadMore = hasNextPage && !isPlaceholderData;
  const sentinelRef = useLoadMoreSentinel(
    () => void fetchNextPage(),
    canLoadMore && !isFetching && !isFetchNextPageError,
  );
  const updating = useDelayedFlag(isPlaceholderData, UPDATING_FEEDBACK_DELAY_MS);

  if (isError && !data) {
    return (
      <ErrorState
        title="Couldn't load your images"
        message={error.message}
        onRetry={() => void refetch()}
      />
    );
  }

  // With no old results to hold on to, the skeletons are the best placeholder for a new query.
  if (isPending || (isPlaceholderData && images.length === 0)) {
    return (
      <div role="status" aria-label="Loading images">
        <ul className={GRID_CLASSES} aria-hidden="true">
          <SkeletonTiles count={PAGE_SIZE} />
        </ul>
      </div>
    );
  }

  if (images.length === 0) {
    const { title, description } = emptyMessage(query);
    return (
      <EmptyState
        title={title}
        description={description}
        action={
          query.kind === "all" ? undefined : (
            <Button variant="secondary" onClick={() => setQuery({ kind: "all" })}>
              Show all images
            </Button>
          )
        }
      />
    );
  }

  return (
    <section aria-label="Images" aria-busy={isPlaceholderData || isFetchingNextPage}>
      {/* Read aloud when an analysis finishes. Hidden visually: the card and modal already show it. */}
      <p aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </p>

      {/* Sits under the sticky header: two rows on a phone (117 px), one row from `sm` (64 px). */}
      {updating && (
        <div className="pointer-events-none sticky top-[7.5rem] z-10 flex h-0 justify-center sm:top-20">
          <div
            role="status"
            className="mt-2 flex h-fit items-center gap-2 rounded-full bg-white px-3 py-1.5 text-sm shadow-md dark:bg-zinc-800"
          >
            <Spinner decorative />
            Updating results…
          </div>
        </div>
      )}

      <ul
        className={clsx(
          GRID_CLASSES,
          "motion-safe:transition-opacity motion-safe:duration-200",
          updating && "opacity-60",
        )}
      >
        {images.map((image) => (
          <li key={image.id}>
            <ImageCard image={image} showMatch={showMatch} />
          </li>
        ))}
        {isFetchingNextPage && <SkeletonTiles count={NEXT_PAGE_SKELETONS} />}
      </ul>

      {canLoadMore && (
        <div ref={sentinelRef} className="flex flex-col items-center gap-2 py-8">
          {isFetchNextPageError && (
            <p role="alert" className="text-sm text-zinc-500 dark:text-zinc-400">
              Couldn&apos;t load more images.
            </p>
          )}
          <Button
            variant="secondary"
            loading={isFetchingNextPage}
            onClick={() => void fetchNextPage()}
          >
            {isFetchNextPageError ? "Try again" : "Load more"}
          </Button>
        </div>
      )}
    </section>
  );
}
