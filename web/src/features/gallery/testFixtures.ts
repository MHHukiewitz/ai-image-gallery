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

/** Stands in for IntersectionObserver: tests decide when the sentinel is "in view". */
export class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  observed: Element[] = [];
  disconnected = false;

  constructor(
    private readonly callback: IntersectionObserverCallback,
    readonly options?: IntersectionObserverInit,
  ) {
    FakeIntersectionObserver.instances.push(this);
  }

  /** Observers that are still watching something. */
  static get live(): FakeIntersectionObserver[] {
    return FakeIntersectionObserver.instances.filter((observer) => !observer.disconnected);
  }

  observe(element: Element) {
    this.observed.push(element);
  }

  disconnect() {
    this.disconnected = true;
  }

  report(isIntersecting: boolean) {
    this.callback([{ isIntersecting } as IntersectionObserverEntry], this as never);
  }
}
