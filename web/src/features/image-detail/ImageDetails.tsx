import { useMutation, useQueryClient } from "@tanstack/react-query";

import clsx from "clsx";

import { Button } from "../../components/ui/Button";
import { DialogTitle } from "../../components/ui/Dialog";
import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { Skeleton } from "../../components/ui/Skeleton";
import { Spinner } from "../../components/ui/Spinner";
import { toast } from "../../components/ui/toast";
import { ApiError, reanalyzeImage } from "../../lib/api";
import type { ImageOut } from "../../lib/api";
import { useGalleryQuery, useSelectedImage } from "../../lib/galleryQuery";
import type { GalleryQuery } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { formatUploadedAt } from "./formatUploadedAt";
import { isAnalyzing } from "./useImageDetail";

const FALLBACK_RETRY_ERROR = "Couldn't restart the analysis. Please try again.";

interface ImageDetailsProps {
  image: ImageOut;
  onDelete: () => void;
}

/**
 * The text side of the modal: the name and upload time, the analysis, the tags and colors, and
 * the actions. Tags, swatches and "Find similar" set the grid's query and close the modal.
 */
export function ImageDetails({ image, onDelete }: ImageDetailsProps) {
  const { close } = useSelectedImage();
  const [, setQuery] = useGalleryQuery();
  const { status, tags, description, colors, error } = image.analysis;

  // `replace` reuses the history entry that opened the modal. A push would leave Back reopening
  // the modal over the filtered grid.
  const filterBy = (query: GalleryQuery) => {
    setQuery(query, { replace: true });
    close();
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="space-y-1 pr-10">
        <DialogTitle className="text-lg font-semibold break-all">{image.filename}</DialogTitle>
        <UploadedLine uploadedAt={image.uploaded_at} />
      </div>

      {status === "failed" && <FailedAnalysis imageId={image.id} message={error} />}
      {isAnalyzing(status) && <Analyzing />}

      {description && <p className="text-sm text-zinc-700 dark:text-zinc-300">{description}</p>}

      {tags.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-xs font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Tags
          </h3>
          <ul className="flex flex-wrap gap-2">
            {tags.map((tag) => (
              <li key={tag}>
                <button
                  type="button"
                  onClick={() => filterBy({ kind: "text", q: tag })}
                  className={clsx(
                    // 44 px tall on phones; the compact pill from `sm` up.
                    "inline-flex min-h-11 items-center rounded-full bg-zinc-100 px-3 text-sm text-zinc-700 hover:bg-zinc-200 sm:min-h-7 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700",
                    FOCUS_RING,
                    FOCUS_COLOR,
                  )}
                >
                  {tag}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {colors.length > 0 && (
        <section className="space-y-2">
          <h3 className="text-xs font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400">
            Colors
          </h3>
          <ul className="flex flex-wrap gap-2">
            {colors.map((hex) => (
              <li key={hex}>
                <button
                  type="button"
                  aria-label={`Filter by color ${hex}`}
                  onClick={() => filterBy({ kind: "color", hex })}
                  className={clsx(
                    "flex min-h-11 items-center gap-2 rounded-full bg-zinc-100 pr-3 pl-1 font-mono text-sm text-zinc-700 hover:bg-zinc-200 sm:min-h-7 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700",
                    FOCUS_RING,
                    FOCUS_COLOR,
                  )}
                >
                  <span
                    aria-hidden="true"
                    className="size-5 rounded-full ring-1 ring-black/10 ring-inset"
                    style={{ backgroundColor: hex }}
                  />
                  {hex}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="flex flex-wrap gap-2 pt-2">
        <Button
          variant="secondary"
          disabled={status !== "done"}
          title={status === "done" ? undefined : "Available once the analysis is done"}
          onClick={() => filterBy({ kind: "similar", imageId: image.id })}
        >
          Find similar
        </Button>
        <Button variant="danger-secondary" onClick={onDelete}>
          Delete
        </Button>
      </div>
    </div>
  );
}

function UploadedLine({ uploadedAt }: { uploadedAt: string }) {
  const { absolute, relative } = formatUploadedAt(uploadedAt);
  return (
    <p className="text-sm text-zinc-500 dark:text-zinc-400">
      Uploaded <time dateTime={uploadedAt}>{absolute}</time> · {relative}
    </p>
  );
}

function Analyzing() {
  return (
    <div role="status" className="space-y-2">
      <p className="flex items-center gap-2 text-sm text-zinc-500 dark:text-zinc-400">
        <Spinner decorative className="size-3.5" />
        Analyzing…
      </p>
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-5/6" />
      <Skeleton className="h-3 w-2/3" />
    </div>
  );
}

function FailedAnalysis({ imageId, message }: { imageId: number; message: string | null }) {
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () => reanalyzeImage(imageId),
    // Refreshing every image query also refetches this one, which then polls again while the
    // retry runs. The mutation stays pending until that refresh is done.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.images.all }),
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : FALLBACK_RETRY_ERROR);
    },
  });

  return (
    <div
      role="alert"
      className="space-y-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-100"
    >
      <p className="font-medium">Analysis failed</p>
      {message && <p>{message}</p>}
      <Button
        variant="secondary"
        size="sm"
        loading={retry.isPending}
        onClick={() => retry.mutate()}
      >
        Retry analysis
      </Button>
    </div>
  );
}
