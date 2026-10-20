import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";

import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { getImage } from "../../lib/api";
import type { GalleryQuery } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";

/** The modes that have no text to show in the input, so they get a chip instead. */
export type FilterQuery = Extract<GalleryQuery, { kind: "similar" | "color" }>;

/** Signed thumbnail URLs last an hour; refetching earlier would hand out new ones for no reason. */
const STALE_TIME_MS = 50 * 60_000;

/** The thumbnail of the image a similarity search started from. */
function SimilarThumbnail({ imageId }: { imageId: number }) {
  // The same key as the modal's detail query, so an image opened there is already cached.
  const { data: image } = useQuery({
    queryKey: queryKeys.images.detail(imageId),
    queryFn: ({ signal }) => getImage(imageId, { signal }),
    staleTime: STALE_TIME_MS,
  });

  if (!image) {
    // Still loading, or the image is gone; the name keeps the chip readable either way.
    return (
      <span className="size-5 shrink-0 rounded bg-zinc-200 dark:bg-zinc-700">
        <span className="sr-only">image {imageId}</span>
      </span>
    );
  }
  return (
    <img
      src={image.thumbnail_url}
      alt={image.filename}
      className="size-5 shrink-0 rounded object-cover"
    />
  );
}

interface ActiveFilterChipProps {
  query: FilterQuery;
  /** Called by the ×. The parent decides what resets. */
  onClear: () => void;
}

/** Says which similar-image or color filter is applied, with a × to go back to every image. */
export function ActiveFilterChip({ query, onClear }: ActiveFilterChipProps) {
  const isSimilar = query.kind === "similar";

  return (
    <div
      role="group"
      aria-label="Active filter"
      className="pointer-events-auto inline-flex h-11 max-w-full items-center gap-2 rounded-full border border-zinc-200 bg-white pr-0 pl-3 text-sm shadow-md sm:h-7 sm:pr-0.5 dark:border-zinc-700 dark:bg-zinc-900"
    >
      {isSimilar ? (
        <>
          <span className="text-zinc-600 dark:text-zinc-400">Similar to</span>
          <SimilarThumbnail imageId={query.imageId} />
        </>
      ) : (
        <>
          <span className="text-zinc-600 dark:text-zinc-400">Color</span>
          <span
            aria-hidden="true"
            data-testid="color-swatch"
            style={{ backgroundColor: query.hex }}
            className="size-4 shrink-0 rounded-full border border-black/15 dark:border-white/25"
          />
          <span className="font-mono text-xs">{query.hex}</span>
        </>
      )}
      <button
        type="button"
        onClick={onClear}
        aria-label={isSimilar ? "Clear similar images filter" : "Clear color filter"}
        className={clsx(
          "grid size-11 shrink-0 place-items-center rounded-full text-zinc-500 motion-safe:transition-colors hover:bg-zinc-100 hover:text-zinc-900 sm:size-6 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
          FOCUS_RING,
          FOCUS_COLOR,
        )}
      >
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-4">
          <path
            d="M6 6l12 12M18 6L6 18"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
      </button>
    </div>
  );
}
