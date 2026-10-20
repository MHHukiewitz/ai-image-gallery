import clsx from "clsx";
import { useState } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { Spinner } from "../../components/ui/Spinner";
import type { ImageOut } from "../../lib/api";
import { useSelectedImage } from "../../lib/galleryQuery";
import { ThumbnailPlaceholder } from "./ThumbnailPlaceholder";
import { useThumbnailSource } from "./useThumbnailSource";

const TAGS_ON_HOVER = 3;

interface ImageCardProps {
  image: ImageOut;
  /**
   * Shows the score as a match percentage. Only similarity and color results have scores that mean
   * a percentage; a text-search rank doesn't, so the grid leaves this off for text.
   */
  showMatch?: boolean;
}

/** One square tile in the grid. Opens the image's modal on click. */
export function ImageCard({ image, showMatch = false }: ImageCardProps) {
  const { open } = useSelectedImage();
  const { status, tags, description } = image.analysis;
  const analyzing = status === "pending" || status === "processing";
  const matchPercent =
    showMatch && typeof image.score === "number" ? Math.round(image.score * 100) : null;

  // Keeps the first URL, and shows a placeholder while there is none (see the hook).
  const { src, showPlaceholder, onError } = useThumbnailSource(image.thumbnail_url);
  const [loaded, setLoaded] = useState(false);

  return (
    <button
      type="button"
      onClick={() => open(image.id)}
      className={clsx(
        "group relative block aspect-square w-full overflow-hidden rounded-xl bg-zinc-200 text-left dark:bg-zinc-800",
        FOCUS_RING,
        FOCUS_COLOR,
      )}
    >
      {showPlaceholder && <ThumbnailPlaceholder />}
      <img
        src={src}
        alt={description || image.filename}
        loading="lazy"
        decoding="async"
        onLoad={() => setLoaded(true)}
        onError={onError}
        className={clsx(
          "absolute inset-0 size-full object-cover motion-safe:transition-[opacity,transform] motion-safe:duration-300",
          loaded ? "opacity-100" : "opacity-0",
          "motion-safe:group-hover:scale-105 motion-safe:group-focus-visible:scale-105",
        )}
      />

      {analyzing && (
        <>
          <span
            aria-hidden="true"
            className="absolute inset-0 animate-pulse bg-linear-to-br from-white/10 via-white/50 to-white/10 dark:from-white/5 dark:via-white/20 dark:to-white/5"
          />
          <span className="absolute inset-0 flex items-center justify-center">
            <span className="flex items-center gap-1.5 rounded-full bg-zinc-900/75 px-3 py-1 text-xs font-medium text-white backdrop-blur-sm">
              <Spinner decorative className="size-3" />
              Analyzing…
            </span>
          </span>
        </>
      )}

      {status === "failed" && (
        <span className="absolute bottom-2 left-2 rounded-full bg-red-600 px-2.5 py-0.5 text-xs font-medium text-white">
          Analysis failed
        </span>
      )}

      {status === "done" && tags.length > 0 && (
        // The alt text already names the image, so the tags are decoration for sighted users.
        <span
          aria-hidden="true"
          className={clsx(
            "absolute inset-x-0 bottom-0 flex max-h-1/2 flex-wrap gap-1 overflow-hidden bg-linear-to-t from-black/75 via-black/40 to-transparent p-2 pt-8",
            "opacity-0 motion-safe:transition-opacity motion-safe:duration-200 group-hover:opacity-100 group-focus-visible:opacity-100",
          )}
        >
          {tags.slice(0, TAGS_ON_HOVER).map((tag) => (
            <span
              key={tag}
              className="rounded-full bg-white/20 px-2 py-0.5 text-xs text-white backdrop-blur-sm"
            >
              {tag}
            </span>
          ))}
        </span>
      )}

      {/* Last, so it paints over the overlays. The role makes the label the only text read aloud. */}
      {matchPercent !== null && (
        <span
          role="img"
          aria-label={`${matchPercent}% match`}
          className="absolute top-2 left-2 rounded-full bg-zinc-900/75 px-2.5 py-0.5 text-xs font-medium text-white tabular-nums backdrop-blur-sm"
        >
          {matchPercent}%
        </span>
      )}
    </button>
  );
}
