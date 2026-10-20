import clsx from "clsx";
import type { ComponentProps } from "react";

interface SpinnerProps extends Omit<ComponentProps<"svg">, "aria-label"> {
  /** Announced to screen readers. Ignored when `decorative`. */
  label?: string;
  /** Hide it from assistive tech, for a spinner that sits next to text saying the same thing. */
  decorative?: boolean;
}

export function Spinner({
  label = "Loading",
  decorative = false,
  className,
  ...props
}: SpinnerProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={clsx("size-4 animate-spin", className)}
      {...(decorative ? { "aria-hidden": true } : { role: "status", "aria-label": label })}
      {...props}
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" className="opacity-25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
