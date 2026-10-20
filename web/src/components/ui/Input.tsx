import clsx from "clsx";
import type { ComponentProps } from "react";

interface InputProps extends ComponentProps<"input"> {
  /** Red border and `aria-invalid`. Link the message with `aria-describedby` yourself. */
  invalid?: boolean;
}

export function Input({ invalid = false, className, ...props }: InputProps) {
  return (
    <input
      aria-invalid={invalid || undefined}
      className={clsx(
        "h-10 w-full rounded-xl border bg-white px-3 text-sm text-zinc-900 placeholder:text-zinc-400",
        "focus-visible:outline-2 focus-visible:outline-offset-0 dark:bg-zinc-900 dark:text-zinc-100 dark:placeholder:text-zinc-500",
        "disabled:cursor-not-allowed disabled:opacity-50",
        invalid
          ? "border-red-500 focus-visible:outline-red-500"
          : "border-zinc-300 focus-visible:outline-zinc-900 dark:border-zinc-700 dark:focus-visible:outline-zinc-100",
        className,
      )}
      {...props}
    />
  );
}
