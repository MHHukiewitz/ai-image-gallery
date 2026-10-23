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
  shares: ["shares"] as const,
  signups: ["signups"] as const,
  // Per user, so an account's answer never shows for the next one to sign in on this tab.
  account: (userId: string) => ["account", userId] as const,
  siteSettings: ["site-settings"] as const,
};
