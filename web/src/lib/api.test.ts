import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  apiFetch,
  deleteImage,
  getImage,
  listImages,
  navigation,
  reanalyzeImage,
  uploadImage,
} from "./api";
import { supabase } from "./supabase";

vi.mock("./supabase", () => ({
  supabase: { auth: { getSession: vi.fn(), signOut: vi.fn() } },
}));

const fetchMock = vi.fn<typeof fetch>();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string, message: string): Response {
  return jsonResponse({ error: { code, message } }, status);
}

function signedIn(token: string | null) {
  vi.mocked(supabase.auth.getSession).mockResolvedValue({
    data: { session: token ? { access_token: token } : null },
    error: null,
  } as Awaited<ReturnType<typeof supabase.auth.getSession>>);
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(supabase.auth.signOut).mockResolvedValue({ error: null });
  vi.spyOn(navigation, "redirectToSignIn").mockImplementation(() => undefined);
  signedIn("test-token");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  fetchMock.mockReset();
  window.history.replaceState(null, "", "/");
});

function lastRequest() {
  const [url, init] = fetchMock.mock.calls.at(-1) ?? [];
  return { url: String(url), init, headers: new Headers(init?.headers) };
}

describe("apiFetch", () => {
  it("sends the access token and returns the parsed body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));

    const body = await apiFetch<{ status: string }>("/v1/images");

    expect(body).toEqual({ status: "ok" });
    expect(lastRequest().url).toBe("/api/v1/images");
    expect(lastRequest().headers.get("Authorization")).toBe("Bearer test-token");
  });

  it("sends no Authorization header without a session", async () => {
    signedIn(null);
    fetchMock.mockResolvedValue(jsonResponse({}));

    await apiFetch("/v1/images");

    expect(lastRequest().headers.has("Authorization")).toBe(false);
  });

  it("maps the error shape to an ApiError", async () => {
    fetchMock.mockResolvedValue(errorResponse(415, "unsupported_type", "Only JPEG and PNG."));

    const failure = await apiFetch("/v1/images").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({
      status: 415,
      code: "unsupported_type",
      message: "Only JPEG and PNG.",
    });
  });

  it("still gives an ApiError when the body isn't our error shape", async () => {
    fetchMock.mockResolvedValue(new Response("<html>Bad gateway</html>", { status: 502 }));

    const failure = await apiFetch("/v1/images").catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 502, code: "http_502" });
  });

  it("reports a network failure as status 0", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const failure = await apiFetch("/v1/images").catch((error: unknown) => error);

    expect(failure).toMatchObject({ status: 0, code: "network_error" });
  });

  it("lets an abort through unchanged", async () => {
    const abort = new DOMException("Aborted", "AbortError");
    fetchMock.mockRejectedValue(abort);

    await expect(apiFetch("/v1/images")).rejects.toBe(abort);
  });

  it("resolves to undefined for a response with no body", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiFetch("/v1/images/1", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("signs out and redirects once on a 401, even with parallel failures", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(errorResponse(401, "unauthorized", "Sign in again.")),
    );

    const results = await Promise.allSettled([apiFetch("/v1/images"), apiFetch("/v1/images/2")]);

    expect(results.map((result) => result.status)).toEqual(["rejected", "rejected"]);
    expect(results[0]).toMatchObject({ reason: { status: 401, code: "unauthorized" } });
    expect(supabase.auth.signOut).toHaveBeenCalledTimes(1);
    expect(supabase.auth.signOut).toHaveBeenCalledWith({ scope: "local" });
    expect(navigation.redirectToSignIn).toHaveBeenCalledTimes(1);
  });

  it("does not redirect when the user is already on the sign-in page", async () => {
    window.history.replaceState(null, "", "/sign-in");
    fetchMock.mockResolvedValue(errorResponse(401, "unauthorized", "Sign in again."));

    await apiFetch("/v1/images").catch(() => undefined);

    expect(supabase.auth.signOut).toHaveBeenCalled();
    expect(navigation.redirectToSignIn).not.toHaveBeenCalled();
  });
});

