import { QueryClient } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { ImageOut, ImagePage } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { isGalleryEmpty, prependToGallery } from "./galleryCache";

function image(id: number): ImageOut {
  return {
    id,
    filename: `photo-${id}.jpg`,
    uploaded_at: "2026-01-01T00:00:00Z",
    width: 100,
    height: 100,
    thumbnail_url: `https://example.test/${id}.webp`,
    analysis: { status: "pending", tags: [], description: null, colors: [], error: null },
  };
}

function pages(...itemIds: number[][]): InfiniteData<ImagePage, number> {
  return {
    pages: itemIds.map((ids, index) => ({
      items: ids.map(image),
      next_offset: index < itemIds.length - 1 ? (index + 1) * 20 : null,
    })),
    pageParams: itemIds.map((_, index) => index * 20),
  };
}

const ALL = queryKeys.images.list({ kind: "all" });

describe("prependToGallery", () => {
  it("puts the image first on the first page and leaves later pages alone", () => {
    const client = new QueryClient();
    const data = pages([3, 2], [1]);
    client.setQueryData(ALL, data);

    prependToGallery(client, image(4));

    const next = client.getQueryData<InfiniteData<ImagePage, number>>(ALL);
    expect(next?.pages[0].items.map(({ id }) => id)).toEqual([4, 3, 2]);
    expect(next?.pages[1]).toBe(data.pages[1]);
    expect(next?.pages[0].next_offset).toBe(20);
    expect(next?.pageParams).toEqual([0, 20]);
  });

  it("works on an empty first page", () => {
    const client = new QueryClient();
    client.setQueryData(ALL, pages([]));

    prependToGallery(client, image(1));

    expect(client.getQueryData<InfiniteData<ImagePage>>(ALL)?.pages[0].items).toHaveLength(1);
  });

  it("doesn't add an image twice", () => {
    const client = new QueryClient();
    client.setQueryData(ALL, pages([2], [1]));

    prependToGallery(client, image(1));

    expect(
      client.getQueryData<InfiniteData<ImagePage>>(ALL)?.pages.flatMap((p) => p.items),
    ).toHaveLength(2);
  });

  it("doesn't create the list when the grid hasn't loaded it", () => {
    const client = new QueryClient();

    prependToGallery(client, image(1));

    expect(client.getQueryData(ALL)).toBeUndefined();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it("leaves other lists alone, such as a search", () => {
    const client = new QueryClient();
    const search = queryKeys.images.list({ kind: "text", q: "beach" });
    client.setQueryData(search, pages([9]));

    prependToGallery(client, image(1));

    expect(client.getQueryData<InfiniteData<ImagePage>>(search)?.pages[0].items).toHaveLength(1);
  });

  it("ignores a cache entry that isn't paged data", () => {
    const client = new QueryClient();
    client.setQueryData(ALL, { unexpected: true });

    prependToGallery(client, image(1));

    expect(client.getQueryData(ALL)).toEqual({ unexpected: true });
  });
});

describe("isGalleryEmpty", () => {
  it("is false until the list has loaded", () => {
    expect(isGalleryEmpty(new QueryClient())).toBe(false);
  });

  it("is true when every loaded page is empty", () => {
    const client = new QueryClient();
    client.setQueryData(ALL, pages([]));

    expect(isGalleryEmpty(client)).toBe(true);
  });

  it("is false once there is an image", () => {
    const client = new QueryClient();
    client.setQueryData(ALL, pages([1]));

    expect(isGalleryEmpty(client)).toBe(false);
  });

  it("ignores search results, since an empty search isn't an empty gallery", () => {
    const client = new QueryClient();
    client.setQueryData(queryKeys.images.list({ kind: "text", q: "nothing" }), pages([]));

    expect(isGalleryEmpty(client)).toBe(false);
  });
});
