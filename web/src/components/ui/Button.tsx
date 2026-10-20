import clsx from "clsx";
import type { ComponentProps } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "./focusRing";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

// Dark-mode danger uses red-600 too: white text on red-500 is below AA contrast.
const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-zinc-900 text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300",
  secondary:
    "border border-zinc-300 bg-white text-zinc-900 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:hover:bg-zinc-800",
  ghost: "text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800",
  danger: "bg-red-600 text-white hover:bg-red-700 dark:bg-red-600 dark:hover:bg-red-700",
};

// 44 px tall on phones, the WCAG 2.5.5 touch size; the compact height returns from `sm` up.
const SIZES: Record<ButtonSize, string> = {
  sm: "h-11 px-3 text-sm sm:h-8",
  md: "h-11 px-4 text-sm sm:h-10",
};

interface ButtonProps extends ComponentProps<"button"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and disables the button, keeping its label so the width doesn't jump. */
  loading?: boolean;
}

/** `type` defaults to "button" so a stray button never submits a form. Pass `type="submit"` to submit. */
export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  type = "button",
  disabled,
  className,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={clsx(
        "inline-flex shrink-0 items-center justify-center gap-2 rounded-xl font-medium motion-safe:transition-colors",
        FOCUS_RING,
        FOCUS_COLOR,
        "disabled:cursor-not-allowed disabled:opacity-50",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...props}
    >
      {loading && <Spinner decorative />}
      {children}
    </button>
  );
}
