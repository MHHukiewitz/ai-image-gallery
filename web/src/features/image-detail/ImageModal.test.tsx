import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { ApiError, deleteImage, getImage, listImages, reanalyzeImage } from "../../lib/api";
import type { AnalysisOut, ImageOut } from "../../lib/api";
import { useSelectedImage } from "../../lib/galleryQuery";
import { queryKeys } from "../../lib/queryKeys";
import { ImageModal } from "./ImageModal";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getImage: vi.fn(),
  listImages: vi.fn(),
  deleteImage: vi.fn(),
  reanalyzeImage: vi.fn(),
}));

type UserEvent = ReturnType<typeof userEvent.setup>;

function makeImage(
  analysis: Partial<AnalysisOut> = {},
  overrides: Partial<ImageOut> = {},
): ImageOut {
  return {
    id: 7,
    filename: "beach-07.jpg",
    uploaded_at: "2026-10-17T10:00:00Z",
    width: 1600,
    height: 1067,
    thumbnail_url: "https://storage.test/thumb-7.webp?token=first",
    original_url: "https://storage.test/original-7.jpg?token=first",
    analysis: {
      status: "done",
      tags: ["beach", "sunset", "ocean"],
      description: "A golden sunset over a quiet beach.",
      colors: ["#aabbcc", "#112233"],
      error: null,
      ...analysis,
    },
    ...overrides,
  };
}

/** The grid's cards open the modal the same way, so the test does too. */
function OpenImage({ id }: { id: number }) {
  const { open } = useSelectedImage();
  return (
    <button type="button" onClick={() => open(id)}>
      Open image {id}
    </button>
  );
}

function renderGallery() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: (
          <>
            <OpenImage id={7} />
            <ImageModal />
          </>
        ),
      },
    ],
    { initialEntries: ["/"] },
  );
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, opener: screen.getByRole("button", { name: "Open image 7" }) };
}

/** Opens the modal and waits for the dialog to be named, which is the filename once loaded. */
async function openModal(user: UserEvent, name = "beach-07.jpg") {
  await user.click(screen.getByRole("button", { name: "Open image 7" }));
  return screen.findByRole("dialog", { name });
}

function searchParams(router: ReturnType<typeof renderGallery>["router"]) {
  return new URLSearchParams(router.state.location.search);
}

function expectModalClosed() {
  return waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
}

