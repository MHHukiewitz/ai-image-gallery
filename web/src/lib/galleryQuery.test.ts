import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigationType } from "react-router";
import { describe, expect, it } from "vitest";

import {
  parseGalleryQuery,
  toSearchParams,
  useGalleryQuery,
  useSelectedImage,
} from "./galleryQuery";
import type { GalleryQuery } from "./galleryQuery";

const params = (search: string) => new URLSearchParams(search);

describe("parseGalleryQuery", () => {
  it("reads each mode", () => {
    expect(parseGalleryQuery(params(""))).toEqual({ kind: "all" });
    expect(parseGalleryQuery(params("q=beach+sunset"))).toEqual({
      kind: "text",
      q: "beach sunset",
    });
    expect(parseGalleryQuery(params("similar_to=42"))).toEqual({ kind: "similar", imageId: 42 });
    expect(parseGalleryQuery(params("color=%23AABBCC"))).toEqual({ kind: "color", hex: "#aabbcc" });
  });

  it.each([
    ["a named color", "color=red"],
    ["a short hex", "color=%23abc"],
    ["a hex with a bad digit", "color=%23gggggg"],
    ["a non-numeric id", "similar_to=abc"],
    ["id zero", "similar_to=0"],
    ["a negative id", "similar_to=-3"],
    ["a fractional id", "similar_to=1.5"],
    ["an empty q", "q="],
    ["a blank q", "q=%20%20"],
  ])("falls back to all for %s", (_name, search) => {
    expect(parseGalleryQuery(params(search))).toEqual({ kind: "all" });
  });

  it("accepts a color typed without the # that would start a URL fragment", () => {
    expect(parseGalleryQuery(params("color=aabbcc"))).toEqual({ kind: "color", hex: "#aabbcc" });
  });

  it("prefers q, then similar_to, then color, and skips invalid ones", () => {
    expect(parseGalleryQuery(params("q=a&similar_to=1&color=%23aabbcc"))).toEqual({
      kind: "text",
      q: "a",
    });
    expect(parseGalleryQuery(params("similar_to=1&color=%23aabbcc"))).toEqual({
      kind: "similar",
      imageId: 1,
    });
    expect(parseGalleryQuery(params("q=&similar_to=x&color=%23aabbcc"))).toEqual({
      kind: "color",
      hex: "#aabbcc",
    });
  });

  it("cuts q at the 200 characters the API accepts", () => {
    const parsed = parseGalleryQuery(params(`q=${"x".repeat(300)}`));

    expect(parsed).toEqual({ kind: "text", q: "x".repeat(200) });
  });
});

describe("toSearchParams", () => {
  const queries: GalleryQuery[] = [
    { kind: "all" },
    { kind: "text", q: "red barn & fields" },
    { kind: "similar", imageId: 7 },
    { kind: "color", hex: "#0a1b2c" },
  ];

  it.each(queries)("round-trips $kind through the URL", (query) => {
    const written = toSearchParams(query, params(""));
    // Through a string, as the browser would.
    expect(parseGalleryQuery(new URLSearchParams(written.toString()))).toEqual(query);
  });

  it("writes the documented param names", () => {
    expect(toSearchParams({ kind: "text", q: "dog" }, params("")).get("q")).toBe("dog");
    expect(toSearchParams({ kind: "similar", imageId: 9 }, params("")).get("similar_to")).toBe("9");
    expect(toSearchParams({ kind: "color", hex: "#AABBCC" }, params("")).get("color")).toBe(
      "#aabbcc",
    );
  });

  it("replaces the previous mode and leaves the image and other params alone", () => {
    const current = params("q=old&similar_to=3&image=12&utm=x");

    const next = toSearchParams({ kind: "color", hex: "#aabbcc" }, current);

    expect(next.get("image")).toBe("12");
    expect(next.get("utm")).toBe("x");
    expect(next.get("color")).toBe("#aabbcc");
    expect(next.has("q")).toBe(false);
    expect(next.has("similar_to")).toBe(false);
  });

  it("clears every mode for all, and treats a blank text query as all", () => {
    const current = params("q=old&image=12");

    expect(toSearchParams({ kind: "all" }, current).toString()).toBe("image=12");
    expect(toSearchParams({ kind: "text", q: "  " }, current).toString()).toBe("image=12");
  });

  it("does not change the params it was given", () => {
    const current = params("q=old");

    toSearchParams({ kind: "all" }, current);

    expect(current.toString()).toBe("q=old");
  });
});

