import { describe, expect, it } from "vitest";

import type { ImageOut } from "../../lib/api";
import { announceFinished } from "./useAnalysisAnnouncement";
import { makeImage } from "./testFixtures";

function withStatus(image: ImageOut, status: ImageOut["analysis"]["status"]): ImageOut {
  return { ...image, analysis: { ...image.analysis, status } };
}

describe("announceFinished", () => {
  const beach = makeImage(1, { status: "processing" });
  const forest = makeImage(2, { status: "pending" });

  it("names the one image whose analysis just finished", () => {
    expect(announceFinished([beach], [withStatus(beach, "done")])).toBe(
      `Analysis finished for ${beach.filename}`,
    );
  });

  it("says so when the one image that finished is a failure", () => {
    expect(announceFinished([beach], [withStatus(beach, "failed")])).toBe(
      `Analysis failed for ${beach.filename}`,
    );
  });

  it("counts a burst of finishes instead of naming each image", () => {
    const previous = [beach, forest];
    const next = [withStatus(beach, "done"), withStatus(forest, "done")];

    expect(announceFinished(previous, next)).toBe("2 analyses finished");
  });

  it("counts the failures in a burst", () => {
    const previous = [beach, forest];
    const next = [withStatus(beach, "done"), withStatus(forest, "failed")];

    expect(announceFinished(previous, next)).toBe("2 analyses finished, 1 failed");
  });

  it("is silent when nothing finished, including an image that was already done", () => {
    const done = makeImage(3, { status: "done" });
    expect(announceFinished([done], [done])).toBe("");
    expect(announceFinished([beach], [beach])).toBe("");
  });

  it("ignores an image that is new to the grid, since it was never seen analyzing", () => {
    expect(announceFinished([], [withStatus(forest, "done")])).toBe("");
  });
});
