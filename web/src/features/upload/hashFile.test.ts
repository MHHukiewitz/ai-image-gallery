import { afterEach, describe, expect, it, vi } from "vitest";

import { hashFile, sha256Hex } from "./hashFile";

const SHA256_OF_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
const SHA256_OF_NOTHING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

const bytes = (text: string) => new TextEncoder().encode(text).buffer as ArrayBuffer;

afterEach(() => vi.unstubAllGlobals());

describe("sha256Hex", () => {
  it("gives the known SHA-256 of a fixed buffer, as lowercase hex", async () => {
    expect(await sha256Hex(bytes("abc"))).toBe(SHA256_OF_ABC);
  });

  it("hashes an empty buffer", async () => {
    expect(await sha256Hex(new ArrayBuffer(0))).toBe(SHA256_OF_NOTHING);
  });

  it("pads every byte to two digits, so the result is always 64 characters", async () => {
    // These bytes make the digest start with low values; a missing pad would shorten it.
    for (const text of ["a", "b", "c", "d", "e", "f", "g", "h"]) {
      expect(await sha256Hex(bytes(text))).toMatch(/^[0-9a-f]{64}$/);
    }
  });

  it("matches what the API's lookup accepts", async () => {
    expect(await sha256Hex(bytes("abc"))).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("hashFile", () => {
  it("hashes the bytes of a file, whatever its name or type", async () => {
    const png = new File(["abc"], "holiday.png", { type: "image/png" });
    const renamed = new File(["abc"], "copy of holiday.jpg", { type: "image/jpeg" });

    expect(await hashFile(png)).toBe(SHA256_OF_ABC);
    expect(await hashFile(renamed)).toBe(SHA256_OF_ABC);
  });

  it("gives different hashes for different bytes", async () => {
    expect(await hashFile(new File(["abd"], "a.png"))).not.toBe(SHA256_OF_ABC);
  });

  it("returns null where Web Crypto is missing, as on a page served over plain http", async () => {
    vi.stubGlobal("crypto", {});

    expect(await hashFile(new File(["abc"], "a.png"))).toBeNull();
  });

  it("returns null when the file can't be read", async () => {
    const unreadable = new File(["abc"], "a.png");
    vi.spyOn(unreadable, "arrayBuffer").mockRejectedValue(
      new DOMException("gone", "NotFoundError"),
    );

    expect(await hashFile(unreadable)).toBeNull();
  });
});
