/** Lowercase hex of the SHA-256 of `data`, the form the API's content hashes use. */
export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * The SHA-256 of a file's bytes, or null when it can't be computed. `crypto.subtle` is missing
 * outside secure contexts (a phone opening the dev server over http), and a file can vanish
 * between the drop and the read. Skipping the duplicate check then is safe: the server hashes
 * every upload itself and bumps a duplicate instead of storing it.
 */
export async function hashFile(file: File): Promise<string | null> {
  try {
    return await sha256Hex(await file.arrayBuffer());
  } catch {
    return null;
  }
}
