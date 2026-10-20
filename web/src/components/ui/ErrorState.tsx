import clsx from "clsx";

import { Button } from "./Button";

interface ErrorStateProps {
  message: string;
  title?: string;
  /** Shows a button that calls this. Without it there is no button. */
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}

export function ErrorState({
  message,
  title,
  onRetry,
  retryLabel = "Try again",
  className,
}: ErrorStateProps) {
  return (
    <div
      role="alert"
      className={clsx(
        "flex flex-col items-center gap-3 px-6 py-16 text-center text-sm text-zinc-500 dark:text-zinc-400",
        className,
      )}
    >
      {title && <h2 className="text-base font-medium text-zinc-900 dark:text-zinc-100">{title}</h2>}
      <p className="max-w-sm">{message}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          {retryLabel}
        </Button>
      )}
    </div>
  );
}
