import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { ApiError, uploadImage } from "../../lib/api";
import type { ImageOut, ImagePage, UploadOptions } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { AUTO_DISMISS_MS, useUploads } from "./useUploads";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  uploadImage: vi.fn(),
}));

interface PendingUpload {
  file: File;
  options: UploadOptions;
  resolve: (image: ImageOut) => void;
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
  vi.mocked(uploadImage).mockImplementation(
    (file, options = {}) =>
      new Promise<ImageOut>((resolve, reject) => {
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
  it("uploads 3 files at once and starts the next as each one finishes", async () => {
    const { result } = setup();

    act(() => result.current.addFiles(files(6)));

    expect(statuses(result.current.items)).toEqual([
      "uploading",
      "uploading",
      "uploading",
      "queued",
      "queued",
      "queued",
    ]);
    expect(pending.map((upload) => upload.file.name)).toEqual([
      "photo-1.png",
      "photo-2.png",
      "photo-3.png",
    ]);

    await act(async () => pending[1].resolve(image(2, "photo-2.png")));

    expect(pending).toHaveLength(4);
    expect(pending[3].file.name).toBe("photo-4.png");
    expect(statuses(result.current.items).filter((status) => status === "uploading")).toHaveLength(
      3,
    );
  });

  it("shows progress while a file uploads", () => {
    const { result } = setup();
    act(() => result.current.addFiles(files(1)));

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
    act(() => result.current.addFiles(files(1)));

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
    act(() => result.current.addFiles(files(1)));

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
    act(() => result.current.addFiles(files(1)));

    await act(async () => pending[0].reject(new Error("boom")));

    expect(result.current.items[0].error).toBe("The upload failed. Please try again.");
  });

  it("aborts an upload in progress on cancel, quietly, and frees its slot", async () => {
    const { result } = setup();
    act(() => result.current.addFiles(files(4)));
    const [first] = result.current.items;

    await act(async () => result.current.cancel(first.id));

    expect(pending[0].options.signal?.aborted).toBe(true);
    expect(pending[1].options.signal?.aborted).toBe(false);
    expect(toast.error).not.toHaveBeenCalled();
    expect(result.current.items[0].status).toBe("canceled");
    expect(pending[3].file.name).toBe("photo-4.png");
  });

  it("never uploads a queued file that was canceled", async () => {
    const { result } = setup();
    act(() => result.current.addFiles(files(4)));

    act(() => result.current.cancel(result.current.items[3].id));
    await act(async () => pending[0].resolve(image(1, "photo-1.png")));

    expect(pending.map((upload) => upload.file.name)).toEqual([
      "photo-1.png",
      "photo-2.png",
      "photo-3.png",
    ]);
    expect(result.current.items[3].status).toBe("canceled");
  });

  it("can retry a canceled upload", async () => {
    const { result } = setup();
    act(() => result.current.addFiles(files(1)));
    await act(async () => result.current.cancel(result.current.items[0].id));

    act(() => result.current.retry(result.current.items[0].id));

    expect(pending).toHaveLength(2);
    expect(pending[1].options.signal?.aborted).toBe(false);
    expect(result.current.items[0].status).toBe("uploading");
  });

  it("dismisses an item on request and doesn't dismiss it again later", async () => {
    vi.useFakeTimers();
    const { result } = setup();
    act(() => result.current.addFiles(files(1)));
    await act(async () => pending[0].resolve(image(1, "photo-1.png")));

    act(() => result.current.dismiss(result.current.items[0].id));

    expect(result.current.items).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("aborts what is still uploading when the page goes away", () => {
    const { result, unmount } = setup();
    act(() => result.current.addFiles(files(2)));

    unmount();

    expect(pending.every((upload) => upload.options.signal?.aborted)).toBe(true);
  });
});
