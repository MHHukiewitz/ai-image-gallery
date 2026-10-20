import clsx from "clsx";

import { Button } from "../../components/ui/Button";
import { summarizeQueue } from "./uploadQueue";
import type { UploadItem, UploadQueueState, UploadStatus } from "./uploadQueue";

interface UploadQueueProps {
  items: UploadQueueState;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onDismiss: (id: string) => void;
  /** Opens the image the user already had, by id. */
  onView: (imageId: number) => void;
}

const DUPLICATE_MESSAGE = "Already in your gallery, moved to top";

const BAR_COLORS: Record<UploadStatus, string> = {
  checking: "bg-zinc-300 dark:bg-zinc-600",
  queued: "bg-zinc-300 dark:bg-zinc-600",
  uploading: "bg-zinc-900 dark:bg-zinc-100",
  done: "bg-emerald-600 dark:bg-emerald-400",
  duplicate: "bg-sky-600 dark:bg-sky-400",
  error: "bg-red-600 dark:bg-red-400",
  canceled: "bg-zinc-300 dark:bg-zinc-600",
};

/** The short label beside the file name. A duplicate's sentence is too long for it, so it goes below. */
function statusLabel(item: UploadItem): string {
  switch (item.status) {
    case "checking":
      return "Checking";
    case "queued":
      return "Waiting";
    case "uploading":
      return `Uploading ${item.progress}%`;
    case "done":
      return "Uploaded";
    case "duplicate":
      return "";
    case "error":
      return "Failed";
    case "canceled":
      return "Canceled";
  }
}

function UploadRow({
  item,
  onCancel,
  onRetry,
  onDismiss,
  onView,
}: { item: UploadItem } & Omit<UploadQueueProps, "items">) {
  const { id, file, progress, status, error, imageId } = item;
  const active = status === "checking" || status === "queued" || status === "uploading";
  const retryable = status === "error" || status === "canceled";

  return (
    <li
      data-status={status}
      className="flex items-center gap-3 rounded-xl border border-zinc-200 bg-white p-3 dark:border-zinc-800 dark:bg-zinc-900"
    >
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="flex items-baseline justify-between gap-3 text-sm">
          <span className="truncate font-medium" title={file.name}>
            {file.name}
          </span>
          <span
            className={clsx(
              "shrink-0 text-xs tabular-nums",
              status === "error"
                ? "text-red-600 dark:text-red-400"
                : "text-zinc-500 dark:text-zinc-400",
            )}
          >
            {statusLabel(item)}
          </span>
        </div>
        <div
          role="progressbar"
          aria-label={`Upload progress for ${file.name}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
          className="h-1.5 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"
        >
          <div
            className={clsx(
              "h-full rounded-full transition-[width] duration-200",
              BAR_COLORS[status],
            )}
            style={{ width: `${progress}%` }}
          />
        </div>
        {error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
        {status === "duplicate" && (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">{DUPLICATE_MESSAGE}</p>
        )}
      </div>
      <div className="flex shrink-0 gap-1">
        {status === "duplicate" && imageId !== undefined && (
          <Button
            variant="secondary"
            size="sm"
            aria-label={`View ${file.name} in your gallery`}
            onClick={() => onView(imageId)}
          >
            View
          </Button>
        )}
        {active && (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Cancel upload of ${file.name}`}
            onClick={() => onCancel(id)}
          >
            Cancel
          </Button>
        )}
        {retryable && (
          <Button
            variant="secondary"
            size="sm"
            aria-label={`Retry upload of ${file.name}`}
            onClick={() => onRetry(id)}
          >
            Retry
          </Button>
        )}
        {!active && (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Dismiss ${file.name}`}
            onClick={() => onDismiss(id)}
          >
            Dismiss
          </Button>
        )}
      </div>
    </li>
  );
}

/**
 * The files being uploaded, each with a progress bar and the buttons that fit its state. The
 * summary line is the live region: it changes when a file starts, finishes or fails, so a
 * screen reader hears the progress without a message for every percent.
 */
export function UploadQueue({ items, onCancel, onRetry, onDismiss, onView }: UploadQueueProps) {
  return (
    <div role="group" aria-label="Uploads" className={clsx(items.length > 0 && "space-y-2")}>
      <p
        aria-live="polite"
        aria-atomic="true"
        className="text-sm font-medium text-zinc-600 empty:hidden dark:text-zinc-300"
      >
        {summarizeQueue(items)}
      </p>
      {items.length > 0 && (
        <ul className="space-y-2">
          {items.map((item) => (
            <UploadRow
              key={item.id}
              item={item}
              onCancel={onCancel}
              onRetry={onRetry}
              onDismiss={onDismiss}
              onView={onView}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
