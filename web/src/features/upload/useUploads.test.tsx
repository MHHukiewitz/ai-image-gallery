import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import {
  ApiError,
  BUMP_LIMIT,
  bumpImages,
  LOOKUP_LIMIT,
  lookupImages,
  uploadImage,
} from "../../lib/api";
import type { ImageOut, ImagePage, UploadOptions } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { hashFile } from "./hashFile";
import { summarizeQueue } from "./uploadQueue";
import { AUTO_DISMISS_MS, DUPLICATE_DISMISS_MS, useUploads } from "./useUploads";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("./hashFile", () => ({ hashFile: vi.fn() }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  uploadImage: vi.fn(),
  lookupImages: vi.fn(),
  bumpImages: vi.fn(),
}));

interface PendingUpload {
  file: File;
  options: UploadOptions;
  /** Answers like the API: 201 for a new image, 200 (`created: false`) for one the user had. */
  resolve: (image: ImageOut, created?: boolean) => void;
  reject: (error: unknown) => void;
}

let pending: PendingUpload[] = [];

function image(id: number, filename: string): ImageOut {
  return {
    id,
    filename,
    uploaded_at: "2026-01-01T00:00:00Z",
    width: 100,
    height: 100,
    thumbnail_url: `https://example.test/${id}.webp`,
    analysis: { status: "pending", tags: [], description: null, colors: [], error: null },
  };
}

function files(count: number): File[] {
  return Array.from(
    { length: count },
    (_, index) => new File(["x"], `photo-${index + 1}.png`, { type: "image/png" }),
  );
}

/** A stand-in for the SHA-256 of a file: the real one is covered in hashFile.test.ts. */
const sha = (name: string) => `sha:${name}`;

/** Lets the drop check and the effects that follow it run: hashing, lookup and bumps are all promises. */
const settle = () =>
  act(async () => {
    for (let turn = 0; turn < 30; turn += 1) await Promise.resolve();
  });

/** Drops files on the hook and waits until the drop check has released them. */
async function drop(result: { current: ReturnType<typeof useUploads> }, dropped: File[]) {
  act(() => result.current.addFiles(dropped));
  await settle();
}

/** Settles a request the way `uploadImage` does when its signal is aborted. */
function rejectOnAbort(upload: PendingUpload) {
  upload.options.signal?.addEventListener("abort", () =>
    upload.reject(new DOMException("Upload canceled", "AbortError")),
  );
}

function setup() {
  const queryClient = new QueryClient();
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, invalidate, ...renderHook(() => useUploads(), { wrapper }) };
}

const statuses = (items: readonly { status: string }[]) => items.map((item) => item.status);

