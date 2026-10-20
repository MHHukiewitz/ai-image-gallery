import { useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { Button } from "../../components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { ErrorState } from "../../components/ui/ErrorState";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import type { ImageOut } from "../../lib/api";
import { useSelectedImage } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { DeleteImageDialog } from "./DeleteImageDialog";
import { ImageDetails } from "./ImageDetails";
import { ImagePanel } from "./ImagePanel";
import { neighborsOf, orderOf } from "./imageNavigation";
import type { Step } from "./imageNavigation";
import { useGalleryOrder } from "./useGalleryOrder";
import { useArrowSteps, useSwipeSteps } from "./useImageSteps";
import { useReturnFocus } from "./useReturnFocus";
import { imageDetailQuery, isNetworkError, isNotFound, useImageDetail } from "./useImageDetail";

const NETWORK_TOAST_ID = "image-network-error";
const LOAD_MORE_TOAST_ID = "image-load-more-error";
/** A neighbor fetched this recently is reused, so stepping back and forth doesn't refetch it. */
const PRELOAD_FRESH_MS = 60_000;

/**
 * The large view of one image, open while `?image=<id>` is in the URL. Every way of closing it
 * (Esc, the corner button, the backdrop, Back) ends with the param removed. ← and → and a swipe
 * step through the grid's images in the order it shows them, replacing the modal's history entry.
 */
export function ImageModal() {
  const { imageId, open, close } = useSelectedImage();
  const returnFocus = useReturnFocus();
  const queryClient = useQueryClient();
  const order = useGalleryOrder();
  const neighbors = imageId === null ? null : neighborsOf(order.ids, imageId);
  const previousId = neighbors?.prev ?? null;
  const nextId = neighbors?.next ?? null;
  // Past the last loaded image, "next" is still on if the grid has another page to fetch.
  const canStepNext = neighbors !== null && (nextId !== null || order.hasMore);

  // The load-more below can finish after the user has moved on, so it reads these at that point.
  const latest = useRef({ imageId, open });
  useEffect(() => {
    latest.current = { imageId, open };
  });

  useEffect(() => {
    for (const id of [previousId, nextId]) {
      if (id !== null) preloadOriginal(queryClient, id);
    }
  }, [queryClient, previousId, nextId]);

  function step(direction: Step) {
    if (imageId === null || neighbors === null) return;
    const target = direction === "previous" ? previousId : nextId;
    if (target !== null) {
      open(target);
      return;
    }
    if (direction === "previous" || !order.hasMore || order.loadingMore) return;

    // At the last loaded image, fetch the next page and then move to its first image.
    const fromId = imageId;
    void order.loadMore().then((result) => {
      if (result.isError) {
        toast.error("Couldn't load more images. Please try again.", { id: LOAD_MORE_TOAST_ID });
        return;
      }
      if (latest.current.imageId !== fromId) return;
      const following = neighborsOf(orderOf(result.data), fromId)?.next ?? null;
      if (following !== null) latest.current.open(following);
    });
  }

  // "of N" counts only the images the grid has loaded, so it gets a "+" while more pages remain.
  const position =
    neighbors === null
      ? null
      : `Image ${neighbors.index + 1} of ${order.ids.length}${order.hasMore ? "+" : ""}`;

  return (
    <Dialog
      open={imageId !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent size="xl" {...returnFocus}>
        <DialogDescription className="sr-only">Image details and actions</DialogDescription>
        {imageId !== null && (
          <>
            {/* The right padding keeps the buttons clear of the corner ×. */}
            <div className="mb-4 flex items-center gap-2 pr-12 sm:pr-10">
              <Button
                variant="secondary"
                size="sm"
                aria-label="Previous image"
                className="w-11 sm:w-8"
                disabled={previousId === null}
                onClick={() => step("previous")}
              >
                <ChevronIcon direction="previous" />
              </Button>
              <p
                aria-live="polite"
                aria-atomic="true"
                className="min-w-0 flex-1 truncate text-center text-sm text-zinc-500 tabular-nums dark:text-zinc-400"
              >
                {position}
              </p>
              <Button
                variant="secondary"
                size="sm"
                aria-label="Next image"
                className="w-11 sm:w-8"
                disabled={!canStepNext || order.loadingMore}
                onClick={() => step("next")}
              >
                <ChevronIcon direction="next" />
              </Button>
            </div>
            <ImageDetail key={imageId} imageId={imageId} onStep={step} />
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Fetches an image's details, then loads its original into the browser's cache. */
function preloadOriginal(queryClient: QueryClient, imageId: number): void {
  void queryClient
    .prefetchQuery({ ...imageDetailQuery(imageId), staleTime: PRELOAD_FRESH_MS })
    .then(() => {
      const url = queryClient.getQueryData<ImageOut>(
        queryKeys.images.detail(imageId),
      )?.original_url;
      if (!url) return;
      const original = new Image();
      original.src = url;
    });
}

function ChevronIcon({ direction }: { direction: Step }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-4">
      <path
        d={direction === "previous" ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6"}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Keyed by image, so a different image starts with a fresh fetch and no leftover delete dialog.
 * Arrow keys and swipes are wired here: the delete confirmation is a second modal, and `deleting`
 * lives here, so the keys stay off while it is up.
 */
function ImageDetail({ imageId, onStep }: { imageId: number; onStep: (step: Step) => void }) {
  const { close } = useSelectedImage();
  const { data: image, error, refetch } = useImageDetail(imageId);
  const [deleting, setDeleting] = useState(false);
  useArrowSteps(onStep, !deleting);
  const swipe = useSwipeSteps(onStep);

  // Offline or unreachable: the error state shows it in place, and the toast makes sure it isn't missed.
  useEffect(() => {
    if (isNetworkError(error)) toast.error(error.message, { id: NETWORK_TOAST_ID });
  }, [error]);

  if (isNotFound(error)) {
    return (
      <>
        <DialogTitle className="sr-only">Image details</DialogTitle>
        <EmptyState
          title="Image not found"
          description="It may have been deleted."
          action={
            <Button variant="secondary" onClick={close}>
              Close
            </Button>
          }
        />
      </>
    );
  }

  if (!image) {
    return (
      <>
        <DialogTitle className="sr-only">Image details</DialogTitle>
        {error ? (
          <ErrorState
            title="Couldn't load this image"
            message={error.message}
            onRetry={() => void refetch()}
          />
        ) : (
          <LoadingDetail />
        )}
      </>
    );
  }

  return (
    <>
      <div className="flex flex-col gap-6 md:flex-row">
        <div className="md:w-3/5 md:shrink-0" {...swipe}>
          <ImagePanel image={image} />
        </div>
        <div className="min-w-0 flex-1">
          <ImageDetails image={image} onDelete={() => setDeleting(true)} />
        </div>
      </div>
      <DeleteImageDialog
        imageId={image.id}
        filename={image.filename}
        open={deleting}
        onOpenChange={setDeleting}
      />
    </>
  );
}

/** Stands in for the picture and the details until the first response arrives. */
function LoadingDetail() {
  return (
    <div role="status" aria-label="Loading image" className="flex flex-col gap-6 md:flex-row">
      <Skeleton className="aspect-square max-h-[60vh] w-full md:w-3/5 md:max-h-[75dvh]" />
      <div className="flex-1 space-y-3">
        <Skeleton className="h-5 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </div>
    </div>
  );
}
