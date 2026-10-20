import { useRef } from "react";

/**
 * Focus handlers for a `DialogContent`, so focus goes back to whatever opened the dialog. Radix does
 * this only for dialogs opened by a `DialogTrigger`. The modal and the delete confirmation open from
 * state or the URL, so Radix would drop focus on the page body. Spread the result onto the content.
 */
export function useReturnFocus() {
  const opener = useRef<HTMLElement | null>(null);

  return {
    onOpenAutoFocus: () => {
      // Runs before Radix moves focus into the dialog, so this is still the element that opened it.
      opener.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
    },
    onCloseAutoFocus: (event: Event) => {
      // Radix would focus its trigger, which doesn't exist here, so take over.
      event.preventDefault();
      opener.current?.focus();
    },
  };
}
