import type { components, operations } from "./api-types";
import type { GalleryQuery } from "./galleryQuery";
import { supabase } from "./supabase";

export type ImageOut = components["schemas"]["ImageOut"];
export type ImagePage = components["schemas"]["ImagePage"];
export type AnalysisOut = components["schemas"]["AnalysisOut"];
export type AnalysisStatus = components["schemas"]["AnalysisStatus"];
export type ShareOut = components["schemas"]["ShareOut"];
export type ShareList = components["schemas"]["ShareList"];
export type SignupStatus = components["schemas"]["SignupStatus"];
export type AccountOut = components["schemas"]["AccountOut"];
export type SiteSettingsOut = components["schemas"]["SiteSettingsOut"];

/** Where the API lives. Same-origin `/api` unless the build says otherwise. */
export const API_BASE = (import.meta.env.VITE_API_BASE || "/api").replace(/\/+$/, "");

/** Items per request. The grid and the mock API both page by this. */
export const PAGE_SIZE = 20;

const SIGN_IN_PATH = "/sign-in";
const AUTH_PATHS = [SIGN_IN_PATH, "/sign-up"];

/** An API failure in the one error shape `{"error": {"code", "message"}}`. Status 0 means no response. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** Seam for the full-page redirect, which jsdom can't observe through `window.location`. */
export const navigation = {
  redirectToSignIn(): void {
    window.location.assign(SIGN_IN_PATH);
  },
};

function isErrorBody(value: unknown): value is components["schemas"]["ErrorResponse"] {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const { error } = value;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string"
  );
}

function networkError(): ApiError {
  return new ApiError(
    0,
    "network_error",
    "Couldn't reach the server. Check your connection and try again.",
  );
}

/** Turns a failed response into an ApiError. Bodies that aren't our error shape (a proxy's 502 page) still get one. */
function toApiError(status: number, body: string): ApiError {
  try {
    const parsed: unknown = JSON.parse(body);
    if (isErrorBody(parsed)) return new ApiError(status, parsed.error.code, parsed.error.message);
  } catch {
    // Not JSON: fall through to the generic error.
  }
  const message =
    status >= 500
      ? "Something went wrong on our side. Please try again."
      : `The request failed (HTTP ${status}).`;
  return new ApiError(status, `http_${status}`, message);
}

async function accessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

let signingOut: Promise<void> | null = null;

/** The API rejected our token, so the session is dead: drop it and go to sign-in. */
function handleUnauthorized(): Promise<void> {
  // Parallel requests all fail together; sign out and redirect once.
  signingOut ??= supabase.auth
    .signOut({ scope: "local" }) // The server already refused the token, so only clear it here.
    .catch(() => undefined)
    .then(() => {
      if (!AUTH_PATHS.includes(window.location.pathname)) navigation.redirectToSignIn();
    })
    .finally(() => {
      signingOut = null;
    });
  return signingOut;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/**
 * Calls the API with the user's access token. `path` is relative to the API base, for example
 * `/v1/images`. Failures throw `ApiError`; a 401 also signs the user out and redirects.
 */
export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const token = await accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch (error) {
    if (isAbort(error)) throw error;
    throw networkError();
  }

  if (!response.ok) {
    const apiError = toApiError(response.status, await response.text());
    if (response.status === 401) await handleUnauthorized();
    throw apiError;
  }

  // 202 and 204 have no body. Callers that expect one name it in T.
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

type ListParams = NonNullable<operations["list_images_api_v1_images_get"]["parameters"]["query"]>;

function listParams(query: GalleryQuery, offset: number): ListParams {
  const base: ListParams = { offset, limit: PAGE_SIZE };
  switch (query.kind) {
    case "text":
      return { ...base, q: query.q };
    case "similar":
      return { ...base, similar_to: query.imageId };
    case "color":
      return { ...base, color: query.hex };
    case "all":
      return base;
  }
}

/** One page of the grid. Pass `next_offset` of the previous page as `offset`. */
export function listImages(
  query: GalleryQuery,
  offset: number,
  options: { signal?: AbortSignal } = {},
): Promise<ImagePage> {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(listParams(query, offset))) {
    if (value !== null && value !== undefined) search.set(name, String(value));
  }
  return apiFetch<ImagePage>(`/v1/images?${search}`, { signal: options.signal });
}

/** One image, with its full-size `original_url`. */
export function getImage(id: number, options: { signal?: AbortSignal } = {}): Promise<ImageOut> {
  return apiFetch<ImageOut>(`/v1/images/${id}`, { signal: options.signal });
}

export function deleteImage(id: number): Promise<void> {
  return apiFetch<void>(`/v1/images/${id}`, { method: "DELETE" });
}

/** Queues the image for analysis again. The status goes back to `pending`. */
export function reanalyzeImage(id: number): Promise<void> {
  return apiFetch<void>(`/v1/images/${id}/reanalyze`, { method: "POST" });
}

/** Whom the user shares their gallery with, and who shares theirs with the user. */
export function listShares(options: { signal?: AbortSignal } = {}): Promise<ShareList> {
  return apiFetch<ShareList>("/v1/shares", { signal: options.signal });
}

/**
 * Asks the account that uses `email` to accept the user's gallery. Fails with `no_account` (404),
 * `already_shared` (409) or `own_address` (422); the error's message says so in words.
 */
