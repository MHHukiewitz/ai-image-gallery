import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { InfiniteData } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { uploadImage } from "../../lib/api";
import type { ImageOut, ImagePage, UploadOptions } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { UploadZone } from "./UploadZone";

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
}

let pending: PendingUpload[] = [];

function image(id: number): ImageOut {
  return {
    id,
    filename: `photo-${id}.png`,
    uploaded_at: "2026-01-01T00:00:00Z",
    width: 100,
    height: 100,
    thumbnail_url: `https://example.test/${id}.webp`,
    analysis: { status: "pending", tags: [], description: null, colors: [], error: null },
  };
}

function png(name: string, size = 10): File {
  return new File([new Uint8Array(size)], name, { type: "image/png" });
}

function galleryWith(...ids: number[]): InfiniteData<ImagePage, number> {
  return { pages: [{ items: ids.map(image), next_offset: null }], pageParams: [0] };
}

function renderZone(cached?: InfiniteData<ImagePage, number>) {
  const queryClient = new QueryClient();
  if (cached) queryClient.setQueryData(queryKeys.images.list({ kind: "all" }), cached);
  render(
    <QueryClientProvider client={queryClient}>
      <UploadZone />
    </QueryClientProvider>,
  );
  return { queryClient };
}

const fileInput = () => document.querySelector<HTMLInputElement>("input[type=file]")!;
// jsdom's `userEvent.upload` would otherwise drop files the input's `accept` excludes, which
// hides the rejection path that a drop takes in a real browser.
const user = userEvent.setup({ applyAccept: false });

/** The part of a browser's DataTransfer that react-dropzone reads. */
function dragFiles(files: File[]) {
  const items = files.map((file) => ({ kind: "file", type: file.type, getAsFile: () => file }));
  return { dataTransfer: { files, items, types: ["Files"] } };
}

beforeEach(() => {
  pending = [];
  vi.mocked(uploadImage).mockImplementation(
    (file, options = {}) =>
      new Promise<ImageOut>((resolve, reject) => {
        pending.push({ file, options, resolve });
        options.signal?.addEventListener("abort", () =>
          reject(new DOMException("Upload canceled", "AbortError")),
        );
      }),
  );
});

afterEach(() => vi.clearAllMocks());

describe("layout", () => {
  it("shows a large drop area with a Browse button when the gallery is empty", () => {
    renderZone(galleryWith());

    expect(screen.getByRole("heading", { name: "Drop your photos here" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Browse files" })).toBeInTheDocument();
    expect(screen.getByText(/up to 10 MB each/)).toBeInTheDocument();
  });

  it("shows a compact bar when the gallery has images", () => {
    renderZone(galleryWith(1));

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload images" })).toBeInTheDocument();
  });

  it("shows the compact bar until the grid has loaded, so a returning user sees no hero flash", () => {
    renderZone();

    expect(screen.getByRole("button", { name: "Upload images" })).toBeInTheDocument();
  });

  it("switches from the hero to the bar once the first image lands in the cache", async () => {
    const { queryClient } = renderZone(galleryWith());
    expect(screen.getByRole("button", { name: "Browse files" })).toBeInTheDocument();

    act(() => {
      queryClient.setQueryData(queryKeys.images.list({ kind: "all" }), galleryWith(1));
    });

    expect(await screen.findByRole("button", { name: "Upload images" })).toBeInTheDocument();
  });

  it("only accepts JPEG and PNG, several at a time, so a phone opens its photo picker", () => {
    renderZone(galleryWith(1));

    expect(fileInput()).toHaveAttribute("accept", "image/jpeg,image/png");
    expect(fileInput()).toHaveAttribute("multiple");
  });
});

describe("uploading", () => {
  it("uploads 6 chosen files as 3 uploading and 3 waiting", async () => {
    renderZone(galleryWith(1));

    await user.upload(
      fileInput(),
      Array.from({ length: 6 }, (_, index) => png(`photo-${index + 1}.png`)),
    );

    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(6);
    expect(rows.filter((row) => row.getAttribute("data-status") === "uploading")).toHaveLength(3);
    expect(rows.filter((row) => row.getAttribute("data-status") === "queued")).toHaveLength(3);
    expect(pending).toHaveLength(3);
    expect(screen.getByText("Uploading 6 images")).toBeInTheDocument();
  });

  it("moves the progress bar and clears a finished file after a moment", async () => {
    renderZone(galleryWith(1));
    await user.upload(fileInput(), png("beach.png"));
    await screen.findByText("Uploading 0%");

    act(() => pending[0].options.onProgress?.(55));
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "55");

    await act(async () => pending[0].resolve(image(2)));
    expect(screen.getByText("Uploaded")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("beach.png")).not.toBeInTheDocument(), {
      timeout: 4000,
    });
  });

  it("cancels an upload in progress from its Cancel button", async () => {
    renderZone(galleryWith(1));
    await user.upload(fileInput(), png("beach.png"));

    await user.click(await screen.findByRole("button", { name: "Cancel upload of beach.png" }));

    expect(pending[0].options.signal?.aborted).toBe(true);
    expect(screen.getByText("Canceled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry upload of beach.png" })).toBeInTheDocument();
  });
});

