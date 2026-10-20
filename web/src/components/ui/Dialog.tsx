import * as RadixDialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import type { ComponentProps } from "react";

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
      <RadixDialog.Overlay className="fixed inset-0 z-40 bg-zinc-950/60" />
      <RadixDialog.Content
        className={clsx(
          "fixed top-1/2 left-1/2 z-50 max-h-[90dvh] w-[calc(100%-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto",
          "rounded-xl bg-white p-6 text-zinc-900 shadow-xl dark:bg-zinc-900 dark:text-zinc-100",
          SIZES[size],
          className,
        )}
        {...props}
      >
        {children}
        {showClose && (
          <RadixDialog.Close
            aria-label="Close"
            className="absolute top-3 right-3 inline-flex size-8 items-center justify-center rounded-xl text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
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
