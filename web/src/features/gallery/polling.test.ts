import { describe, expect, it } from "vitest";

import type { AnalysisStatus, ImagePage } from "../../lib/api";
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

describe("refetchIntervalFor", () => {
  it("polls every 2 seconds while an image is pending", () => {
    expect(POLL_INTERVAL_MS).toBe(2000);
    expect(refetchIntervalFor(pagesWith(["done", "pending"]))).toBe(2000);
  });

  it("polls while an image is processing", () => {
    expect(refetchIntervalFor(pagesWith(["done", "processing"]))).toBe(2000);
  });

  it("finds a pending image on a later page", () => {
    expect(refetchIntervalFor(pagesWith(["done", "done"], ["done", "pending"]))).toBe(2000);
  });

  it("stops when everything is done or failed", () => {
    expect(refetchIntervalFor(pagesWith(["done", "failed", "done"], ["done"]))).toBe(false);
  });

  it("does not poll an empty gallery or before the first page arrives", () => {
    expect(refetchIntervalFor(pagesWith([]))).toBe(false);
    expect(refetchIntervalFor(undefined)).toBe(false);
  });
});
