import { useGalleryQuery } from "../../lib/galleryQuery";
import { ActiveFilterChip } from "./ActiveFilterChip";

/**
 * The row above the grid that names a similar-image or color filter. It sits in the page's content
 * column, so it never covers the upload bar. Other modes render nothing.
 */
export function ActiveFilterRow() {
  const [query, setQuery] = useGalleryQuery();
  if (query.kind !== "similar" && query.kind !== "color") return null;

  return (
    <div className="mb-6">
      <ActiveFilterChip query={query} onClear={() => setQuery({ kind: "all" })} />
    </div>
  );
}
