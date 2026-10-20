import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { Button } from "../../components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../../components/ui/Dialog";
import { toast } from "../../components/ui/toast";
import { ApiError, deleteImage } from "../../lib/api";
import { useSelectedImage } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { useReturnFocus } from "./useReturnFocus";

const FALLBACK_DELETE_ERROR = "Couldn't delete the image. Please try again.";

interface DeleteImageDialogProps {
  imageId: number;
  filename: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Confirms before deleting. On success the modal closes, the image lists refresh and the user gets
 * a toast. A failure keeps the dialog open with a toast.
 */
export function DeleteImageDialog({
  imageId,
  filename,
  open,
  onOpenChange,
}: DeleteImageDialogProps) {
  const queryClient = useQueryClient();
  const { imageId: shownImageId, close } = useSelectedImage();
  const returnFocus = useReturnFocus();

  // The delete can outlive the modal: Back closes it, or another image opens while it runs. The
  // latest values are read at success time, so the modal only closes if it still shows this image.
  const latest = useRef({ shownImageId, close });
  useEffect(() => {
    latest.current = { shownImageId, close };
  });

  const deletion = useMutation({
    mutationFn: () => deleteImage(imageId),
    onSuccess: () => {
      toast.success(`Deleted ${filename}`);
      onOpenChange(false);
      if (latest.current.shownImageId === imageId) latest.current.close();
      // Every image query is refreshed except the deleted image's own. Refetching that would ask
      // the API for an image that no longer exists.
      const deletedKey = JSON.stringify(queryKeys.images.detail(imageId));
      void queryClient.invalidateQueries({
        queryKey: queryKeys.images.all,
        predicate: (query) => JSON.stringify(query.queryKey) !== deletedKey,
      });
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : FALLBACK_DELETE_ERROR);
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!deletion.isPending) onOpenChange(next);
      }}
    >
      <DialogContent size="md" showClose={false} {...returnFocus}>
        <DialogTitle>Delete this image?</DialogTitle>
        <DialogDescription>
          {filename} is removed from your gallery, along with its tags and colors. This can&apos;t
          be undone.
        </DialogDescription>
        <div className="mt-6 flex justify-end gap-2">
          <Button
            variant="secondary"
            disabled={deletion.isPending}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button variant="danger" loading={deletion.isPending} onClick={() => deletion.mutate()}>
            Delete image
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