describe("rejected files", () => {
  it("toasts a wrong type by name and uploads nothing for it", async () => {
    renderZone(galleryWith(1));

    await user.upload(fileInput(), new File(["x"], "cat.gif", { type: "image/gif" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(toast.error).toHaveBeenCalledWith(
      "cat.gif isn't a JPEG or PNG image, so it was skipped.",
    );
    expect(uploadImage).not.toHaveBeenCalled();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("toasts a file over the limit with the limit stated", async () => {
    renderZone(galleryWith(1));

    await user.upload(fileInput(), png("huge.png", 10 * 1024 * 1024 + 1));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "huge.png is larger than the 10 MB limit, so it was skipped.",
      ),
    );
    expect(uploadImage).not.toHaveBeenCalled();
  });

  it("toasts once per rejected file and still uploads the good ones from the same drop", async () => {
    renderZone(galleryWith(1));

    await user.upload(fileInput(), [
      png("ok.png"),
      new File(["x"], "a.gif", { type: "image/gif" }),
      new File(["x"], "b.webp", { type: "image/webp" }),
    ]);

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(2));
    expect(vi.mocked(toast.error).mock.calls.map(([message]) => message)).toEqual([
      "a.gif isn't a JPEG or PNG image, so it was skipped.",
      "b.webp isn't a JPEG or PNG image, so it was skipped.",
    ]);
    expect(await screen.findByText("ok.png")).toBeInTheDocument();
    expect(pending).toHaveLength(1);
  });
});

describe("dropping", () => {
  it("accepts a drop on the bar", async () => {
    renderZone(galleryWith(1));
    const bar = screen.getByText(/up to 10 MB each/).closest("div")!.parentElement!;

    fireEvent.drop(bar, dragFiles([png("dropped.png")]));

    expect(await screen.findByText("dropped.png")).toBeInTheDocument();
  });

  it("covers the page while a file is dragged over it and takes the drop anywhere", async () => {
    renderZone(galleryWith(1));
    const cover = screen.getByText("Drop images to upload");
    expect(cover).not.toBeVisible();

    fireEvent.dragEnter(document.body, dragFiles([png("dragged.png")]));
    await waitFor(() => expect(cover).toBeVisible());
    fireEvent.drop(cover, dragFiles([png("dragged.png")]));

    expect(await screen.findByText("dragged.png")).toBeInTheDocument();
    expect(pending).toHaveLength(1);
    await waitFor(() => expect(cover).not.toBeVisible());
  });

  it("doesn't cover the page for something that isn't a file, such as selected text", () => {
    renderZone(galleryWith(1));

    fireEvent.dragEnter(document.body, {
      dataTransfer: { files: [], items: [], types: ["text/plain"] },
    });

    expect(screen.getByText("Drop images to upload")).not.toBeVisible();
  });

  it("takes the cover away when the drag leaves the page", async () => {
    renderZone(galleryWith(1));
    const cover = screen.getByText("Drop images to upload");
    fireEvent.dragEnter(document.body, dragFiles([png("a.png")]));
    await waitFor(() => expect(cover).toBeVisible());

    fireEvent.dragLeave(document.body, dragFiles([png("a.png")]));

    await waitFor(() => expect(cover).not.toBeVisible());
  });
});

describe("hero", () => {
  it("uploads what the Browse files button's picker returns", async () => {
    renderZone(galleryWith());
    const clickInput = vi.spyOn(fileInput(), "click");

    await user.click(screen.getByRole("button", { name: "Browse files" }));
    expect(clickInput).toHaveBeenCalledTimes(1);

    await user.upload(fileInput(), png("first.png"));
    const row = (await screen.findByText("first.png")).closest("li")!;
    expect(within(row).getByRole("progressbar")).toBeInTheDocument();
  });
});
