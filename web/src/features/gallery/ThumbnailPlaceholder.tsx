/**
 * What a tile shows while it has no usable thumbnail: no URL yet, or one that won't load. It fills
 * the tile, so the grid keeps its shape. The `<img>` beside it carries the name for screen readers.
 */
export function ThumbnailPlaceholder() {
  return (
    <span
      aria-hidden="true"
      data-testid="thumbnail-placeholder"
      className="absolute inset-0 flex items-center justify-center bg-zinc-200 text-zinc-400 dark:bg-zinc-800 dark:text-zinc-600"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="size-1/4 min-w-6"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="9" cy="9" r="1.5" />
        <path d="m21 15-4.5-4.5L6 21" />
      </svg>
    </span>
  );
}