describe("ImageModal", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getImage).mockResolvedValue(makeImage());
  });

  it("opens for the image in the URL and shows its details", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();

    const dialog = await openModal(user);

    expect(router.state.location.search).toBe("?image=7");
    expect(getImage).toHaveBeenCalledWith(
      7,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(within(dialog).getByText("A golden sunset over a quiet beach.")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "beach" })).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Filter by color #aabbcc" }),
    ).toBeInTheDocument();
  });

  it("closes on Escape and gives focus back to the card that opened it", async () => {
    const { router, opener } = renderGallery();
    const user = userEvent.setup();
    await openModal(user);

    await user.keyboard("{Escape}");

    await expectModalClosed();
    expect(router.state.location.search).toBe("");
    // Radix hands focus back on a timer after the dialog unmounts, so wait for it.
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("closes when the back button is pressed", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();
    await openModal(user);

    await act(async () => {
      await router.navigate(-1);
    });

    await expectModalClosed();
  });

  it("keeps keyboard focus inside the dialog", async () => {
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    for (let step = 0; step < 15; step++) {
      await user.tab();
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
  });

  it("searches for a tag and closes the modal", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    await user.click(within(dialog).getByRole("button", { name: "sunset" }));

    await expectModalClosed();
    expect(router.state.location.search).toBe("?q=sunset");
  });

  it("goes Back to the unfiltered grid after a tag search, not back into the modal", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    await user.click(within(dialog).getByRole("button", { name: "sunset" }));
    await expectModalClosed();

    await act(async () => {
      await router.navigate(-1);
    });

    expect(router.state.location.search).toBe("");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("filters by a color swatch and closes the modal", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    await user.click(within(dialog).getByRole("button", { name: "Filter by color #aabbcc" }));

    await expectModalClosed();
    expect(searchParams(router).get("color")).toBe("#aabbcc");
    expect(searchParams(router).has("image")).toBe(false);
  });

  it("finds similar images and closes the modal", async () => {
    const { router } = renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    await user.click(within(dialog).getByRole("button", { name: "Find similar" }));

    await expectModalClosed();
    expect(searchParams(router).get("similar_to")).toBe("7");
    expect(searchParams(router).has("image")).toBe(false);
  });

  it("holds Find similar until the analysis is done", async () => {
    vi.mocked(getImage).mockResolvedValue(
      makeImage({ status: "pending", tags: [], description: null, colors: [] }),
    );
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    expect(within(dialog).getByRole("button", { name: "Find similar" })).toBeDisabled();
  });

  it("shows the analysis as running, then picks up the result by polling", async () => {
    vi.mocked(getImage)
      .mockResolvedValueOnce(
        makeImage({ status: "pending", tags: [], description: null, colors: [] }),
      )
      .mockResolvedValue(
        makeImage({}, { original_url: "https://storage.test/original-7.jpg?token=second" }),
      );
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    const picture = within(dialog).getByRole("img", {
      name: "beach-07.jpg",
    });
    expect(within(dialog).getByText("Analyzing…")).toBeInTheDocument();

    await within(dialog).findByRole("button", { name: "beach" }, { timeout: 3500 });

    expect(getImage).toHaveBeenCalledTimes(2);
    // Each poll signs the URL again. The picture keeps the first one rather than downloading it again.
    expect(picture).toHaveAttribute("src", "https://storage.test/original-7.jpg?token=first");
  });

  it("retries a failed analysis and refreshes the image", async () => {
    vi.mocked(getImage)
      .mockResolvedValueOnce(
        makeImage(
          {
            status: "failed",
            tags: [],
            description: null,
            error: "The analysis service didn't respond. You can retry.",
          },
          {},
        ),
      )
      .mockResolvedValue(makeImage({ status: "pending", tags: [], description: null }));
    vi.mocked(reanalyzeImage).mockResolvedValue(undefined);
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    const alert = within(dialog).getByRole("alert");
    expect(alert).toHaveTextContent("Analysis failed");
    expect(alert).toHaveTextContent("didn't respond");

    await user.click(within(dialog).getByRole("button", { name: "Retry analysis" }));

    expect(reanalyzeImage).toHaveBeenCalledWith(7);
    expect(await within(dialog).findByText("Analyzing…")).toBeInTheDocument();
    expect(getImage).toHaveBeenCalledTimes(2);
  });

  it("shows Image not found without retrying, and closes from there", async () => {
    vi.mocked(getImage).mockRejectedValue(new ApiError(404, "not_found", "Image not found."));
    const { router } = renderGallery();
    const user = userEvent.setup();
    await openModal(user, "Image details");

    const heading = await screen.findByRole("heading", { name: "Image not found" });
    expect(getImage).toHaveBeenCalledTimes(1);

    await user.click(
      within(heading.parentElement as HTMLElement).getByRole("button", { name: "Close" }),
    );

    await expectModalClosed();
    expect(router.state.location.search).toBe("");
  });

  it("toasts a lost connection, so the failure is not missed", async () => {
    vi.mocked(getImage).mockRejectedValue(
      new ApiError(0, "network_error", "Couldn't reach the server. Check your connection."),
    );
    renderGallery();
    const user = userEvent.setup();
    await openModal(user, "Image details");

    await waitFor(
      () =>
        expect(toast.error).toHaveBeenCalledWith(
          "Couldn't reach the server. Check your connection.",
          { id: "image-network-error" },
        ),
      { timeout: 3000 },
    );
  });

  it("does not toast a server error, which the error state already shows", async () => {
    vi.mocked(getImage).mockRejectedValue(new ApiError(500, "http_500", "Something went wrong."));
    renderGallery();
    const user = userEvent.setup();
    await openModal(user, "Image details");

    await screen.findByRole("heading", { name: "Couldn't load this image" }, { timeout: 3000 });
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("shows the thumbnail, blurred, until the original has loaded", async () => {
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    const picture = within(dialog).getByRole("img", {
      name: "A golden sunset over a quiet beach.",
    });
    const placeholder = dialog.querySelector<HTMLImageElement>('img[aria-hidden="true"]');

    expect(picture).toHaveAttribute("src", "https://storage.test/original-7.jpg?token=first");
    expect(picture).toHaveClass("opacity-0");
    expect(placeholder).toHaveAttribute("src", "https://storage.test/thumb-7.webp?token=first");
    expect(placeholder).toHaveClass("blur-xl");
    expect(placeholder).not.toHaveClass("opacity-0");

    fireEvent.load(picture);

    expect(picture).toHaveClass("opacity-100");
    expect(placeholder).toHaveClass("opacity-0");
  });

  it("shows a placeholder, and loads nothing, when the original has no URL", async () => {
    vi.mocked(getImage).mockResolvedValue(makeImage({}, { original_url: "" }));
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    expect(within(dialog).getByTestId("image-placeholder")).toBeInTheDocument();
    expect(within(dialog).getByRole("img", { name: "Image unavailable" })).toBeInTheDocument();
    expect(
      within(dialog).getByRole("img", { name: "A golden sunset over a quiet beach." }),
    ).not.toHaveAttribute("src");
  });

  it("shows no placeholder while the original is there", async () => {
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    expect(within(dialog).queryByTestId("image-placeholder")).not.toBeInTheDocument();
  });

  it("shows a placeholder when the original fails to load", async () => {
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    const picture = within(dialog).getByRole("img", {
      name: "A golden sunset over a quiet beach.",
    });

    fireEvent.error(picture);

    expect(within(dialog).getByTestId("image-placeholder")).toBeInTheDocument();
  });

  it("polls until the original has a URL, then drops the placeholder", async () => {
    vi.mocked(getImage)
      .mockResolvedValueOnce(makeImage({}, { original_url: "", thumbnail_url: "" }))
      .mockResolvedValue(makeImage());
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    expect(within(dialog).getByTestId("image-placeholder")).toBeInTheDocument();

    await waitFor(
      () => expect(within(dialog).queryByTestId("image-placeholder")).not.toBeInTheDocument(),
      { timeout: 3500 },
    );

    expect(getImage).toHaveBeenCalledTimes(2);
    expect(
      within(dialog).getByRole("img", { name: "A golden sunset over a quiet beach." }),
    ).toHaveAttribute("src", "https://storage.test/original-7.jpg?token=first");
  });

  it("asks before deleting, then closes the modal and says so", async () => {
    vi.mocked(deleteImage).mockResolvedValue(undefined);
    const { router } = renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);

    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete this image?" });
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Delete this image?" })).not.toBeInTheDocument(),
    );
    expect(deleteImage).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "beach-07.jpg" })).toBeInTheDocument();
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Delete" })).toHaveFocus(),
    );

    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    const confirmAgain = await screen.findByRole("dialog", { name: "Delete this image?" });
    await user.click(within(confirmAgain).getByRole("button", { name: "Delete image" }));

    await expectModalClosed();
    expect(deleteImage).toHaveBeenCalledTimes(1);
    expect(deleteImage).toHaveBeenCalledWith(7);
    // The deleted image isn't fetched again, which would only get a 404.
    expect(getImage).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("Deleted beach-07.jpg");
    expect(router.state.location.search).toBe("");
  });

  it("keeps the confirmation open and reports a delete that fails", async () => {
    vi.mocked(deleteImage).mockRejectedValue(
      new ApiError(500, "http_500", "Something went wrong on our side. Please try again."),
    );
    renderGallery();
    const user = userEvent.setup();
    const dialog = await openModal(user);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete this image?" });

    await user.click(within(confirm).getByRole("button", { name: "Delete image" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Something went wrong on our side. Please try again.",
      ),
    );
    expect(confirm).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Delete image" })).toBeEnabled();
    expect(toast.success).not.toHaveBeenCalled();
  });
});

