import type { InfiniteData } from "@tanstack/react-query";

import type { ImagePage } from "../../lib/api";

/** Which way the modal moves through the grid. */
export type Step = "previous" | "next";

/** How far a finger must travel sideways before a touch counts as a swipe. */
export const SWIPE_MIN_PX = 50;

/**
 * The grid's image ids in the order it shows them. Pages can overlap after an upload, so a repeat
 * keeps its first place, the same way the grid drops it.
 */
export function orderOf(data: InfiniteData<ImagePage> | undefined): number[] {
  const seen = new Set<number>();
  const ids: number[] = [];
  for (const page of data?.pages ?? []) {
    for (const image of page.items) {
      if (seen.has(image.id)) continue;
      seen.add(image.id);
      ids.push(image.id);
    }
  }
  return ids;
}

export interface Neighbors {
  /** Zero-based place of the image in the order. */
  index: number;
  prev: number | null;
  next: number | null;
}

/** Where `imageId` sits in `ids`, and the ids either side. Null when it isn't among them. */
export function neighborsOf(ids: readonly number[], imageId: number): Neighbors | null {
  const index = ids.indexOf(imageId);
  if (index === -1) return null;
  return { index, prev: ids[index - 1] ?? null, next: ids[index + 1] ?? null };
}

interface Point {
  x: number;
  y: number;
}

/**
 * The step a touch makes, or null when it is too short or mostly vertical. A vertical drag is a
 * scroll, not a swipe. Swiping left shows the next image, as a page turns.
 */
export function swipeStep(start: Point, end: Point): Step | null {
  const dx = end.x - start.x;
  if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) <= Math.abs(end.y - start.y)) return null;
  return dx < 0 ? "next" : "previous";
}
