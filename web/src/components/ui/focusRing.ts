/**
 * The keyboard focus ring every control uses, so focus looks the same from the header to a modal.
 * The offset matches the page background in each mode, so the gap doesn't read as a box.
 */
export const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-zinc-50 dark:focus-visible:ring-offset-zinc-950";

/** The ring's color. Dark enough on the page and on cards, and light in dark mode. */
export const FOCUS_COLOR = "focus-visible:ring-zinc-900 dark:focus-visible:ring-zinc-100";