export function shareGallery(email: string): Promise<ShareOut> {
  const body: components["schemas"]["ShareRequest"] = { email };
  return apiFetch<ShareOut>("/v1/shares", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Accepts a share made to the user. The owner's images then join the user's gallery. */
export function acceptShare(id: number): Promise<void> {
  return apiFetch<void>(`/v1/shares/${id}/accept`, { method: "POST" });
}

/** Ends a share from either side: revoke one the user made, or decline or leave one they received. */
export function deleteShare(id: number): Promise<void> {
  return apiFetch<void>(`/v1/shares/${id}`, { method: "DELETE" });
}

/** Whether new accounts can be created right now. Needs no sign-in, so the sign-up page can ask. */
export function getSignupStatus(options: { signal?: AbortSignal } = {}): Promise<SignupStatus> {
  return apiFetch<SignupStatus>("/v1/signups", { signal: options.signal });
}

/** What the signed-in user's account may do. The server decides who is an admin. */
export function getAccount(options: { signal?: AbortSignal } = {}): Promise<AccountOut> {
  return apiFetch<AccountOut>("/v1/account", { signal: options.signal });
}

/** The site-wide settings. Fails with `forbidden` (403) for anyone but an admin. */
export function getSiteSettings(options: { signal?: AbortSignal } = {}): Promise<SiteSettingsOut> {
  return apiFetch<SiteSettingsOut>("/v1/admin/settings", { signal: options.signal });
}

/** Opens or closes sign-ups. Fails with `forbidden` (403) for anyone but an admin. */
export function updateSiteSettings(
  update: components["schemas"]["SiteSettingsUpdate"],
): Promise<SiteSettingsOut> {
  return apiFetch<SiteSettingsOut>("/v1/admin/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
}

/** The most hashes one `lookupImages` request may carry. */
export const LOOKUP_LIMIT = 50;

/**
 * Asks which of the files, by SHA-256 (64 lowercase hex characters), the user already has.
 * Returns the id of the matching image for each hash that has one; the rest are absent. At most
 * `LOOKUP_LIMIT` hashes per call.
 */
export async function lookupImages(
  sha256s: readonly string[],
  options: { signal?: AbortSignal } = {},
): Promise<Record<string, number>> {
  const body: components["schemas"]["LookupRequest"] = { content_sha256: [...sha256s] };
  const response = await apiFetch<components["schemas"]["LookupResponse"]>("/v1/images/lookup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });
  return response.matches;
}

/** Moves one of the user's images to the top of their gallery, as if it had just been uploaded. */
export function bumpImage(id: number, options: { signal?: AbortSignal } = {}): Promise<ImageOut> {
  return apiFetch<ImageOut>(`/v1/images/${id}/bump`, { method: "POST", signal: options.signal });
}

/** The most ids one `bumpImages` request may carry. */
export const BUMP_LIMIT = 50;

/**
 * Moves several of the user's images to the top of their gallery in one request. The order
 * matters: the last id ends up on top, as if the images had been uploaded one after another.
 * Returns the images that moved, in the order given; an id that is missing or isn't the user's is
 * absent from the answer. At most `BUMP_LIMIT` ids per call.
 */
export async function bumpImages(
  ids: readonly number[],
  options: { signal?: AbortSignal } = {},
): Promise<ImageOut[]> {
  const body: components["schemas"]["BumpRequest"] = { image_ids: [...ids] };
  const response = await apiFetch<components["schemas"]["BumpResponse"]>("/v1/images/bump", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal,
  });
  return response.items;
}

/** What an upload came back with: the image, and whether it is new or one the user already had. */
export interface UploadedImage {
  image: ImageOut;
  /** False when the server found the same file already in the gallery and bumped it instead. */
  created: boolean;
}

export interface UploadOptions {
  /** Called with 0..100 as the file's bytes go out. */
  onProgress?: (percent: number) => void;
  /** Aborting rejects with an `AbortError` DOMException and cancels the request. */
  signal?: AbortSignal;
}

/**
 * Uploads one file. Uses XHR because `fetch` can't report upload progress. The returned image
 * is already stored with its thumbnail; its analysis is still `pending`. The server answers 201
 * for a new image and 200 for one the user already had (nothing is stored, and that image moves to
 * the top), which `created` tells apart.
 */
export async function uploadImage(file: File, options: UploadOptions = {}): Promise<UploadedImage> {
  const { onProgress, signal } = options;
  const token = await accessToken();
  signal?.throwIfAborted();

  return new Promise<UploadedImage>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/v1/images`);
    xhr.setRequestHeader("Accept", "application/json");
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress?.(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100);
        try {
          resolve({ image: JSON.parse(xhr.responseText) as ImageOut, created: xhr.status === 201 });
        } catch {
          reject(
            new ApiError(xhr.status, "invalid_response", "The server sent an unreadable reply."),
          );
        }
        return;
      }
      const apiError = toApiError(xhr.status, xhr.responseText);
      if (xhr.status === 401) {
        void handleUnauthorized().finally(() => reject(apiError));
      } else {
        reject(apiError);
      }
    };
    xhr.onerror = () => reject(networkError());
    xhr.onabort = () => reject(new DOMException("Upload canceled", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });

    const body = new FormData();
    body.append("file", file);
    xhr.send(body);
  });
}
