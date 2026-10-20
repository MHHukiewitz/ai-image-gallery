import clsx from "clsx";
import type { ComponentProps } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "./focusRing";

interface InputProps extends ComponentProps<"input"> {
  /** Red border and `aria-invalid`. Link the message with `aria-describedby` yourself. */
  invalid?: boolean;
}

// Placeholders are text, so they meet AA on the field: zinc-500 on white, zinc-400 on zinc-900.
export function Input({ invalid = false, className, ...props }: InputProps) {
  return (
    <input
      aria-invalid={invalid || undefined}
      className={clsx(
        "h-11 w-full rounded-xl border bg-white px-3 text-sm text-zinc-900 placeholder:text-zinc-500 sm:h-10",
        "dark:bg-zinc-900 dark:text-zinc-100 dark:placeholder:text-zinc-400",
        FOCUS_RING,
        "disabled:cursor-not-allowed disabled:opacity-50",
        invalid
          ? "border-red-500 focus-visible:ring-red-500"
          : clsx("border-zinc-300 dark:border-zinc-700", FOCUS_COLOR),
        className,
      )}
      {...props}
    />
  );
}
