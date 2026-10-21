import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ImageOut } from "../../lib/api";
import { LIVE_POLL_INTERVAL_MS } from "../../lib/realtimeStatus";
import { analysisRefetchInterval, isAnalyzing, isNotFound } from "./useImageDetail";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));

function withStatus(status: ImageOut["analysis"]["status"]): ImageOut {
  return {
    id: 1,
    filename: "beach.jpg",
    uploaded_at: "2026-10-20T10:00:00Z",
    width: 1600,
    height: 1067,
    thumbnail_url: "https://storage.test/thumb.webp",
    analysis: { status, tags: [], description: null, colors: [], error: null },
  };
}

describe("analysisRefetchInterval", () => {
  it.each(["pending", "processing"] as const)("polls every 2 s while %s", (status) => {
    expect(analysisRefetchInterval(withStatus(status))).toBe(2000);
  });

  it.each(["done", "failed"] as const)("stops polling once %s", (status) => {
    expect(analysisRefetchInterval(withStatus(status))).toBe(false);
  });

  it("polls while the original or the thumbnail has no URL, whatever the status", () => {
    const done = withStatus("done");

    expect(analysisRefetchInterval({ ...done, original_url: "" })).toBe(2000);
    expect(analysisRefetchInterval({ ...done, thumbnail_url: "" })).toBe(2000);
    expect(analysisRefetchInterval({ ...withStatus("failed"), original_url: "" })).toBe(2000);
  });

  it("stops once every URL is there, or the original was never part of the response", () => {
    const done = withStatus("done");

    expect(analysisRefetchInterval({ ...done, original_url: "https://storage.test/o.jpg" })).toBe(
      false,
    );
    expect(analysisRefetchInterval({ ...done, original_url: null })).toBe(false);
  });

  it("doesn't poll before the first response", () => {
    expect(analysisRefetchInterval(undefined)).toBe(false);
  });
});

describe("analysisRefetchInterval with the Realtime channel", () => {
  it.each(["pending", "processing"] as const)(
    "polls every 15 s while %s and the channel is live",
    (status) => {
      expect(LIVE_POLL_INTERVAL_MS).toBe(15_000);
      expect(analysisRefetchInterval(withStatus(status), true)).toBe(15_000);
    },
  );

  it.each(["pending", "processing"] as const)(
    "keeps polling every 2 s while %s and the channel is down or absent",
    (status) => {
      expect(analysisRefetchInterval(withStatus(status), false)).toBe(2000);
      expect(analysisRefetchInterval(withStatus(status))).toBe(2000);
    },
  );

  it("still stops once the analysis is final", () => {
    expect(analysisRefetchInterval(withStatus("done"), true)).toBe(false);
    expect(analysisRefetchInterval(withStatus("failed"), true)).toBe(false);
  });

  it("keeps the fast pace for an empty URL, which no push announces", () => {
    expect(analysisRefetchInterval({ ...withStatus("pending"), original_url: "" }, true)).toBe(
      2000,
    );
    expect(analysisRefetchInterval({ ...withStatus("done"), thumbnail_url: "" }, true)).toBe(2000);
  });
});

describe("isAnalyzing", () => {
  it("is true only for pending and processing", () => {
    expect(isAnalyzing("pending")).toBe(true);
    expect(isAnalyzing("processing")).toBe(true);
    expect(isAnalyzing("done")).toBe(false);
    expect(isAnalyzing("failed")).toBe(false);
    expect(isAnalyzing(undefined)).toBe(false);
  });
});

describe("isNotFound", () => {
  it("matches only a 404 from the API", () => {
    expect(isNotFound(new ApiError(404, "not_found", "Image not found."))).toBe(true);
    expect(isNotFound(new ApiError(500, "http_500", "Something went wrong."))).toBe(false);
    expect(isNotFound(new Error("network"))).toBe(false);
    expect(isNotFound(null)).toBe(false);
  });
});