beforeEach(() => {
  pending = [];
  vi.mocked(hashFile).mockImplementation((file) => Promise.resolve(sha(file.name)));
  vi.mocked(lookupImages).mockResolvedValue({});
  // Like the API: every id the user owns comes back, in the order asked.
  vi.mocked(bumpImages).mockImplementation((ids) =>
    Promise.resolve(ids.map((id) => image(id, `old-${id}.png`))),
  );
  vi.mocked(uploadImage).mockImplementation(
    (file, options = {}) =>
      new Promise((resolveUpload, reject) => {
        const resolve = (uploaded: ImageOut, created = true) =>
          resolveUpload({ image: uploaded, created });
        const upload = { file, options, resolve, reject };
        rejectOnAbort(upload);
        pending.push(upload);
      }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useUploads", () => {
  it("uploads 2 files at once and starts the next as each one finishes", async () => {
    const { result } = setup();

    await drop(result, files(6));

    expect(statuses(result.current.items)).toEqual([
      "uploading",
      "uploading",
      "queued",
      "queued",
      "queued",
      "queued",
    ]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-2.png"]);

    await act(async () => pending[1].resolve(image(2, "photo-2.png")));

    expect(pending).toHaveLength(3);
    expect(pending[2].file.name).toBe("photo-3.png");
    expect(statuses(result.current.items).filter((status) => status === "uploading")).toHaveLength(
      2,
    );
  });

  it("shows progress while a file uploads", async () => {
    const { result } = setup();
    await drop(result, files(1));

    act(() => pending[0].options.onProgress?.(40));

    expect(result.current.items[0]).toMatchObject({ status: "uploading", progress: 40 });
  });

  it("adds a finished image to the grid's cache, refetches the lists and clears itself after 3 s", async () => {
    vi.useFakeTimers();
    const { result, queryClient, invalidate } = setup();
    const existing: InfiniteData<ImagePage, number> = {
      pages: [{ items: [image(1, "old.png")], next_offset: null }],
      pageParams: [0],
    };
    queryClient.setQueryData(queryKeys.images.list({ kind: "all" }), existing);
    await drop(result, files(1));

    await act(async () => pending[0].resolve(image(2, "photo-1.png")));

    const cached = queryClient.getQueryData<InfiniteData<ImagePage>>(
      queryKeys.images.list({ kind: "all" }),
    );
    expect(cached?.pages[0].items.map(({ id }) => id)).toEqual([2, 1]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.images.all });
    expect(result.current.items[0]).toMatchObject({ status: "done", progress: 100 });

    act(() => void vi.advanceTimersByTime(AUTO_DISMISS_MS - 1));
    expect(result.current.items).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(1));
    expect(result.current.items).toHaveLength(0);
  });

  it("toasts the server's message, keeps the failed item and re-uploads it on retry", async () => {
    const { result } = setup();
    await drop(result, files(1));

    await act(async () =>
      pending[0].reject(
        new ApiError(413, "too_large", "That file is larger than the upload limit."),
      ),
    );

    expect(toast.error).toHaveBeenCalledWith(
      "photo-1.png: That file is larger than the upload limit.",
    );
    expect(result.current.items[0]).toMatchObject({
      status: "error",
      error: "That file is larger than the upload limit.",
    });

    act(() => result.current.retry(result.current.items[0].id));

    expect(pending).toHaveLength(2);
    expect(result.current.items[0]).toMatchObject({ status: "uploading", progress: 0 });
    await act(async () => pending[1].resolve(image(7, "photo-1.png")));
    expect(result.current.items[0].status).toBe("done");
  });

  it("uses a generic message when the failure isn't an API error", async () => {
    const { result } = setup();
    await drop(result, files(1));

    await act(async () => pending[0].reject(new Error("boom")));

    expect(result.current.items[0].error).toBe("The upload failed. Please try again.");
  });

  it("aborts an upload in progress on cancel, quietly, and frees its slot", async () => {
    const { result } = setup();
    await drop(result, files(3));
    const [first] = result.current.items;

    await act(async () => result.current.cancel(first.id));

    expect(pending[0].options.signal?.aborted).toBe(true);
    expect(pending[1].options.signal?.aborted).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
    expect(result.current.items[0].status).toBe("canceled");
    expect(pending[2].file.name).toBe("photo-3.png");
  });

  it("never uploads a queued file that was canceled", async () => {
    const { result } = setup();
    await drop(result, files(3));

    act(() => result.current.cancel(result.current.items[2].id));
    await act(async () => pending[0].resolve(image(1, "photo-1.png")));

    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-2.png"]);
    expect(result.current.items[2].status).toBe("canceled");
  });

  it("can retry a canceled upload", async () => {
    const { result } = setup();
    await drop(result, files(1));
    await act(async () => result.current.cancel(result.current.items[0].id));

    act(() => result.current.retry(result.current.items[0].id));

    expect(pending).toHaveLength(2);
    expect(pending[1].options.signal?.aborted).toBe(false);
    expect(result.current.items[0].status).toBe("uploading");
  });

  it("dismisses an item on request and doesn't dismiss it again later", async () => {
    vi.useFakeTimers();
    const { result } = setup();
    await drop(result, files(1));
    await act(async () => pending[0].resolve(image(1, "photo-1.png")));

    act(() => result.current.dismiss(result.current.items[0].id));

    expect(result.current.items).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts what is still uploading when the page goes away", async () => {
    const { result, unmount } = setup();
    await drop(result, files(2));

    unmount();

    expect(pending.every((upload) => upload.options.signal?.aborted)).toBe(true);
  });
});

describe("useUploads: files the user already has", () => {
  const galleryKey = queryKeys.images.list({ kind: "all" });

  function cacheWith(queryClient: QueryClient, ...ids: number[]) {
    queryClient.setQueryData<InfiniteData<ImagePage, number>>(galleryKey, {
      pages: [{ items: ids.map((id) => image(id, `old-${id}.png`)), next_offset: null }],
      pageParams: [0],
    });
  }

  const cachedIds = (queryClient: QueryClient) =>
    queryClient
      .getQueryData<InfiniteData<ImagePage>>(galleryKey)
      ?.pages[0].items.map(({ id }) => id);

  it("holds a dropped file back as 'checking' until it has been looked up", async () => {
    let answer: (matches: Record<string, number>) => void = () => undefined;
    vi.mocked(lookupImages).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const { result } = setup();

    await drop(result, files(2));

    expect(statuses(result.current.items)).toEqual(["checking", "checking"]);
    expect(pending).toHaveLength(0);

    answer({});
    await settle();

    expect(statuses(result.current.items)).toEqual(["uploading", "uploading"]);
  });

  it("looks the whole drop up in one request by the files' hashes, in drop order", async () => {
    const { result } = setup();

    await drop(result, files(3));

    expect(lookupImages).toHaveBeenCalledOnce();
    expect(vi.mocked(lookupImages).mock.calls[0][0]).toEqual([
      sha("photo-1.png"),
      sha("photo-2.png"),
      sha("photo-3.png"),
    ]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-2.png"]);
  });

  it("looks up in chunks of 50", async () => {
    const { result } = setup();
    const many = Array.from({ length: 120 }, (_, index) => new File(["x"], `many-${index}.png`));

    await drop(result, many);

    const sizes = vi.mocked(lookupImages).mock.calls.map(([hashes]) => hashes.length);
    expect(LOOKUP_LIMIT).toBe(50);
    expect(sizes).toEqual([50, 50, 20]);
    expect(vi.mocked(lookupImages).mock.calls.flatMap(([hashes]) => hashes)).toEqual(
      many.map((file) => sha(file.name)),
    );
  });

  it("bumps a match instead of uploading it, and uploads the rest", async () => {
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-2.png")]: 22 });
    vi.mocked(bumpImages).mockResolvedValue([image(22, "photo-2.png")]);
    const { result } = setup();

    await drop(result, files(3));

    expect(bumpImages).toHaveBeenCalledOnce();
    expect(vi.mocked(bumpImages).mock.calls[0][0]).toEqual([22]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-3.png"]);
    expect(result.current.items[1]).toMatchObject({
      status: "duplicate",
      progress: 100,
      imageId: 22,
    });
    expect(statuses(result.current.items)).toEqual(["uploading", "duplicate", "uploading"]);
  });

  it("moves the bumped image to the top of the grid's cache and refetches the lists", async () => {
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-1.png")]: 1 });
    vi.mocked(bumpImages).mockResolvedValue([image(1, "photo-1.png")]);
    const { result, queryClient, invalidate } = setup();
    cacheWith(queryClient, 3, 2, 1);

    await drop(result, files(1));

    expect(cachedIds(queryClient)).toEqual([1, 3, 2]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.images.all });
  });

  it("doesn't refetch, or touch the cache, when nothing was bumped", async () => {
    const { result, queryClient, invalidate } = setup();
    cacheWith(queryClient, 3);

    await drop(result, files(1));

    expect(cachedIds(queryClient)).toEqual([3]);
    expect(invalidate).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("toasts a summary of the files that were already there", async () => {
    vi.mocked(lookupImages).mockResolvedValue({
      [sha("photo-1.png")]: 11,
      [sha("photo-3.png")]: 33,
    });
    const { result } = setup();

    await drop(result, files(3));

    expect(toast.success).toHaveBeenCalledOnce();
    expect(toast.success).toHaveBeenCalledWith("2 already in your gallery, moved to top");
  });

  it("bumps the duplicates of a drop in one request, in drop order", async () => {
    vi.mocked(lookupImages).mockResolvedValue({
      [sha("photo-1.png")]: 11,
      [sha("photo-2.png")]: 22,
      [sha("photo-3.png")]: 33,
    });
    const { result, queryClient } = setup();
    cacheWith(queryClient, 22, 11, 5);

    await drop(result, files(3));

    expect(vi.mocked(bumpImages).mock.calls.map(([ids]) => ids)).toEqual([[11, 22, 33]]);
    // The last file of the drop is on top, as it would be after three uploads.
    expect(cachedIds(queryClient)).toEqual([33, 22, 11, 5]);
    expect(statuses(result.current.items)).toEqual(["duplicate", "duplicate", "duplicate"]);
  });

  it("orders the grid by the drop even when the answer comes back in another order", async () => {
    vi.mocked(lookupImages).mockResolvedValue({
      [sha("photo-1.png")]: 11,
      [sha("photo-2.png")]: 22,
    });
    vi.mocked(bumpImages).mockResolvedValue([image(22, "x"), image(11, "x")]);
    const { result, queryClient } = setup();
    cacheWith(queryClient, 5);

    await drop(result, files(2));

    expect(cachedIds(queryClient)).toEqual([22, 11, 5]);
  });

  it("sends one bump request per 50 duplicates, one after the other", async () => {
    const many = Array.from({ length: 120 }, (_, index) => new File(["x"], `many-${index}.png`));
    const idOf = (hash: string) => 1000 + many.findIndex((file) => sha(file.name) === hash);
    vi.mocked(lookupImages).mockImplementation((hashes) =>
      Promise.resolve(Object.fromEntries(hashes.map((hash) => [hash, idOf(hash)]))),
    );
    const settleBump: Array<() => void> = [];
    vi.mocked(bumpImages).mockImplementation(
      (ids) =>
        new Promise<ImageOut[]>((resolve) =>
          settleBump.push(() => resolve(ids.map((id) => image(id, "x")))),
        ),
    );
    const { result, queryClient } = setup();
    cacheWith(queryClient, 5);

    await drop(result, many);

    // The first chunk is out. The next waits for its answer, so a later chunk lands on top.
    expect(BUMP_LIMIT).toBe(50);
    expect(vi.mocked(bumpImages).mock.calls).toHaveLength(1);
    settleBump[0]();
    await settle();
    expect(vi.mocked(bumpImages).mock.calls).toHaveLength(2);
    settleBump[1]();
    await settle();
    settleBump[2]();
    await settle();

    const sizes = vi.mocked(bumpImages).mock.calls.map(([ids]) => ids.length);
    expect(sizes).toEqual([50, 50, 20]);
    expect(vi.mocked(bumpImages).mock.calls.flatMap(([ids]) => ids)).toEqual(
      many.map((_, index) => 1000 + index),
    );
    expect(lookupImages).toHaveBeenCalledTimes(3);
    expect(statuses(result.current.items)).toEqual(many.map(() => "duplicate"));
    expect(cachedIds(queryClient)?.slice(0, 3)).toEqual([1119, 1118, 1117]);
    expect(toast.success).toHaveBeenCalledWith("120 already in your gallery, moved to top");
  });

  it("makes a single bump request for fewer than 50 duplicates", async () => {
    const some = files(40);
    vi.mocked(lookupImages).mockResolvedValue(
      Object.fromEntries(some.map((file, index) => [sha(file.name), 100 + index])),
    );
    const { result } = setup();

    await drop(result, some);

    expect(bumpImages).toHaveBeenCalledOnce();
    expect(vi.mocked(bumpImages).mock.calls[0][0]).toHaveLength(40);
    expect(lookupImages).toHaveBeenCalledOnce();
    expect(pending).toHaveLength(0);
  });

  it("shows how many files of a drop have been hashed", async () => {
    const hashers: Array<() => void> = [];
    vi.mocked(hashFile).mockImplementation(
      (file) =>
        new Promise((resolve) => {
          // The second file can't be hashed: it still counts as read.
          hashers.push(() => resolve(file.name === "photo-2.png" ? null : sha(file.name)));
        }),
    );
    vi.mocked(lookupImages).mockReturnValue(new Promise(() => undefined));
    const { result } = setup();

    act(() => result.current.addFiles(files(3)));
    await settle();
    expect(summarizeQueue(result.current.items)).toBe("Checking 0 of 3…");

    // Hashing is one file at a time, so each file starts when the one before it is done.
    hashers[0]();
    await settle();
    expect(summarizeQueue(result.current.items)).toBe("Checking 1 of 3…");
    hashers[1]();
    await settle();
    expect(summarizeQueue(result.current.items)).toBe("Checking 2 of 3…");
    hashers[2]();
    await settle();

    // All hashed: now it waits for the lookup.
    expect(summarizeQueue(result.current.items)).toBe("Checking 3 images");
  });

  it("collapses identical files within a drop and uploads the first of them", async () => {
    vi.mocked(hashFile).mockImplementation((file) =>
      Promise.resolve(file.name === "photo-2.png" ? sha("photo-1.png") : sha(file.name)),
    );
    const { result } = setup();

    await drop(result, files(3));

    expect(result.current.items.map((item) => item.file.name)).toEqual([
      "photo-1.png",
      "photo-3.png",
    ]);
    expect(vi.mocked(lookupImages).mock.calls[0][0]).toEqual([
      sha("photo-1.png"),
      sha("photo-3.png"),
    ]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-3.png"]);
    expect(toast.success).toHaveBeenCalledWith("1 identical file in this drop was skipped");
  });

  it("reports repeats and bumped files in one toast", async () => {
    vi.mocked(hashFile).mockImplementation((file) =>
      Promise.resolve(file.name === "photo-3.png" ? sha("photo-1.png") : sha(file.name)),
    );
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-2.png")]: 2 });
    vi.mocked(bumpImages).mockResolvedValue([image(2, "old.png")]);
    const { result } = setup();

    await drop(result, files(3));

    expect(toast.success).toHaveBeenCalledOnce();
    expect(toast.success).toHaveBeenCalledWith(
      "1 already in your gallery, moved to top; 1 identical file in this drop was skipped",
    );
  });

  it("uploads everything when the lookup fails, because the server catches duplicates too", async () => {
    vi.mocked(lookupImages).mockRejectedValue(new ApiError(500, "http_500", "Server error."));
    const { result } = setup();

    await drop(result, files(2));

    expect(statuses(result.current.items)).toEqual(["uploading", "uploading"]);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("uploads a file that can't be hashed without asking about it", async () => {
    vi.mocked(hashFile).mockResolvedValue(null);
    const { result } = setup();

    await drop(result, files(2));

    expect(lookupImages).not.toHaveBeenCalled();
    expect(pending).toHaveLength(2);
  });

  it("uploads a matched file after all when its bump fails, for example when it was deleted", async () => {
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-1.png")]: 1 });
    vi.mocked(bumpImages).mockRejectedValue(new ApiError(500, "http_500", "Server error."));
    const { result } = setup();

    await drop(result, files(1));

    expect(statuses(result.current.items)).toEqual(["uploading"]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png"]);
    expect(toast.error).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("uploads a matched file the bump left out, because it was deleted since the lookup", async () => {
    vi.mocked(lookupImages).mockResolvedValue({
      [sha("photo-1.png")]: 1,
      [sha("photo-2.png")]: 2,
    });
    // Image 1 is gone: the API leaves it out instead of failing the batch.
    vi.mocked(bumpImages).mockResolvedValue([image(2, "old.png")]);
    const { result } = setup();

    await drop(result, files(2));

    expect(statuses(result.current.items)).toEqual(["uploading", "duplicate"]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png"]);
    expect(toast.success).toHaveBeenCalledWith("1 already in your gallery, moved to top");
  });

  it("uploads the files of a chunk that failed and still bumps the other chunks", async () => {
    const many = Array.from({ length: 60 }, (_, index) => new File(["x"], `many-${index}.png`));
    vi.mocked(lookupImages).mockImplementation((hashes) =>
      Promise.resolve(Object.fromEntries(hashes.map((hash, index) => [hash, 1000 + index]))),
    );
    vi.mocked(bumpImages).mockRejectedValueOnce(new ApiError(500, "http_500", "Server error."));
    const { result } = setup();

    await drop(result, many);

    expect(bumpImages).toHaveBeenCalledTimes(2);
    const counts = statuses(result.current.items).reduce<Record<string, number>>(
      (tally, status) => ({ ...tally, [status]: (tally[status] ?? 0) + 1 }),
      {},
    );
    // 50 files of the failed chunk go on to upload (2 at a time), 10 were bumped.
    expect(counts).toEqual({ duplicate: 10, uploading: 2, queued: 48 });
    expect(toast.success).toHaveBeenCalledWith("10 already in your gallery, moved to top");
  });

  it("doesn't send a file canceled while an earlier chunk was being bumped", async () => {
    const many = Array.from({ length: 60 }, (_, index) => new File(["x"], `many-${index}.png`));
    vi.mocked(lookupImages).mockImplementation((hashes) =>
      Promise.resolve(Object.fromEntries(hashes.map((hash, index) => [hash, 1000 + index]))),
    );
    const settleBump: Array<() => void> = [];
    vi.mocked(bumpImages).mockImplementation(
      (ids) =>
        new Promise<ImageOut[]>((resolve) =>
          settleBump.push(() => resolve(ids.map((id) => image(id, "x")))),
        ),
    );
    const { result } = setup();
    await drop(result, many);

    act(() => result.current.cancel(result.current.items[55].id));
    settleBump[0]();
    await settle();
    settleBump[1]();
    await settle();

    const second = vi.mocked(bumpImages).mock.calls[1][0];
    expect(second).toHaveLength(9);
    expect(second).not.toContain(1055);
    expect(result.current.items[55].status).toBe("canceled");
  });

  it("never bumps or uploads a file canceled while the drop was being checked", async () => {
    let answer: (matches: Record<string, number>) => void = () => undefined;
    vi.mocked(lookupImages).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const { result } = setup();
    await drop(result, files(2));

    act(() => result.current.cancel(result.current.items[0].id));
    answer({ [sha("photo-1.png")]: 1, [sha("photo-2.png")]: 2 });
    await settle();

    expect(vi.mocked(bumpImages).mock.calls.map(([ids]) => ids)).toEqual([[2]]);
    expect(statuses(result.current.items)).toEqual(["canceled", "duplicate"]);
    expect(pending).toHaveLength(0);
  });

  it("stops the lookup and the bumps when the page goes away", async () => {
    vi.mocked(lookupImages).mockReturnValue(new Promise(() => undefined));
    const { result, unmount } = setup();
    await drop(result, files(1));

    unmount();

    expect(vi.mocked(lookupImages).mock.calls[0][1]?.signal?.aborted).toBe(true);
  });

  it("stops the bumps when the page goes away", async () => {
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-1.png")]: 1 });
    vi.mocked(bumpImages).mockReturnValue(new Promise(() => undefined));
    const { result, unmount } = setup();
    await drop(result, files(1));

    unmount();

    expect(vi.mocked(bumpImages).mock.calls[0][1]?.signal?.aborted).toBe(true);
  });

  it("treats a 200 answer to an upload as a duplicate: top of the cache, not 'done'", async () => {
    const { result, queryClient, invalidate } = setup();
    cacheWith(queryClient, 3, 2, 9);
    await drop(result, files(1));

    await act(async () => pending[0].resolve(image(9, "first-upload.png"), false));

    expect(result.current.items[0]).toMatchObject({
      status: "duplicate",
      progress: 100,
      imageId: 9,
    });
    expect(cachedIds(queryClient)).toEqual([9, 3, 2]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.images.all });
  });

  it("frees the upload slot when the server says a file was a duplicate", async () => {
    const { result } = setup();
    await drop(result, files(3));
    expect(pending).toHaveLength(2);

    await act(async () => pending[0].resolve(image(9, "first-upload.png"), false));

    expect(pending.map((upload) => upload.file.name)).toEqual([
      "photo-1.png",
      "photo-2.png",
      "photo-3.png",
    ]);
  });

  it("clears a duplicate after 8 s, longer than a finished upload, so View can be tapped", async () => {
    vi.useFakeTimers();
    vi.mocked(lookupImages).mockResolvedValue({ [sha("photo-1.png")]: 1 });
    vi.mocked(bumpImages).mockResolvedValue([image(1, "old.png")]);
    const { result } = setup();
    await drop(result, files(1));
    expect(result.current.items[0].status).toBe("duplicate");

    act(() => void vi.advanceTimersByTime(AUTO_DISMISS_MS));
    expect(result.current.items).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(DUPLICATE_DISMISS_MS - AUTO_DISMISS_MS - 1));
    expect(result.current.items).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(1));
    expect(result.current.items).toHaveLength(0);
  });
});

describe("useUploads: concurrency", () => {
  const originalMatchMedia = window.matchMedia;

  function pointerIs(kind: "coarse" | "fine") {
    window.matchMedia = ((query: string) => ({
      matches: kind === "coarse" && query === "(pointer: coarse)",
    })) as typeof window.matchMedia;
  }

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it("uploads one file at a time on a touch device, starting the next as each finishes", async () => {
    pointerIs("coarse");
    const { result } = setup();

    await drop(result, files(3));

    expect(statuses(result.current.items)).toEqual(["uploading", "queued", "queued"]);
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png"]);

    await act(async () => pending[0].resolve(image(1, "photo-1.png")));
    expect(pending.map((upload) => upload.file.name)).toEqual(["photo-1.png", "photo-2.png"]);
    await act(async () => pending[1].resolve(image(2, "photo-2.png")));
    expect(pending).toHaveLength(3);
  });

  it("uploads two at a time with a mouse", async () => {
    pointerIs("fine");
    const { result } = setup();

    await drop(result, files(3));

    expect(statuses(result.current.items)).toEqual(["uploading", "uploading", "queued"]);
  });
});

describe("useUploads when the daily quota refuses an upload", () => {
  const LIMIT_MESSAGE = "You've reached today's upload limit of 300 images. Try again tomorrow.";
  const refused = () => new ApiError(429, "quota_exceeded", LIMIT_MESSAGE);

  it("fails the file with the server's message, stops the queue and toasts once", async () => {
    const { result } = setup();
    await drop(result, files(5));

    await act(async () => pending[0].reject(refused()));
    // The other upload was already in flight, so it is refused too: still one toast.
    await act(async () => pending[1].reject(refused()));
    await settle();

    expect(statuses(result.current.items)).toEqual([
      "error",
      "error",
      "queued",
      "queued",
      "queued",
    ]);
    expect(result.current.items[0]).toMatchObject({ status: "error", error: LIMIT_MESSAGE });
    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(LIMIT_MESSAGE);
    expect(result.current.paused).toBe(true);
    // Nothing is sent again, and nothing new starts on its own.
    expect(uploadImage).toHaveBeenCalledTimes(2);
  });

  it("shows the queue as paused, with the files still waiting", async () => {
    const { result } = setup();
    await drop(result, files(4));
    await act(async () => pending[0].reject(refused()));
    await act(async () => pending[1].reject(refused()));

    expect(summarizeQueue(result.current.items, result.current.paused)).toBe(
      "Upload limit reached, 2 images waiting, 2 uploads failed",
    );
  });

  it("doesn't pause for a failure that is not the quota", async () => {
    const { result } = setup();
    await drop(result, files(3));

    await act(async () => pending[0].reject(new ApiError(413, "too_large", "Too big.")));

    expect(result.current.paused).toBe(false);
    expect(pending.map((upload) => upload.file.name)).toEqual([
      "photo-1.png",
      "photo-2.png",
      "photo-3.png",
    ]);
    expect(toast.error).toHaveBeenCalledWith("photo-1.png: Too big.");
  });

  it("starts the queue again when the user retries a file", async () => {
    const { result } = setup();
    await drop(result, files(5));
    await act(async () => pending[0].reject(refused()));
    await act(async () => pending[1].reject(refused()));

    act(() => result.current.retry(result.current.items[0].id));

    expect(result.current.paused).toBe(false);
    expect(result.current.items[0]).toMatchObject({ status: "uploading", progress: 0 });
    expect(pending[2].file.name).toBe("photo-1.png");
    await act(async () => pending[2].resolve(image(9, "photo-1.png")));
    expect(result.current.items[0].status).toBe("done");
  });

  it("starts the queue again when the user drops new files", async () => {
    const { result } = setup();
    await drop(result, files(4));
    await act(async () => pending[0].reject(refused()));
    await act(async () => pending[1].reject(refused()));

    await drop(result, [new File(["y"], "later.png", { type: "image/png" })]);

    expect(result.current.paused).toBe(false);
    expect(pending.slice(2).map((upload) => upload.file.name)).toEqual([
      "photo-3.png",
      "photo-4.png",
    ]);
  });
});
