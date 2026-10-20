import { describe, expect, it } from "vitest";

import type { ImageOut, ImagePage } from "../../lib/api";
import { neighborsOf, orderOf, SWIPE_MIN_PX, swipeStep } from "./imageNavigation";

function image(id: number): ImageOut {
  return {
    id,
    filename: `photo-${id}.jpg`,
    uploaded_at: "2026-10-20T10:00:00Z",
    width: 1600,
    height: 1067,
    thumbnail_url: `https://storage.test/thumb-${id}.webp`,
    analysis: { status: "done", tags: [], description: null, colors: [], error: null },
  };
}

function pageOf(ids: number[]): ImagePage {
  return { items: ids.map(image), next_offset: null };
}

describe("orderOf", () => {
  it("lists the ids of every page in order, and keeps the first place of a repeat", () => {
    const data = { pages: [pageOf([3, 1]), pageOf([1, 2])], pageParams: [0, 2] };

    expect(orderOf(data)).toEqual([3, 1, 2]);
  });

  it("is empty before the first page has loaded", () => {
    expect(orderOf(undefined)).toEqual([]);
  });
});

describe("neighborsOf", () => {
  it("gives the image's place and the ids either side", () => {
    expect(neighborsOf([4, 9, 2], 9)).toEqual({ index: 1, prev: 4, next: 2 });
  });

  it("has no previous at the first image, and no next at the last", () => {
    expect(neighborsOf([4, 9, 2], 4)).toEqual({ index: 0, prev: null, next: 9 });
    expect(neighborsOf([4, 9, 2], 2)).toEqual({ index: 2, prev: 9, next: null });
  });

  it("is null for an image that isn't in the order", () => {
    expect(neighborsOf([4, 9, 2], 7)).toBeNull();
  });
});

describe("swipeStep", () => {
  const start = { x: 200, y: 100 };

  it("steps to the next image on a leftward swipe, and to the previous on a rightward one", () => {
    expect(swipeStep(start, { x: 200 - SWIPE_MIN_PX, y: 100 })).toBe("next");
    expect(swipeStep(start, { x: 200 + SWIPE_MIN_PX, y: 100 })).toBe("previous");
  });

  it("ignores a swipe shorter than the threshold", () => {
    expect(swipeStep(start, { x: 200 - SWIPE_MIN_PX + 1, y: 100 })).toBeNull();
  });

  it("ignores a mostly vertical drag, which is a scroll and not a swipe", () => {
    expect(swipeStep(start, { x: 200 - 60, y: 100 + 120 })).toBeNull();
  });
});
