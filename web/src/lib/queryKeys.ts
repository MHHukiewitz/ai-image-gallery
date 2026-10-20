import type { GalleryQuery } from "./galleryQuery";

/**
 * Every TanStack Query key in one place. Keys nest, so invalidating `images.all` refreshes
 * every list and every detail at once.
 */
export const queryKeys = {
  images: {
    all: ["images"] as const,
    list: (query: GalleryQuery) => ["images", "list", query] as const,
    detail: (id: number) => ["images", "detail", id] as const,
  },
};