/** An image as the list endpoint returns it, with the original the detail endpoint would add. */
function listItem(id: number): ImageOut {
  return makeImage(
    {},
    {
      id,
      filename: `photo-${id}.jpg`,
      original_url: `https://storage.test/original-${id}.jpg?token=first`,
    },
  );
}

/**
 * The modal over a grid whose cache holds `ids` in that order, as the grid leaves it. `more` says
 * the grid has another page the modal can fetch.
 */
function renderGridModal(ids: number[], current: number, more = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(queryKeys.images.list({ kind: "all" }), {
    pages: [{ items: ids.map(listItem), next_offset: more ? ids.length : null }],
    pageParams: [0],
  });
  const router = createMemoryRouter([{ path: "/", element: <ImageModal /> }], {
    initialEntries: [`/?image=${current}`],
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, queryClient };
}

/**
 * A one-finger drag, as the browser reports it: a touch down at `from`, lifted at `to`. Both lists
 * are filled in because Radix's scroll lock reads `changedTouches` even from a touch start.
 */
function drag(target: Element, from: { x: number; y: number }, to: { x: number; y: number }) {
  const down = { clientX: from.x, clientY: from.y };
  const up = { clientX: to.x, clientY: to.y };
  fireEvent.touchStart(target, { touches: [down], changedTouches: [down] });
  fireEvent.touchEnd(target, { touches: [], changedTouches: [up] });
}

describe("ImageModal through the grid", () => {
  // The alt text of the full-size picture, the same for every listed image in these tests.
  const PICTURE = "A golden sunset over a quiet beach.";

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getImage).mockImplementation(async (id) => listItem(id));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("steps with ← and → through the grid's order, replacing the history entry", async () => {
    const user = userEvent.setup();
    const { router } = renderGridModal([4, 9, 2], 9);
    await screen.findByRole("dialog", { name: "photo-9.jpg" });

    await user.keyboard("{ArrowRight}");
    await screen.findByRole("dialog", { name: "photo-2.jpg" });
    expect(searchParams(router).get("image")).toBe("2");
    expect(router.state.historyAction).toBe("REPLACE");

    await user.keyboard("{ArrowLeft}");
    await screen.findByRole("dialog", { name: "photo-9.jpg" });
    await user.keyboard("{ArrowLeft}");
    await screen.findByRole("dialog", { name: "photo-4.jpg" });
    expect(searchParams(router).get("image")).toBe("4");
  });

  it("moves with the Previous and Next buttons, which are disabled at the ends", async () => {
    const user = userEvent.setup();
    const { router } = renderGridModal([4, 9, 2], 4);
    await screen.findByRole("dialog", { name: "photo-4.jpg" });
    expect(screen.getByRole("button", { name: "Previous image" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Next image" }));
    await screen.findByRole("dialog", { name: "photo-9.jpg" });
    await user.click(screen.getByRole("button", { name: "Next image" }));
    await screen.findByRole("dialog", { name: "photo-2.jpg" });

    expect(screen.getByRole("button", { name: "Next image" })).toBeDisabled();
    expect(searchParams(router).get("image")).toBe("2");
  });

  it("does nothing past either end of the grid", async () => {
    const user = userEvent.setup();
    const { router } = renderGridModal([4, 9, 2], 4);
    await screen.findByRole("dialog", { name: "photo-4.jpg" });

    await user.keyboard("{ArrowLeft}");
    expect(searchParams(router).get("image")).toBe("4");

    await user.keyboard("{ArrowRight}");
    await screen.findByRole("dialog", { name: "photo-9.jpg" });
    await user.keyboard("{ArrowRight}");
    await screen.findByRole("dialog", { name: "photo-2.jpg" });
    await user.keyboard("{ArrowRight}");

    expect(searchParams(router).get("image")).toBe("2");
    expect(listImages).not.toHaveBeenCalled();
  });

  it("swipes left for the next image and right for the previous, and ignores short or vertical drags", async () => {
    const { router } = renderGridModal([4, 9, 2], 9);
    const dialog = await screen.findByRole("dialog", { name: "photo-9.jpg" });
    const pictureOf = () => within(dialog).getByRole("img", { name: PICTURE });

    drag(pictureOf(), { x: 300, y: 100 }, { x: 200, y: 110 });
    await screen.findByRole("dialog", { name: "photo-2.jpg" });
    expect(searchParams(router).get("image")).toBe("2");

    drag(pictureOf(), { x: 200, y: 100 }, { x: 300, y: 100 });
    await screen.findByRole("dialog", { name: "photo-9.jpg" });
    expect(searchParams(router).get("image")).toBe("9");

    drag(pictureOf(), { x: 300, y: 100 }, { x: 270, y: 100 });
    drag(pictureOf(), { x: 300, y: 100 }, { x: 240, y: 260 });
    expect(searchParams(router).get("image")).toBe("9");
  });

  it("announces its place in the grid, in a live region that stays put as it moves", async () => {
    const user = userEvent.setup();
    renderGridModal([4, 9, 2], 9);
    const counter = await screen.findByText("Image 2 of 3");
    expect(counter).toHaveAttribute("aria-live", "polite");

    await user.keyboard("{ArrowRight}");

    expect(await screen.findByText("Image 3 of 3")).toBe(counter);
  });

  it("marks the count as partial while the grid has more pages to load", async () => {
    renderGridModal([4, 9], 9, true);

    expect(await screen.findByText("Image 2 of 2+")).toBeInTheDocument();
  });

  it("fetches the next page from the last loaded image, then moves to its first image", async () => {
    vi.mocked(listImages).mockResolvedValue({ items: [listItem(5)], next_offset: null });
    const user = userEvent.setup();
    const { router } = renderGridModal([4, 9], 9, true);
    await screen.findByRole("dialog", { name: "photo-9.jpg" });

    await user.keyboard("{ArrowRight}");

    await waitFor(() => expect(searchParams(router).get("image")).toBe("5"));
    expect(listImages).toHaveBeenCalledWith(
      { kind: "all" },
      2,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("does not step while the delete confirmation is open", async () => {
    const user = userEvent.setup();
    const { router } = renderGridModal([4, 9, 2], 9);
    const dialog = await screen.findByRole("dialog", { name: "photo-9.jpg" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await screen.findByRole("dialog", { name: "Delete this image?" });

    await user.keyboard("{ArrowRight}");

    expect(searchParams(router).get("image")).toBe("9");
  });

  it("loads the originals of the neighbors before the reader steps to them", async () => {
    const loaded: string[] = [];
    vi.stubGlobal(
      "Image",
      class FakeImage {
        set src(url: string) {
          loaded.push(url);
        }
      },
    );

    renderGridModal([4, 9, 2], 9);
    await screen.findByRole("dialog", { name: "photo-9.jpg" });

    await waitFor(() =>
      expect(loaded).toEqual(
        expect.arrayContaining([
          "https://storage.test/original-4.jpg?token=first",
          "https://storage.test/original-2.jpg?token=first",
        ]),
      ),
    );
    expect(getImage).toHaveBeenCalledWith(4, expect.anything());
    expect(getImage).toHaveBeenCalledWith(2, expect.anything());
  });
});
