import type { FileError } from "react-dropzone";

const DEFAULT_MAX_UPLOAD_MB = 10;

/**
 * Types by MIME only, no extensions: the file input's `accept` then lists just the image types,
 * so a phone opens its photo picker instead of the generic file browser.
 */
export const ACCEPTED_TYPES = { "image/jpeg": [], "image/png": [] } as const;

/** Reads `VITE_MAX_UPLOAD_MB`. Anything that isn't a positive number falls back to the default. */
export function parseMaxUploadMb(value: string | undefined): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_UPLOAD_MB;
}

export const MAX_UPLOAD_MB = parseMaxUploadMb(import.meta.env.VITE_MAX_UPLOAD_MB);
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

/** The toast text for a file react-dropzone refused. A wrong type is reported before a size problem. */
export function rejectionMessage(
  { file, errors }: { file: File; errors: readonly FileError[] },
  maxMb: number = MAX_UPLOAD_MB,
): string {
  const codes = errors.map((error) => error.code);
  if (codes.includes("file-invalid-type")) {
    return `${file.name} isn't a JPEG or PNG image, so it was skipped.`;
  }
  if (codes.includes("file-too-large")) {
    return `${file.name} is larger than the ${maxMb} MB limit, so it was skipped.`;
  }
  return `${file.name} couldn't be added: ${errors[0]?.message ?? "unknown problem"}.`;
}

/**
 * The toast after a drop in which some files were already in the gallery or repeated within the
 * drop, or null when neither happened. `bumped` files were moved to the top instead of uploaded;
 * `skipped` files were identical to another file in the drop and were not sent twice.
 */
export function duplicateSummary({
  bumped,
  skipped,
}: {
  bumped: number;
  skipped: number;
}): string | null {
  const parts: string[] = [];
  if (bumped > 0) parts.push(`${bumped} already in your gallery, moved to top`);
  if (skipped > 0) {
    const [noun, verb] = skipped === 1 ? ["file", "was"] : ["files", "were"];
    parts.push(`${skipped} identical ${noun} in this drop ${verb} skipped`);
  }
  return parts.length > 0 ? parts.join("; ") : null;
}
