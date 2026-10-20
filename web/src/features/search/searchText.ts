import type { GalleryQuery } from "../../lib/galleryQuery";

/** The API rejects longer `q` values with a 422, so the input stops at the same length. */
export const MAX_SEARCH_LENGTH = 200;

/** The text the search input shows for a query: only a text search has any. */
export function textOf(query: GalleryQuery): string {
  return query.kind === "text" ? query.q : "";
}

/**
 * The query that typing `text` asks for. Surrounding spaces don't change a search, so they
 * don't make a new query (a trailing space shouldn't refetch). Blank text means no search.
 */
export function queryForText(text: string): GalleryQuery {
  const q = text.trim().slice(0, MAX_SEARCH_LENGTH);
  return q === "" ? { kind: "all" } : { kind: "text", q };
}
