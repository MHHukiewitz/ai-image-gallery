import * as RadixDialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import type { ComponentProps } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "./focusRing";

/** Radix Dialog with our look. Radix handles focus trapping, Esc, scroll lock and ARIA. */
export const Dialog = RadixDialog.Root;
export const DialogTrigger = RadixDialog.Trigger;
export const DialogClose = RadixDialog.Close;

const SIZES = {
  md: "max-w-md",
  lg: "max-w-3xl",
  xl: "max-w-6xl",
} as const;

interface DialogContentProps extends ComponentProps<typeof RadixDialog.Content> {
  size?: keyof typeof SIZES;
  /** The corner × button. Turn it off only when the content has its own close control. */
  showClose?: boolean;
}

/** Always render a `DialogTitle` inside, with `className="sr-only"` if it shouldn't show. */
export function DialogContent({
  size = "md",
  showClose = true,
  className,
  children,
  ...props
}: DialogContentProps) {
  return (
    <RadixDialog.Portal>
      {/* `starting:` is the enter state, so the dialog fades and grows in on mount. Radix unmounts
          on close, so there is no exit to animate. */}
      <RadixDialog.Overlay className="fixed inset-0 z-40 bg-zinc-950/60 motion-safe:transition-opacity motion-safe:duration-200 starting:opacity-0" />
      <RadixDialog.Content
        className={clsx(
          "fixed top-1/2 left-1/2 z-50 max-h-[90dvh] w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto",
          "rounded-xl bg-white p-4 text-zinc-900 shadow-xl sm:p-6 dark:bg-zinc-900 dark:text-zinc-100",
          "motion-safe:transition motion-safe:duration-200 starting:scale-95 starting:opacity-0",
          SIZES[size],
          className,
        )}
        {...props}
      >
        {children}
        {showClose && (
          // The white backing keeps the × visible when it sits over a picture, as in the image modal.
          <RadixDialog.Close
            aria-label="Close"
            className={clsx(
              "absolute top-2 right-2 inline-flex size-11 items-center justify-center rounded-xl bg-white/90 text-zinc-600 shadow-sm backdrop-blur-sm hover:bg-zinc-100 hover:text-zinc-900 sm:top-3 sm:right-3 sm:size-8 dark:bg-zinc-900/90 dark:text-zinc-300 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
              FOCUS_RING,
              FOCUS_COLOR,
            )}
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-4">
              <path
                d="M6 6l12 12M18 6L6 18"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </RadixDialog.Close>
        )}
      </RadixDialog.Content>
    </RadixDialog.Portal>
  );
}

export function DialogTitle({ className, ...props }: ComponentProps<typeof RadixDialog.Title>) {
  return <RadixDialog.Title className={clsx("text-lg font-semibold", className)} {...props} />;
}

export function DialogDescription({
  className,
  ...props
}: ComponentProps<typeof RadixDialog.Description>) {
  return (
    <RadixDialog.Description
      className={clsx("mt-1 text-sm text-zinc-500 dark:text-zinc-400", className)}
      {...props}
    />
  );
}
