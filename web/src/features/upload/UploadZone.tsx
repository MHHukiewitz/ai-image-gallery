import clsx from "clsx";
import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import type { FileRejection } from "react-dropzone";

import { Button } from "../../components/ui/Button";
import { toast } from "../../components/ui/toast";
import { useSelectedImage } from "../../lib/galleryQuery";
// The extension is not optional: on a case-insensitive filesystem "./UploadQueue" would resolve
// to uploadQueue.ts, the reducer, since TypeScript tries .ts before .tsx.
import { UploadQueue } from "./UploadQueue.tsx";
import { useGalleryIsEmpty } from "./useGalleryIsEmpty";
import { useUploads } from "./useUploads";
import { ACCEPTED_TYPES, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB, rejectionMessage } from "./uploadRules";

type DropHandler = (accepted: File[], rejections: FileRejection[]) => void;

const DROPZONE_OPTIONS = {
  accept: ACCEPTED_TYPES,
  maxSize: MAX_UPLOAD_BYTES,
  multiple: true,
  // The Browse button opens the picker; the zone itself is only a drop target, so it stays out of
  // the tab order.
  noClick: true,
  noKeyboard: true,
  // The native input also makes `accept` open a phone's photo picker, which the File System
  // Access picker (desktop only) can't do.
  useFsAccessApi: false,
};

function UploadIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="size-10 text-zinc-400 dark:text-zinc-500"
    >
      <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" />
      <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
    </svg>
  );
}

/**
 * Covers the page while a file is dragged anywhere over it, so a drop doesn't have to land on the
 * bar. It is a sibling of the zone, not a child, so that one drop is never handled twice. It stays
 * mounted and is only hidden between drags: the drop's files are read asynchronously, and
 * unmounting the dropzone at the moment of the drop would discard them.
 */
function PageDropTarget({ visible, onDrop }: { visible: boolean; onDrop: DropHandler }) {
  const { getRootProps, isDragReject } = useDropzone({ ...DROPZONE_OPTIONS, onDrop });

  return (
    <div
      hidden={!visible}
      {...getRootProps({
        className:
          "fixed inset-0 z-40 flex items-center justify-center bg-white/75 p-6 backdrop-blur-sm dark:bg-zinc-950/75",
      })}
    >
      <p
        className={clsx(
          "rounded-xl border-2 border-dashed px-8 py-6 text-center text-lg font-medium",
          isDragReject
            ? "border-red-500 text-red-700 dark:text-red-400"
            : "border-zinc-900 text-zinc-900 dark:border-zinc-100 dark:text-zinc-100",
        )}
      >
        {isDragReject ? "Only JPEG and PNG images can be uploaded" : "Drop images to upload"}
      </p>
    </div>
  );
}

/**
 * Upload entry point: a large drop area while the gallery is empty, a compact bar otherwise, and
 * the queue of files in flight below it. A file dragged anywhere over the page can be dropped.
 */
export function UploadZone() {
  const { items, paused, addFiles, cancel, retry, dismiss } = useUploads();
  const galleryIsEmpty = useGalleryIsEmpty();
  const { open: openImage } = useSelectedImage();

  const handleDrop = useCallback<DropHandler>(
    (accepted, rejections) => {
      for (const rejection of rejections) toast.error(rejectionMessage(rejection));
      addFiles(accepted);
    },
    [addFiles],
  );

  const { getRootProps, getInputProps, open, isDragActive, isDragReject, isDragGlobal } =
    useDropzone({ ...DROPZONE_OPTIONS, onDrop: handleDrop });

  const highlighted = isDragActive || isDragGlobal;

  return (
    <div className="space-y-3">
      {/* One root element in both layouts, so a listener never gets re-attached mid-drag. */}
      <div
        {...getRootProps({
          className: clsx(
            "rounded-xl border-2 border-dashed motion-safe:transition-colors",
            galleryIsEmpty ? "px-6 py-16 text-center" : "px-4 py-2.5 sm:py-3",
            highlighted
              ? isDragReject
                ? "border-red-500 bg-red-50 dark:bg-red-950/30"
                : "border-zinc-900 bg-zinc-100 dark:border-zinc-100 dark:bg-zinc-800"
              : "border-zinc-300 dark:border-zinc-700",
          ),
        })}
      >
        <input {...getInputProps({ "aria-label": "Choose images to upload" })} />
        {galleryIsEmpty ? (
          <div className="mx-auto flex max-w-md flex-col items-center gap-3">
            <UploadIcon />
            <h2 className="text-lg font-semibold">Drop your photos here</h2>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              JPEG or PNG, up to {MAX_UPLOAD_MB} MB each. Drop as many as you like; we tag and
              describe them in the background.
            </p>
            <Button onClick={open}>Browse files</Button>
          </div>
        ) : (
          <div className="flex items-center justify-between gap-3">
            <p className="min-w-0 text-sm text-zinc-600 dark:text-zinc-300">
              <span className="hidden sm:inline">Drop images anywhere to upload. </span>
              JPEG or PNG, up to {MAX_UPLOAD_MB} MB each.
            </p>
            <Button size="sm" onClick={open}>
              Upload images
            </Button>
          </div>
        )}
      </div>
      <PageDropTarget visible={isDragGlobal} onDrop={handleDrop} />
      <UploadQueue
        items={items}
        paused={paused}
        onCancel={cancel}
        onRetry={retry}
        onDismiss={dismiss}
        onView={openImage}
      />
    </div>
  );
}
