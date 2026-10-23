import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { toast } from "../../components/ui/toast";
import { acceptShare, ApiError, deleteShare, listShares } from "../../lib/api";
import type { ShareList, ShareOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

/** The user's shares in both directions. Refetched on window focus, so a new request shows up. */
export function useShares() {
  return useQuery<ShareList>({
    queryKey: queryKeys.shares,
    queryFn: ({ signal }) => listShares({ signal }),
  });
}

/** Requests from other users to share their gallery with this one, waiting for an answer. */
export function pendingRequests(shares: ShareList | undefined): ShareOut[] {
  return shares?.incoming.filter((share) => share.status === "pending") ?? [];
}

/** Galleries the user accepted, whose images are in their grid. */
export function acceptedGalleries(shares: ShareList | undefined): ShareOut[] {
  return shares?.incoming.filter((share) => share.status === "accepted") ?? [];
}

function errorMessage(error: Error, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

/**
 * Accepting adds the owner's images to the grid, so every image query refreshes along with the
 * shares.
 */
export function useAcceptShare() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (share: ShareOut) => acceptShare(share.id),
    onSuccess: async (_, share) => {
      toast.success(`You can now see the images of ${share.email}.`);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.shares }),
        queryClient.invalidateQueries({ queryKey: queryKeys.images.all }),
      ]);
    },
    onError: (error) => toast.error(errorMessage(error, "Couldn't accept the request.")),
  });
}

/**
 * Ends a share from either side. `success` is the toast to show. The images refresh too, because
 * leaving a gallery takes its images out of the grid.
 */
export function useEndShare() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ share }: { share: ShareOut; success: string }) => deleteShare(share.id),
    onSuccess: async (_, { success }) => {
      toast.success(success);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.shares }),
        queryClient.invalidateQueries({ queryKey: queryKeys.images.all }),
      ]);
    },
    onError: (error) => toast.error(errorMessage(error, "Couldn't update the share.")),
  });
}
