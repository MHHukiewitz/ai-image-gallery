import clsx from "clsx";
import { useState } from "react";
import type { ComponentProps } from "react";

import { Input } from "../../components/ui/Input";

/** A password field with a Show/Hide button. The button sits beside the input, not inside its label. */
export function PasswordInput({
  id,
  className,
  ...props
}: Omit<ComponentProps<typeof Input>, "type">) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      <Input
        id={id}
        type={visible ? "text" : "password"}
        className={clsx("pr-16", className)}
        {...props}
      />
      <button
        type="button"
        aria-controls={id}
        aria-label={visible ? "Hide password" : "Show password"}
        onClick={() => setVisible((current) => !current)}
        className="absolute inset-y-0 right-0 flex items-center rounded-r-xl px-3 text-sm font-medium text-zinc-600 hover:text-zinc-900 focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100 dark:focus-visible:outline-zinc-100"
      >
        {visible ? "Hide" : "Show"}
      </button>
    </div>
  );
}
