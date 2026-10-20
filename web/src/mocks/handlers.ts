import { delay, http, HttpResponse } from "msw";

import type { components } from "../lib/api-types";
import { MockStore } from "./data";
import type { SearchMode } from "./data";

type ErrorResponse = components["schemas"]["ErrorResponse"];
type ImagePage = components["schemas"]["ImagePage"];

const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 50;
const MAX_QUERY_LENGTH = 200;
const ALLOWED_TYPES = ["image/jpeg", "image/png"];
const MAX_UPLOAD_BYTES = Number(import.meta.env.VITE_MAX_UPLOAD_MB || 10) * 1024 * 1024;

function errorResponse(status: number, code: string, message: string) {
  const body: ErrorResponse = { error: { code, message } };
  return HttpResponse.json(body, { status });
}

function invalid(message: string) {
  return errorResponse(422, "validation_error", message);
}

function notFound() {
  return errorResponse(404, "not_found", "Image not found.");
}

function integerParam(
  params: URLSearchParams,
  name: string,
  { min, max }: { min: number; max: number },
): number | "invalid" | null {
  const raw = params.get(name);
  if (raw === null) return null;
  const value = Number(raw);
  return Number.isInteger(value) && value >= min && value <= max ? value : "invalid";
}

/** Reads the list endpoint's query string with the same rules and 422 messages as the API. */
function parseListRequest(
  params: URLSearchParams,
): { offset: number; limit: number; mode: SearchMode } | { problem: string } {
  const offset = integerParam(params, "offset", { min: 0, max: Number.MAX_SAFE_INTEGER });
  const limit = integerParam(params, "limit", { min: 1, max: MAX_PAGE_SIZE });
  const similarTo = integerParam(params, "similar_to", { min: 1, max: Number.MAX_SAFE_INTEGER });
  if (offset === "invalid") return { problem: "offset must be 0 or more" };
  if (limit === "invalid") return { problem: `limit must be between 1 and ${MAX_PAGE_SIZE}` };
  if (similarTo === "invalid") return { problem: "similar_to must be an image id" };

  const q = params.get("q");
  const color = params.get("color");
  if (q !== null && (q.length < 1 || q.length > MAX_QUERY_LENGTH)) {
    return { problem: `q must be 1 to ${MAX_QUERY_LENGTH} characters` };
  }
  if (color !== null && !/^#[0-9a-fA-F]{6}$/.test(color)) {
    return { problem: "color must look like #aabbcc" };
  }

  const given = [
    q !== null && "q",
    similarTo !== null && "similar_to",
    color !== null && "color",
  ].filter(Boolean);
  if (given.length > 1) {
    return { problem: `only one of q, similar_to, color may be set; got ${given.join(", ")}` };
  }

  let mode: SearchMode = { kind: "all" };
  if (q !== null) mode = { kind: "text", q };
  else if (similarTo !== null) mode = { kind: "similar", imageId: similarTo };
  else if (color !== null) mode = { kind: "color", hex: color };
  return { offset: offset ?? 0, limit: limit ?? DEFAULT_PAGE_SIZE, mode };
}

interface HandlerOptions {
  store?: MockStore;
  /** Fake network delay for reads. Uploads take four times as long. Tests pass 0. */
  latencyMs?: number;
  /** Origin to match, for Node tests where a relative path has nothing to resolve against. */
  baseUrl?: string;
}

/**
 * MSW handlers for the `/api` contract, typed from `api-types.ts`. They fake the whole backend
 * in memory: 45 seeded images, an upload that analyzes for 3 seconds, and rough versions of the
 * three search modes. Supabase auth is not mocked; it still talks to `supabase start`.
 */
export function createHandlers({
  store = new MockStore(),
  latencyMs = 200,
  baseUrl = "",
}: HandlerOptions = {}) {
  const api = (path: string) => `${baseUrl}/api${path}`;
  return [
    http.get(api("/health"), () => HttpResponse.json({ status: "ok" })),

    http.get(api("/v1/images"), async ({ request }) => {
      await delay(latencyMs);
      const parsed = parseListRequest(new URL(request.url).searchParams);
      if ("problem" in parsed) return invalid(parsed.problem);

      const matches = store.list(parsed.mode);
      const end = parsed.offset + parsed.limit;
      const page: ImagePage = {
        items: matches.slice(parsed.offset, end),
        next_offset: end < matches.length ? end : null,
      };
      return HttpResponse.json(page);
    }),

    http.post(api("/v1/images"), async ({ request }) => {
      await delay(latencyMs * 4);
      const file = (await request.formData()).get("file");
      if (file === null || typeof file === "string") return invalid("file is required");
      if (!ALLOWED_TYPES.includes(file.type)) {
        return errorResponse(415, "unsupported_type", "Only JPEG and PNG images are supported.");
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        return errorResponse(413, "too_large", "That file is larger than the upload limit.");
      }
      return HttpResponse.json(store.add(file.name), { status: 201 });
    }),

    http.get(api("/v1/images/:id"), async ({ params }) => {
      await delay(latencyMs);
      const image = store.get(Number(params.id));
      return image ? HttpResponse.json(image) : notFound();
    }),

    http.delete(api("/v1/images/:id"), async ({ params }) => {
      await delay(latencyMs);
      return store.remove(Number(params.id)) ? new HttpResponse(null, { status: 204 }) : notFound();
    }),

    http.post(api("/v1/images/:id/reanalyze"), async ({ params }) => {
      await delay(latencyMs);
      return store.reanalyze(Number(params.id))
        ? new HttpResponse(null, { status: 202 })
        : notFound();
    }),
  ];
}
