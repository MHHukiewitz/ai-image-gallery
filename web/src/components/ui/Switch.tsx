import clsx from "clsx";
import type { ComponentProps } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "./focusRing";

interface SwitchProps extends Omit<ComponentProps<"button">, "onChange" | "role" | "type"> {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

/**
 * An on/off toggle: a button with the `switch` role, so assistive tech reads its state. Name it
 * with `aria-labelledby` or `aria-label`. 44 px wide everywhere, so it stays a comfortable target.
 */
export function Switch({ checked, onCheckedChange, disabled, className, ...props }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={clsx(
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full motion-safe:transition-colors",
        FOCUS_RING,
        FOCUS_COLOR,
        "disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "bg-zinc-900 dark:bg-zinc-100" : "bg-zinc-300 dark:bg-zinc-700",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={clsx(
          "inline-block size-5 rounded-full bg-white shadow motion-safe:transition-transform dark:bg-zinc-900",
          checked ? "translate-x-5.5" : "translate-x-0.5",
        )}
      />
    </button>
  );
}