describe("image endpoints", () => {
  const emptyPage = { items: [], next_offset: null };

  it.each([
    [{ kind: "all" }, "offset=40&limit=20"],
    [{ kind: "text", q: "red barn" }, "offset=40&limit=20&q=red+barn"],
    [{ kind: "similar", imageId: 7 }, "offset=40&limit=20&similar_to=7"],
    [{ kind: "color", hex: "#aabbcc" }, "offset=40&limit=20&color=%23aabbcc"],
  ] as const)("listImages sends %j as %s", async (query, search) => {
    fetchMock.mockResolvedValue(jsonResponse(emptyPage));

    await expect(listImages(query, 40)).resolves.toEqual(emptyPage);

    expect(lastRequest().url).toBe(`/api/v1/images?${search}`);
  });

  it("getImage, deleteImage and reanalyzeImage use the right verbs and paths", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 5 }));
    await getImage(5);
    expect(lastRequest().url).toBe("/api/v1/images/5");
    expect(lastRequest().init?.method).toBeUndefined();

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteImage(5);
    expect(lastRequest().url).toBe("/api/v1/images/5");
    expect(lastRequest().init?.method).toBe("DELETE");

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 202 }));
    await reanalyzeImage(5);
    expect(lastRequest().url).toBe("/api/v1/images/5/reanalyze");
    expect(lastRequest().init?.method).toBe("POST");
  });
});

/** Just enough of XMLHttpRequest for uploadImage; tests drive its events by hand. */
class FakeXhr {
  static instances: FakeXhr[] = [];
  upload: { onprogress: ((event: ProgressEvent) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  status = 0;
  responseText = "";
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: unknown = null;

  constructor() {
    FakeXhr.instances.push(this);
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.onabort?.();
  }
  progress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable } as ProgressEvent);
  }
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = typeof body === "string" ? body : JSON.stringify(body);
    this.onload?.();
  }
}

describe("uploadImage", () => {
  const file = new File(["bytes"], "cat.png", { type: "image/png" });
  const created = { id: 9, filename: "cat.png" };

  beforeEach(() => {
    FakeXhr.instances = [];
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
  });

  /** uploadImage reads the session before it opens the request, so wait for it. */
  async function started(): Promise<FakeXhr> {
    await vi.waitFor(() => expect(FakeXhr.instances).toHaveLength(1));
    return FakeXhr.instances[0];
  }

  it("posts the file as multipart form data with the access token", async () => {
    const upload = uploadImage(file);
    const xhr = await started();

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("/api/v1/images");
    expect(xhr.headers.Authorization).toBe("Bearer test-token");
    expect(xhr.body).toBeInstanceOf(FormData);
    expect((xhr.body as FormData).get("file")).toBeInstanceOf(File);

    xhr.respond(201, created);
    await expect(upload).resolves.toEqual(created);
  });

  it("reports progress as a percentage, finishing at 100", async () => {
    const onProgress = vi.fn();
    const upload = uploadImage(file, { onProgress });
    const xhr = await started();

    xhr.progress(25, 100);
    xhr.progress(1, 3);
    xhr.progress(5, 0);
    xhr.progress(10, 100, false);
    xhr.respond(201, created);
    await upload;

    expect(onProgress.mock.calls.map(([percent]) => percent)).toEqual([25, 33, 100]);
  });

  it("maps an error response to an ApiError", async () => {
    const upload = uploadImage(file);
    const xhr = await started();

    xhr.respond(413, { error: { code: "too_large", message: "Over 10 MB." } });

    await expect(upload).rejects.toMatchObject({
      status: 413,
      code: "too_large",
      message: "Over 10 MB.",
    });
  });

  it("signs out on a 401", async () => {
    const upload = uploadImage(file);
    const xhr = await started();

    xhr.respond(401, { error: { code: "unauthorized", message: "Sign in again." } });

    await expect(upload).rejects.toMatchObject({ status: 401 });
    expect(supabase.auth.signOut).toHaveBeenCalledWith({ scope: "local" });
    expect(navigation.redirectToSignIn).toHaveBeenCalled();
  });

  it("reports a dropped connection as a network error", async () => {
    const upload = uploadImage(file);
    const xhr = await started();

    xhr.onerror?.();

    await expect(upload).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("rejects with an AbortError when the signal aborts", async () => {
    const controller = new AbortController();
    const upload = uploadImage(file, { signal: controller.signal });
    await started();

    controller.abort();

    await expect(upload).rejects.toMatchObject({ name: "AbortError" });
  });

  it("never starts the request if the signal was already aborted", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(uploadImage(file, { signal: controller.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(FakeXhr.instances).toHaveLength(0);
  });
});
