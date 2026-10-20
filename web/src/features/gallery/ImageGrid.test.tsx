import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import type { ImageOut, ImagePage } from "../../lib/api";
import { ApiError, listImages, PAGE_SIZE } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { MockStore } from "../../mocks/data";
import { ImageGrid } from "./ImageGrid";
import { FakeIntersectionObserver, makeImage } from "./testFixtures";

vi.mock("../../lib/supabase", () => ({ supabase: {} }));
vi.mock("../../components/ui/toast", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  listImages: vi.fn(),
}));

const mockListImages = vi.mocked(listImages);

function pageOf(images: ImageOut[], offset: number): ImagePage {
  const end = offset + PAGE_SIZE;
  return { items: images.slice(offset, end), next_offset: end < images.length ? end : null };
}

/** Serves `listImages` from the 45 seeded mock images, filtered the way the mock API does. */
function serveMockImages() {
  const store = new MockStore();
  mockListImages.mockImplementation(async (query, offset) => pageOf(store.list(query), offset));
  return store;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderGrid(path = "/") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const router = createMemoryRouter([{ path: "/", element: <ImageGrid /> }], {
    initialEntries: [path],
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, queryClient };
}

/** The image tiles, not the Load more / Try again buttons around them. */
const tiles = () => document.querySelectorAll("li button");
const skeletons = () => document.querySelectorAll("li[aria-hidden='true']");

async function reachSentinel() {
  await waitFor(() => expect(FakeIntersectionObserver.live).toHaveLength(1));
  act(() => FakeIntersectionObserver.live[0].report(true));
}

beforeEach(() => {
  FakeIntersectionObserver.instances = [];
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  window.scrollTo = vi.fn();
  mockListImages.mockReset();
});

afterEach(() => vi.unstubAllGlobals());

describe("loading", () => {
  it("shows 20 skeleton tiles until the first page arrives", async () => {
    const firstPage = deferred<ImagePage>();
    mockListImages.mockReturnValue(firstPage.promise);

    renderGrid();

    expect(screen.getByRole("status", { name: "Loading images" })).toBeInTheDocument();
    expect(skeletons()).toHaveLength(20);
    expect(tiles()).toHaveLength(0);

    await act(async () => firstPage.resolve(pageOf(new MockStore().list({ kind: "all" }), 0)));

    await waitFor(() => expect(tiles()).toHaveLength(20));
    expect(skeletons()).toHaveLength(0);
  });

  it("lays the images out in a responsive grid of square tiles", async () => {
    serveMockImages();

    renderGrid();

    const grid = (await screen.findAllByRole("list"))[0];
    expect(grid).toHaveClass(
      "grid-cols-2",
      "sm:grid-cols-3",
      "md:grid-cols-4",
      "lg:grid-cols-5",
      "xl:grid-cols-6",
    );
    expect(within(grid).getAllByRole("button")[0]).toHaveClass("aspect-square");
  });

  it("asks for the first page of the URL's query", async () => {
    serveMockImages();

    renderGrid("/?q=beach");

    await waitFor(() => expect(mockListImages).toHaveBeenCalled());
    expect(mockListImages).toHaveBeenCalledWith(
      { kind: "text", q: "beach" },
      0,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });
});

describe("paging", () => {
  it("loads all 45 mock images in 3 pages as the sentinel scrolls into view", async () => {
    serveMockImages();
    renderGrid();

    await waitFor(() => expect(tiles()).toHaveLength(20));
    await reachSentinel();
    await waitFor(() => expect(tiles()).toHaveLength(40));
    await reachSentinel();
    await waitFor(() => expect(tiles()).toHaveLength(45));

    expect(mockListImages.mock.calls.map(([, offset]) => offset)).toEqual([0, 20, 40]);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(FakeIntersectionObserver.live).toHaveLength(0);
  });

  it("shows 4 skeleton tiles after the loaded ones while the next page is on its way", async () => {
    const store = serveMockImages();
    renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));
    const secondPage = deferred<ImagePage>();
    mockListImages.mockReturnValueOnce(secondPage.promise);

    await reachSentinel();

    await waitFor(() => expect(skeletons()).toHaveLength(4));
    expect(tiles()).toHaveLength(20);
    await act(async () => secondPage.resolve(pageOf(store.list({ kind: "all" }), 20)));
    await waitFor(() => expect(tiles()).toHaveLength(40));
    expect(skeletons()).toHaveLength(0);
  });

  it("has a Load more button for when the observer can't help", async () => {
    serveMockImages();
    renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    await waitFor(() => expect(tiles()).toHaveLength(40));
  });

  it("keeps the Load more button when the browser has no IntersectionObserver", async () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    serveMockImages();
    renderGrid();

    await waitFor(() => expect(tiles()).toHaveLength(20));

    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
  });

  it("shows an image once even when an upload shifted it onto the next page", async () => {
    const before = Array.from({ length: 41 }, (_, index) => makeImage(index + 1));
    const after = [makeImage(100), ...before];
    // The first page is fetched, then someone uploads image 100, which pushes image 20 to offset 20.
    mockListImages.mockImplementation(async (_query, offset) =>
      offset === 0 ? pageOf(before, 0) : pageOf(after, offset),
    );
    renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    // 20 + 20 fetched, one of them a repeat.
    await waitFor(() => expect(tiles()).toHaveLength(39));
  });

  it("offers a retry in place when the next page fails, and keeps what is already shown", async () => {
    const store = serveMockImages();
    renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));
    mockListImages.mockRejectedValueOnce(new ApiError(0, "network_error", "No connection."));

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load more images.");
    expect(tiles()).toHaveLength(20);
    mockListImages.mockImplementation(async (query, offset) => pageOf(store.list(query), offset));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(tiles()).toHaveLength(40));
  });
});

