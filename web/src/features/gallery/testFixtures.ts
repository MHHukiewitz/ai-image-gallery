import type { AnalysisOut, ImageOut } from "../../lib/api";

/** An image as the list endpoint returns it. Override `analysis` fields per test. */
export function makeImage(
  id: number,
  analysis: Partial<AnalysisOut> = {},
  overrides: Partial<ImageOut> = {},
): ImageOut {
  return {
    id,
    filename: `photo-${id}.jpg`,
    uploaded_at: "2026-10-20T10:00:00Z",
    width: 1600,
    height: 1067,
    thumbnail_url: `https://storage.test/thumb-${id}.webp?token=first`,
    analysis: {
      status: "done",
      tags: ["beach", "sunset", "ocean", "sand", "waves"],
      description: `Description of photo ${id}.`,
      colors: ["#aabbcc", "#112233", "#445566"],
      error: null,
      ...analysis,
    },
    ...overrides,
  };
}
