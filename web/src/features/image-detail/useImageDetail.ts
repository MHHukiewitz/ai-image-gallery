import { queryOptions, useQuery } from "@tanstack/react-query";

import { ApiError, getImage } from "../../lib/api";
import type { AnalysisStatus, ImageOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { LIVE_POLL_INTERVAL_MS, useRealtimeLive } from "../../lib/realtimeStatus";

/**
 * How often the open modal refetches while its image is still being analyzed (architecture §4).
 * It is also the fallback pace when the Realtime channel is down.
 */
export const ANALYSIS_POLL_MS = 2000;

export function isAnalyzing(status: AnalysisStatus | undefined): boolean {
  return status === "pending" || status === "processing";
}

/**
 * The modal's `refetchInterval`. Polls while the analysis is in flight, and while a URL came back
 * empty (the server couldn't sign it, and a later poll may). `done` and `failed` are final until
 * the user retries, so they don't keep the modal polling.
 *
 * While the Realtime channel is live, an analysis announces itself, so polling only backs it up
 * and slows to `LIVE_POLL_INTERVAL_MS`. An empty URL is not announced by anyone, so it keeps the
 * fast pace.
 */
export function analysisRefetchInterval(
  image: ImageOut | undefined,
  realtimeLive = false,
): number | false {
  const missingUrl = image?.thumbnail_url === "" || image?.original_url === "";
  if (missingUrl) return ANALYSIS_POLL_MS;
  if (!isAnalyzing(image?.analysis.status)) return false;
  return realtimeLive ? LIVE_POLL_INTERVAL_MS : ANALYSIS_POLL_MS;
}

export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/** The request never got an answer: offline, DNS, or the server is down. Status 0 in `ApiError`. */
export function isNetworkError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === "network_error";
}

/**
 * The query for one image, with the full-size `original_url`. A 404 isn't retried: a deleted image
 * stays deleted, and the user is waiting on "Image not found". Also used to preload the neighbors,
 * so a preloaded image sits in the cache under the key the modal reads.
 */
export function imageDetailQuery(imageId: number) {
  return queryOptions({
    queryKey: queryKeys.images.detail(imageId),
    queryFn: ({ signal }) => getImage(imageId, { signal }),
    retry: (failureCount: number, error: Error) => !isNotFound(error) && failureCount < 1,
  });
}

/** The image shown in the modal. Polls while its analysis runs (see `analysisRefetchInterval`). */
export function useImageDetail(imageId: number) {
  const realtimeLive = useRealtimeLive();
  return useQuery({
    ...imageDetailQuery(imageId),
    refetchInterval: (query) => analysisRefetchInterval(query.state.data, realtimeLive),
  });
}