describe("changing the query", () => {
  it("keeps the old results on screen, dimmed with a spinner, instead of flashing skeletons", async () => {
    const store = serveMockImages();
    const { router } = renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));
    const beachResults = deferred<ImagePage>();
    mockListImages.mockImplementationOnce(() => beachResults.promise);

    await act(async () => {
      await router.navigate("/?q=beach");
    });

    expect(tiles()).toHaveLength(20);
    expect(skeletons()).toHaveLength(0);
    expect(screen.getByRole("region", { name: "Images" })).toHaveAttribute("aria-busy", "true");
    expect(await screen.findByText("Updating results…")).toBeInTheDocument();
    expect(screen.getAllByRole("list")[0]).toHaveClass("opacity-60");

    await act(async () =>
      beachResults.resolve(pageOf(store.list({ kind: "text", q: "beach" }), 0)),
    );

    await waitFor(() => expect(screen.queryByText("Updating results…")).not.toBeInTheDocument());
    expect(tiles().length).toBeGreaterThan(0);
    expect(tiles().length).toBeLessThan(20);
    expect(screen.getAllByRole("list")[0]).not.toHaveClass("opacity-60");
  });

  it("shows no spinner when the new results come back quickly", async () => {
    serveMockImages();
    const { router } = renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));

    await act(async () => {
      await router.navigate("/?q=beach");
    });

    await waitFor(() => expect(tiles().length).toBeLessThan(20));
    expect(screen.queryByText("Updating results…")).not.toBeInTheDocument();
  });

  it("scrolls back to the top for a new query, but not when the grid first appears", async () => {
    serveMockImages();
    const { router } = renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));
    expect(window.scrollTo).not.toHaveBeenCalled();

    await act(async () => {
      await router.navigate("/?q=beach");
    });

    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 });
  });

  it("does not scroll or refetch when only the open image changes", async () => {
    serveMockImages();
    const { router } = renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));

    await act(async () => {
      await router.navigate("/?image=3");
    });

    expect(window.scrollTo).not.toHaveBeenCalled();
    expect(mockListImages).toHaveBeenCalledTimes(1);
    expect(tiles()).toHaveLength(20);
  });

  it("shows skeletons for a new query when there were no old results to keep", async () => {
    serveMockImages();
    const { router } = renderGrid("/?q=zzzz");
    expect(await screen.findByText("No matches for “zzzz”")).toBeInTheDocument();
    const nextResults = deferred<ImagePage>();
    mockListImages.mockImplementationOnce(() => nextResults.promise);

    await act(async () => {
      await router.navigate("/?q=beach");
    });

    expect(skeletons()).toHaveLength(20);
    expect(screen.queryByText(/No matches/)).not.toBeInTheDocument();
  });
});

