/**
 * What the picture frame shows while the full-size image has no usable URL: the server couldn't
 * sign it, or the signed link won't load. It fills the frame, so the modal keeps its layout.
 */
export function ImagePlaceholder() {
  return (
    <div
      role="img"
      aria-label="Image unavailable"
      data-testid="image-placeholder"
      className="absolute inset-0 flex items-center justify-center bg-zinc-200 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-600"
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="size-1/6 min-w-8"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="9" cy="9" r="1.5" />
        <path d="m21 15-4.5-4.5L6 21" />
      </svg>
    </div>
  );
}