function renderGalleryHooks(initialEntry: string) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(MemoryRouter, { initialEntries: ["/start", initialEntry] }, children);
  return renderHook(
    () => ({
      gallery: useGalleryQuery(),
      selected: useSelectedImage(),
      location: useLocation(),
      navigationType: useNavigationType(),
    }),
    { wrapper },
  );
}

// Separate async acts stand in for separate browser events: the queued-params guard in
// galleryQuery.ts is cleared by a microtask between them.
describe("useGalleryQuery", () => {
  it("reads the query from the URL", () => {
    const { result } = renderGalleryHooks("/?similar_to=5&image=2");

    expect(result.current.gallery[0]).toEqual({ kind: "similar", imageId: 5 });
  });

  it("pushes a history entry by default and replaces on request", async () => {
    const { result } = renderGalleryHooks("/");

    await act(async () => result.current.gallery[1]({ kind: "text", q: "a" }));
    expect(result.current.navigationType).toBe("PUSH");
    expect(result.current.location.search).toBe("?q=a");

    await act(async () => result.current.gallery[1]({ kind: "text", q: "ab" }, { replace: true }));
    expect(result.current.navigationType).toBe("REPLACE");
    expect(result.current.location.search).toBe("?q=ab");
  });

  it("keeps the open image when the filter changes", () => {
    const { result } = renderGalleryHooks("/?image=8");

    act(() => result.current.gallery[1]({ kind: "color", hex: "#aabbcc" }));

    expect(result.current.location.search).toBe("?image=8&color=%23aabbcc");
  });

  it("keeps the same query object while only other params change", async () => {
    const { result } = renderGalleryHooks("/?q=cat");
    const before = result.current.gallery[0];

    await act(async () => result.current.selected.open(3));

    expect(result.current.selected.imageId).toBe(3);
    expect(result.current.gallery[0]).toBe(before);
  });

  it("does nothing when the query is unchanged", () => {
    const { result } = renderGalleryHooks("/?q=cat");

    act(() => result.current.gallery[1]({ kind: "text", q: "cat" }));

    expect(result.current.navigationType).toBe("POP");
  });
});

describe("useSelectedImage", () => {
  it("reports no image for a missing or invalid param", () => {
    expect(renderGalleryHooks("/").result.current.selected.imageId).toBeNull();
    expect(renderGalleryHooks("/?image=abc").result.current.selected.imageId).toBeNull();
    expect(renderGalleryHooks("/?image=-1").result.current.selected.imageId).toBeNull();
  });

  it("opens with a new history entry and closes by going back to the grid", async () => {
    const { result } = renderGalleryHooks("/");

    await act(async () => result.current.selected.open(4));
    expect(result.current.location.search).toBe("?image=4");
    expect(result.current.navigationType).toBe("PUSH");

    await act(async () => result.current.selected.close());
    await waitFor(() => expect(result.current.selected.imageId).toBeNull());
    expect(result.current.location.search).toBe("");
    expect(result.current.navigationType).toBe("POP");
  });

  it("drops the param in place when the modal was opened by a deep link", () => {
    const { result } = renderGalleryHooks("/?image=4");

    act(() => result.current.selected.close());

    expect(result.current.selected.imageId).toBeNull();
    expect(result.current.navigationType).toBe("REPLACE");
  });

  it("swaps the image in an open modal without stacking history", async () => {
    const { result } = renderGalleryHooks("/");
    await act(async () => result.current.selected.open(4));

    await act(async () => result.current.selected.open(5));

    expect(result.current.selected.imageId).toBe(5);
    expect(result.current.navigationType).toBe("REPLACE");
  });

  it("lets a filter change and a close in the same handler both take effect", async () => {
    const { result } = renderGalleryHooks("/");
    await act(async () => result.current.selected.open(4));

    await act(async () => {
      result.current.gallery[1]({ kind: "text", q: "beach" });
      result.current.selected.close();
    });

    expect(result.current.location.search).toBe("?q=beach");
    expect(result.current.selected.imageId).toBeNull();
  });
});