describe("empty states", () => {
  it("invites the first upload when the gallery is empty", async () => {
    mockListImages.mockResolvedValue({ items: [], next_offset: null });

    renderGrid();

    expect(await screen.findByText("Upload your first image")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("says what found nothing, and a button resets the search to all images", async () => {
    serveMockImages();
    const { router } = renderGrid("/?q=zzzz");

    expect(await screen.findByText("No matches for “zzzz”")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show all images" }));

    expect(router.state.location.search).toBe("");
    await waitFor(() => expect(tiles()).toHaveLength(20));
    expect(mockListImages).toHaveBeenLastCalledWith({ kind: "all" }, 0, expect.anything());
  });

  it.each([
    ["/?color=%23aabbcc", "No matches for #aabbcc"],
    ["/?similar_to=999", "No matches similar to image 999"],
  ])("also resets %s to all images", async (path, title) => {
    mockListImages.mockImplementation(async (query, offset) =>
      query.kind === "all" ? pageOf([makeImage(1)], offset) : { items: [], next_offset: null },
    );
    const { router } = renderGrid(path);

    expect(await screen.findByText(title)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show all images" }));

    expect(router.state.location.search).toBe("");
    await waitFor(() => expect(tiles()).toHaveLength(1));
  });
});

describe("errors", () => {
  it("shows the error with a retry button that loads the grid", async () => {
    const store = serveMockImages();
    mockListImages.mockRejectedValueOnce(
      new ApiError(0, "network_error", "Couldn't reach the server. Check your connection."),
    );

    renderGrid();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't reach the server. Check your connection.");
    expect(tiles()).toHaveLength(0);
    mockListImages.mockImplementation(async (query, offset) => pageOf(store.list(query), offset));
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(tiles()).toHaveLength(20));
  });

  it("keeps showing the images when a background refresh fails", async () => {
    mockListImages.mockResolvedValueOnce(
      pageOf([makeImage(1, { status: "pending", tags: [], description: null })], 0),
    );
    renderGrid();
    await screen.findByText("Analyzing…");

    mockListImages.mockRejectedValue(new ApiError(502, "http_502", "Bad gateway."));
    await waitFor(() => expect(mockListImages.mock.calls.length).toBeGreaterThan(1), {
      timeout: 4000,
    });

    expect(screen.getByText("Analyzing…")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  }, 8000);
});

describe("connection and announcements", () => {
  it("toasts a lost connection, even while the older results stay on screen", async () => {
    mockListImages.mockResolvedValueOnce(
      pageOf(new MockStore().list({ kind: "all" }).slice(0, 3), 0),
    );
    const { queryClient } = renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(3));

    mockListImages.mockRejectedValue(
      new ApiError(0, "network_error", "Couldn't reach the server. Check your connection."),
    );
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.images.all }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Couldn't reach the server. Check your connection.",
        { id: "gallery-network-error" },
      ),
    );
    expect(tiles()).toHaveLength(3);
  });

  it("announces a finished analysis in a polite live region, once", async () => {
    mockListImages.mockResolvedValueOnce(
      pageOf([makeImage(1, { status: "processing", tags: [], description: null })], 0),
    );
    const { queryClient } = renderGrid();
    await screen.findByText("Analyzing…");

    mockListImages.mockResolvedValue(pageOf([makeImage(1)], 0));
    await act(() => queryClient.invalidateQueries({ queryKey: queryKeys.images.all }));

    const announcement = await screen.findByText("Analysis finished for photo-1.jpg");
    expect(announcement).toHaveAttribute("aria-live", "polite");
    expect(announcement).toHaveClass("sr-only");
  });
});

describe("polling", () => {
  it("refetches every 2 seconds while an image is analyzing, and flips it to its tags", async () => {
    const analyzing = makeImage(1, { status: "processing", tags: [], description: null });
    const done = makeImage(1, { status: "done", tags: ["lake", "dawn", "mist", "calm"] });
    mockListImages.mockResolvedValueOnce(pageOf([analyzing], 0));
    mockListImages.mockResolvedValue(pageOf([done], 0));

    renderGrid();
    expect(await screen.findByText("Analyzing…")).toBeInTheDocument();
    expect(mockListImages).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(screen.getByText("lake")).toBeInTheDocument(), { timeout: 4000 });

    expect(screen.queryByText("Analyzing…")).not.toBeInTheDocument();
    expect(mockListImages).toHaveBeenCalledTimes(2);
  }, 8000);

  it("makes no further requests once everything is analyzed", async () => {
    serveMockImages();
    renderGrid();
    await waitFor(() => expect(tiles()).toHaveLength(20));

    await new Promise((resolve) => setTimeout(resolve, 2300));

    expect(mockListImages).toHaveBeenCalledTimes(1);
  }, 8000);
});

describe("match badges", () => {
  const matchBadges = () => screen.queryAllByRole("img", { name: /% match$/ });

  it.each([["/?similar_to=45"], ["/?color=%23aabbcc"]])(
    "shows a match percentage on every tile at %s",
    async (path) => {
      serveMockImages();

      renderGrid(path);

      await waitFor(() => expect(tiles().length).toBeGreaterThan(0));
      expect(matchBadges()).toHaveLength(tiles().length);
      expect(matchBadges()[0]).toHaveAccessibleName(/^\d+% match$/);
    },
  );

  it.each([["/"], ["/?q=beach"]])(
    "shows no match percentage at %s, where the scores are not percentages",
    async (path) => {
      serveMockImages();

      renderGrid(path);

      await waitFor(() => expect(tiles().length).toBeGreaterThan(0));
      expect(matchBadges()).toHaveLength(0);
    },
  );
});
