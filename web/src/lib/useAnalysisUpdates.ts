import { useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { queryKeys } from "./queryKeys";
import { subscribeToAnalysisUpdates } from "./supabase";

/**
 * Refreshes every image list, and this image's detail. Other images' details keep their cache:
 * their analysis didn't change.
 */
function refreshAnalyzedImage(queryClient: QueryClient, imageId: number): void {
  void queryClient.invalidateQueries({
    queryKey: queryKeys.images.all,
    predicate: ({ queryKey }) => {
      const [, kind, id] = queryKey;
      return kind === "list" || (kind === "detail" && id === imageId);
    },
  });
}

/**
 * Listens for analysis status changes of the signed-in user's images, so the grid and the modal
 * learn that an analysis finished without waiting for a poll. Mount it once, with the user's id
 * (`undefined` while signed out). Returns whether the channel is live; while it isn't, the grid
 * and the modal keep polling at their normal pace (see `RealtimeLiveContext`).
 *
 * Mocks mode has no Realtime server behind it, so it never subscribes there.
 */
export function useAnalysisUpdates(userId: string | undefined): boolean {
  const queryClient = useQueryClient();
  const [live, setLive] = useState(false);

  useEffect(() => {
    if (!userId || import.meta.env.VITE_USE_MOCKS === "true") return;

    const unsubscribe = subscribeToAnalysisUpdates(
      userId,
      ({ imageId, status }) => {
        // Only a finished analysis changes what a tile or the modal shows (tags, description,
        // colors). `pending` and `processing` carry nothing new.
        if (status === "done" || status === "failed") refreshAnalyzedImage(queryClient, imageId);
      },
      setLive,
    );
    return () => {
      unsubscribe();
      setLive(false);
    };
  }, [userId, queryClient]);

  return live;
}
