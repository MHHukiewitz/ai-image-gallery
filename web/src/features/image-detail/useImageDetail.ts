import { useQuery } from "@tanstack/react-query";

import { ApiError, getImage } from "../../lib/api";
import type { AnalysisStatus, ImageOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

/** How often the open modal refetches while its image is still being analyzed (architecture §4). */
export const ANALYSIS_POLL_MS = 2000;

export function isAnalyzing(status: AnalysisStatus | undefined): boolean {
  return status === "pending" || status === "processing";
}

/**
 * The modal's `refetchInterval`. Polls only while the analysis is in flight. `done` and `failed`
 * are final until the user retries, so they don't keep the modal polling.
 */
export function analysisRefetchInterval(image: ImageOut | undefined): number | false {
  return isAnalyzing(image?.analysis.status) ? ANALYSIS_POLL_MS : false;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * The image shown in the modal, with the full-size `original_url`. A 404 isn't retried: a deleted
 * image stays deleted, and the user is waiting on "Image not found".
 */
export function useImageDetail(imageId: number) {
  return useQuery({
    queryKey: queryKeys.images.detail(imageId),
    queryFn: ({ signal }) => getImage(imageId, { signal }),
    refetchInterval: (query) => analysisRefetchInterval(query.state.data),
    retry: (failureCount, error) => !isNotFound(error) && failureCount < 1,
  });
}
