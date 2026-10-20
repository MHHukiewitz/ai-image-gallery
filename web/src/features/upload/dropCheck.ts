/** A file from one drop with the SHA-256 of its bytes, or null when it couldn't be hashed. */
export interface HashedFile {
  id: string;
  file: File;
  sha256: string | null;
}

/**
 * Splits a drop into the files to send on and the ones that repeat an earlier file in the same
 * drop (same bytes, any name). The first of each group stays; the rest are `skipped`, so the same
 * photo picked twice is uploaded once. A file with no hash is never skipped, because nothing says
 * it is a copy.
 */
export function collapseIdentical(files: readonly HashedFile[]): {
  unique: HashedFile[];
  skipped: HashedFile[];
} {
  const seen = new Set<string>();
  const unique: HashedFile[] = [];
  const skipped: HashedFile[] = [];
  for (const entry of files) {
    if (entry.sha256 !== null && seen.has(entry.sha256)) {
      skipped.push(entry);
      continue;
    }
    if (entry.sha256 !== null) seen.add(entry.sha256);
    unique.push(entry);
  }
  return { unique, skipped };
}

/** `items` in consecutive groups of at most `size`, for the API's per-request limit. */
export function inChunks<T>(items: readonly T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let start = 0; start < items.length; start += size) {
    chunks.push(items.slice(start, start + size));
  }
  return chunks;
}
