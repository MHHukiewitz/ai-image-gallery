import { useState } from "react";

/**
 * The URL one `<img>` of the modal loads, and whether the picture is unavailable.
 *
 * Each poll can sign the URLs again, so `currentUrl` changes while the file doesn't, and a new
 * `src` would download the picture again. The image keeps the first URL, and switches to the
 * current one only when that first URL fails to load, since a signed URL expires.
 *
 * `currentUrl` is empty when the server couldn't sign it. Nothing loads then, and the first real
 * URL a later poll brings is taken. A URL that fails even after the switch is unavailable, until a
 * different one arrives and gets one more try.
 */
export function useImageSource(currentUrl: string) {
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
    unavailable: !pinnedUrl || failedUrl === pinnedUrl,
    onError: () => {
      if (currentUrl && currentUrl !== pinnedUrl) setPinnedUrl(currentUrl);
      else setFailedUrl(pinnedUrl);
    },
  };
}
