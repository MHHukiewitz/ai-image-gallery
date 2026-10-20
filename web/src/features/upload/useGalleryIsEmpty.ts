import { useQueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { isGalleryEmpty } from "./galleryCache";

/**
 * True once the unfiltered grid has loaded and is empty. Reads the grid's cache entry instead of
 * running its own query, since the grid owns that request and its paging.
 */
export function useGalleryIsEmpty(): boolean {
  const queryClient = useQueryClient();
  return useSyncExternalStore(
    (onChange) => queryClient.getQueryCache().subscribe(onChange),
    () => isGalleryEmpty(queryClient),
  );
}
