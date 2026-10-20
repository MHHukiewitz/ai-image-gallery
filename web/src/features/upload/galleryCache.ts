import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import type { ImageOut, ImagePage } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

const ALL_IMAGES_KEY = queryKeys.images.list({ kind: "all" });

type GalleryPages = InfiniteData<ImagePage, number>;

/** The grid owns this cache entry, so check its shape instead of trusting it. */
function isGalleryPages(data: unknown): data is GalleryPages {
  return (
    typeof data === "object" &&
    data !== null &&
    "pages" in data &&
    Array.isArray(data.pages) &&
    data.pages.length > 0 &&
    data.pages.every(
      (page: unknown) =>
        typeof page === "object" && page !== null && "items" in page && Array.isArray(page.items),
    )
  );
}

/**
 * Puts a just-uploaded image at the top of the unfiltered grid, so its tile shows up before the
 * refetch lands. Does nothing when that list isn't cached (the user is on a search view): there is
 * no grid to update, and the invalidation that follows covers it.
 */
export function prependToGallery(queryClient: QueryClient, image: ImageOut): void {
  queryClient.setQueryData<unknown>(ALL_IMAGES_KEY, (current: unknown) => {
    if (!isGalleryPages(current)) return current;
    const alreadyThere = current.pages.some((page) => page.items.some(({ id }) => id === image.id));
    if (alreadyThere) return current;
    const [first, ...rest] = current.pages;
    return { ...current, pages: [{ ...first, items: [image, ...first.items] }, ...rest] };
  });
}

/** True once the unfiltered grid has loaded and has nothing in it: the first-upload state. */
export function isGalleryEmpty(queryClient: QueryClient): boolean {
  const data = queryClient.getQueryData<unknown>(ALL_IMAGES_KEY);
  return isGalleryPages(data) && data.pages.every((page) => page.items.length === 0);
}
