import { useState } from "react";

/**
 * The URL a tile loads, and whether to show a placeholder instead.
 *
 * Every list refetch can sign a thumbnail again, so `currentUrl` changes while the file doesn't.
 * Following it would reload the thumbnail on each poll, so the tile keeps the first URL. It
 * switches to the current one only when that first URL fails to load, since a signed URL expires.
 *
 * `currentUrl` is empty when the server couldn't sign it. The tile then shows the placeholder,
 * and takes the first real URL a later poll brings. A URL that fails even after the switch also
 * ends in the placeholder, until a different URL arrives and gets one more try.
 */
export function useThumbnailSource(currentUrl: string) {
  const [pinnedUrl, setPinnedUrl] = useState(currentUrl);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  // Adjusting state while rendering is the supported way to derive it from props. React re-renders
  // right away, before anything reaches the screen.
  if (!pinnedUrl && currentUrl) {
    setPinnedUrl(currentUrl);
  } else if (failedUrl !== null && currentUrl && currentUrl !== failedUrl) {
    setPinnedUrl(currentUrl);
    setFailedUrl(null);
  }

  return {
    /** `undefined`, not "", when there is nothing to load: an empty `src` makes browsers refetch the page. */
    src: pinnedUrl || undefined,
    showPlaceholder: !pinnedUrl || failedUrl === pinnedUrl,
    onError: () => {
      if (currentUrl && currentUrl !== pinnedUrl) setPinnedUrl(currentUrl);
      else setFailedUrl(pinnedUrl);
    },
  };
}
