import clsx from "clsx";
import type { ComponentProps } from "react";

/** A pulsing placeholder block. Size it with `className`, for example `aspect-square`. */
export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      aria-hidden="true"
      className={clsx("animate-pulse rounded-xl bg-zinc-200 dark:bg-zinc-800", className)}
      {...props}
    />
  );
}
