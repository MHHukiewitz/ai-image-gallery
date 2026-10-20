import { describe, expect, it } from "vitest";

import { collapseIdentical, inChunks } from "./dropCheck";
import type { HashedFile } from "./dropCheck";

function entry(id: string, sha256: string | null): HashedFile {
  return { id, file: new File(["x"], `${id}.png`), sha256 };
}

describe("collapseIdentical", () => {
  it("keeps every file when all differ", () => {
    const files = [entry("a", "1"), entry("b", "2"), entry("c", "3")];

    const { unique, skipped } = collapseIdentical(files);

    expect(unique).toEqual(files);
    expect(skipped).toEqual([]);
  });

  it("keeps the first of identical files and skips the rest, whatever they are called", () => {
    const { unique, skipped } = collapseIdentical([
      entry("beach", "1"),
      entry("forest", "2"),
      entry("beach-copy", "1"),
      entry("beach-copy-2", "1"),
    ]);

    expect(unique.map(({ id }) => id)).toEqual(["beach", "forest"]);
    expect(skipped.map(({ id }) => id)).toEqual(["beach-copy", "beach-copy-2"]);
  });

  it("never skips a file it could not hash", () => {
    const { unique, skipped } = collapseIdentical([entry("a", null), entry("b", null)]);

    expect(unique.map(({ id }) => id)).toEqual(["a", "b"]);
    expect(skipped).toEqual([]);
  });

  it("handles an empty drop", () => {
    expect(collapseIdentical([])).toEqual({ unique: [], skipped: [] });
  });
});

describe("inChunks", () => {
  it("splits a list into groups of at most the size, in order", () => {
    expect(inChunks([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
  });

  it("makes 50 hashes one chunk and 51 two", () => {
    const fifty = Array.from({ length: 50 }, (_, index) => index);

    expect(inChunks(fifty, 50)).toHaveLength(1);
    expect(inChunks([...fifty, 50], 50).map((chunk) => chunk.length)).toEqual([50, 1]);
  });

  it("gives no chunks for an empty list", () => {
    expect(inChunks([], 50)).toEqual([]);
  });
});
