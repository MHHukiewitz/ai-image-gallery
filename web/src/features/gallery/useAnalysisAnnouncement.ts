import { useState } from "react";

import type { ImageOut } from "../../lib/api";

function isAnalyzing(image: ImageOut): boolean {
  return image.analysis.status === "pending" || image.analysis.status === "processing";
}

/**
 * The sentence for the analyses that finished between two snapshots of the grid, or "" when none
 * did. One finished image is named. Several are counted, so a burst of finishes isn't read out
 * image by image.
 */
export function announceFinished(previous: readonly ImageOut[], next: readonly ImageOut[]): string {
  const wasAnalyzing = new Set(previous.filter(isAnalyzing).map((image) => image.id));
  const finished = next.filter((image) => wasAnalyzing.has(image.id) && !isAnalyzing(image));
  if (finished.length === 0) return "";

  const failed = finished.filter((image) => image.analysis.status === "failed");
  if (finished.length === 1) {
    const [image] = finished;
    return failed.length === 1
      ? `Analysis failed for ${image.filename}`
      : `Analysis finished for ${image.filename}`;
  }
  return failed.length > 0
    ? `${finished.length} analyses finished, ${failed.length} failed`
    : `${finished.length} analyses finished`;
}

/**
 * The text for the grid's live region. It changes only when the images change, and it never
 * announces an image that was already finished the first time the grid showed it.
 */
export function useAnalysisAnnouncement(images: readonly ImageOut[]): string {
  const [shown, setShown] = useState(images);
  const [announcement, setAnnouncement] = useState("");

  // The React-recommended way to react to a prop change without an effect: compare with the last
  // snapshot during render and update state, which re-renders before the DOM is committed.
  if (images !== shown) {
    setShown(images);
    const message = announceFinished(shown, images);
    if (message !== "") setAnnouncement(message);
  }
  return announcement;
}
