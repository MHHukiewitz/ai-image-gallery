import { useState } from "react";

import { Button } from "../../components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { ErrorState } from "../../components/ui/ErrorState";
import { Skeleton } from "../../components/ui/Skeleton";
import { useSelectedImage } from "../../lib/galleryQuery";
import { DeleteImageDialog } from "./DeleteImageDialog";
import { ImageDetails } from "./ImageDetails";
import { ImagePanel } from "./ImagePanel";
import { useReturnFocus } from "./useReturnFocus";
import { isNotFound, useImageDetail } from "./useImageDetail";

/**
 * The large view of one image, open while `?image=<id>` is in the URL. Every way of closing it
 * (Esc, the corner button, the backdrop, Back) ends with the param removed.
 */
export function ImageModal() {
  const { imageId, close } = useSelectedImage();
  const returnFocus = useReturnFocus();

  return (
    <Dialog
      open={imageId !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent size="xl" {...returnFocus}>
        <DialogDescription className="sr-only">Image details and actions</DialogDescription>
        {imageId !== null && <ImageDetail key={imageId} imageId={imageId} />}
      </DialogContent>
    </Dialog>
  );
}

/** Keyed by image, so a different image starts with a fresh fetch and no leftover delete dialog. */
function ImageDetail({ imageId }: { imageId: number }) {
  const { close } = useSelectedImage();
  const { data: image, error, refetch } = useImageDetail(imageId);
  const [deleting, setDeleting] = useState(false);

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
        <div className="md:w-3/5 md:shrink-0">
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
