import clsx from "clsx";
import { useState } from "react";

import type { ImageOut } from "../../lib/api";
import { ImagePlaceholder } from "./ImagePlaceholder";
import { useImageSource } from "./useImageSource";

interface ImagePanelProps {
  image: ImageOut;
}

/**
 * The picture. The thumbnail fills the frame, blurred, until the full-size file has loaded, so the
 * frame never sits empty. Each poll signs the URLs again, and a new `src` would download the
 * picture again, so the URLs from the first render are kept. Only a failed load switches to the
 * latest ones, since a signed URL expires after an hour (see `useImageSource`). A placeholder
 * covers the frame while the full-size image has no URL or won't load.
 */
export function ImagePanel({ image }: ImagePanelProps) {
  const thumbnail = useImageSource(image.thumbnail_url);
  const full = useImageSource(image.original_url ?? image.thumbnail_url);
  const [loaded, setLoaded] = useState(false);

  return (
    <div
      className="relative max-h-[60vh] w-full overflow-hidden rounded-xl bg-zinc-100 md:max-h-[75dvh] dark:bg-zinc-800"
      style={{ aspectRatio: `${image.width} / ${image.height}` }}
    >
      {/* Decoration only: the full-size image below carries the name for screen readers. */}
      <img
        src={thumbnail.src}
        alt=""
        aria-hidden="true"
        className={clsx(
          "absolute inset-0 size-full scale-110 object-cover blur-xl motion-safe:transition-opacity motion-safe:duration-500",
          loaded && "opacity-0",
        )}
      />
      <img
        src={full.src}
        alt={image.analysis.description || image.filename}
        decoding="async"
        onLoad={() => setLoaded(true)}
        onError={full.onError}
        className={clsx(
          "absolute inset-0 size-full object-contain motion-safe:transition-opacity motion-safe:duration-300",
          loaded ? "opacity-100" : "opacity-0",
        )}
      />
      {full.unavailable && <ImagePlaceholder />}
    </div>
  );
}
