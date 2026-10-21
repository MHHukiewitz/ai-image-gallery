import { describe, expect, it } from "vitest";

import type { AnalysisStatus, ImageOut, ImagePage } from "../../lib/api";
import { LIVE_POLL_INTERVAL_MS } from "../../lib/realtimeStatus";
import { POLL_INTERVAL_MS, refetchIntervalFor } from "./polling";
import { makeImage } from "./testFixtures";

function pagesWith(...statuses: AnalysisStatus[][]): { pages: ImagePage[] } {
  let id = 0;
  return {
    pages: statuses.map((pageStatuses) => ({
      items: pageStatuses.map((status) => makeImage(++id, { status })),
      next_offset: null,
    })),
  };
}

function pageOf(...items: ImageOut[]): { pages: ImagePage[] } {
  return { pages: [{ items, next_offset: null }] };
}

describe("refetchIntervalFor", () => {
  it("polls every 3 seconds while an image is pending", () => {
    expect(POLL_INTERVAL_MS).toBe(3000);
    expect(refetchIntervalFor(pagesWith(["done", "pending"]))).toBe(3000);
  });

  it("polls while an image is processing", () => {
    expect(refetchIntervalFor(pagesWith(["done", "processing"]))).toBe(3000);
  });

  it("finds a pending image on a later page", () => {
    expect(refetchIntervalFor(pagesWith(["done", "done"], ["done", "pending"]))).toBe(3000);
  });

  it("polls while a finished image has no thumbnail URL, so a fresh one can arrive", () => {
    const data = pageOf(makeImage(1), makeImage(2, {}, { thumbnail_url: "" }));

    expect(refetchIntervalFor(data)).toBe(3000);
  });

  it("polls for an empty URL on a failed image, and on a later page", () => {
    const failed = makeImage(1, { status: "failed" }, { thumbnail_url: "" });

    expect(refetchIntervalFor(pageOf(failed))).toBe(3000);
    expect(
      refetchIntervalFor({
        pages: [{ items: [makeImage(2)], next_offset: 1 }, ...pageOf(failed).pages],
      }),
    ).toBe(3000);
  });

  it("stops once every URL is present and nothing is being analyzed", () => {
    expect(refetchIntervalFor(pageOf(makeImage(1), makeImage(2, { status: "failed" })))).toBe(
      false,
    );
  });

  it("stops when everything is done or failed", () => {
    expect(refetchIntervalFor(pagesWith(["done", "failed", "done"], ["done"]))).toBe(false);
  });

  it("does not poll an empty gallery or before the first page arrives", () => {
    expect(refetchIntervalFor(pagesWith([]))).toBe(false);
    expect(refetchIntervalFor(undefined)).toBe(false);
  });
});

describe("refetchIntervalFor with the Realtime channel", () => {
  it("polls every 15 seconds while the channel is live and an image is analyzing", () => {
    expect(LIVE_POLL_INTERVAL_MS).toBe(15_000);
    expect(refetchIntervalFor(pagesWith(["done", "pending"]), true)).toBe(15_000);
    expect(refetchIntervalFor(pagesWith(["done"], ["processing"]), true)).toBe(15_000);
  });

  it("keeps the 3 second pace while the channel is down", () => {
    expect(refetchIntervalFor(pagesWith(["done", "pending"]), false)).toBe(3000);
  });

  it("keeps the 3 second pace when there is no channel at all, as in mocks mode", () => {
    expect(refetchIntervalFor(pagesWith(["done", "pending"]))).toBe(3000);
  });

  it("still stops once nothing is analyzing", () => {
    expect(refetchIntervalFor(pagesWith(["done", "failed"]), true)).toBe(false);
    expect(refetchIntervalFor(undefined, true)).toBe(false);
  });

  it("keeps the fast pace for an empty thumbnail URL, which no push announces", () => {
    const data = pageOf(makeImage(1), makeImage(2, { status: "pending" }, { thumbnail_url: "" }));

    expect(refetchIntervalFor(data, true)).toBe(3000);
  });
});
