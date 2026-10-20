import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getImage } from "../../lib/api";
import type { ImagePage } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { makeImage } from "../gallery/testFixtures";
import { SearchBar } from "./SearchBar";
import { SPINNER_DELAY_MS } from "./useSearchSpinner";
import { SEARCH_DEBOUNCE_MS } from "./useSearchText";

vi.mock("../../lib/supabase", () => ({ supabase: {} }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getImage: vi.fn(),
}));

const mockGetImage = vi.mocked(getImage);

function renderBar(path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: (
          <>
            <SearchBar />
            <textarea aria-label="Notes" />
          </>
        ),
      },
    ],
    { initialEntries: [path] },
  );
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  // No delay between actions: user-event's own pauses would wait on the faked timers forever.
  const user = userEvent.setup({ delay: null });
  return { router, queryClient, user };
}

const input = () => screen.getByRole("searchbox", { name: "Search images" });
const advance = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
const search = (router: ReturnType<typeof renderBar>["router"]) => router.state.location.search;

beforeEach(() => {
  vi.useFakeTimers();
  // Testing Library waits on a real timer after each user-event call, unless it finds a `jest`
  // global to advance the faked clock with. Vitest has none, so hand it the equivalent.
  vi.stubGlobal("jest", { advanceTimersByTime: (ms: number) => vi.advanceTimersByTime(ms) });
  mockGetImage.mockReset();
  mockGetImage.mockImplementation(async (id) => makeImage(id));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("typing", () => {
  it("changes the query once, 250 ms after the last keystroke", async () => {
    const { router, user } = renderBar();
    const onNavigate = vi.fn();
    router.subscribe(onNavigate);

    await user.type(input(), "beach");
    await advance(SEARCH_DEBOUNCE_MS - 1);
    expect(search(router)).toBe("");
    expect(onNavigate).not.toHaveBeenCalled();

    await advance(1);
    expect(search(router)).toBe("?q=beach");
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it("replaces the history entry while typing", async () => {
    const { router, user } = renderBar();

    await user.type(input(), "beach");
    await advance(SEARCH_DEBOUNCE_MS);

    expect(router.state.historyAction).toBe("REPLACE");
  });

  it("shows every image again when the input is emptied", async () => {
    const { router, user } = renderBar("/?q=beach");
    expect(input()).toHaveValue("beach");

    await user.clear(input());
    await advance(SEARCH_DEBOUNCE_MS);

    expect(search(router)).toBe("");
  });

  it("applies the text at once on Enter", async () => {
    const { router, user } = renderBar();

    await user.type(input(), "beach{Enter}");

    expect(search(router)).toBe("?q=beach");
  });

  it("replaces a similar filter with the text typed", async () => {
    const { router, user } = renderBar("/?similar_to=7");
    await advance(0);
    expect(screen.getByRole("group", { name: "Active filter" })).toBeInTheDocument();

    await user.type(input(), "dog");
    await advance(SEARCH_DEBOUNCE_MS);

    expect(search(router)).toBe("?q=dog");
    expect(screen.queryByRole("group", { name: "Active filter" })).not.toBeInTheDocument();
  });

  it("keeps a similar filter when a letter is typed and deleted again", async () => {
    const { router, user } = renderBar("/?similar_to=7");

    await user.type(input(), "a{Backspace}");
    await advance(SEARCH_DEBOUNCE_MS);

    expect(search(router)).toBe("?similar_to=7");
  });
});

describe("outside changes", () => {
  it("shows the text of a search set from outside, such as a tag click", async () => {
    const { router } = renderBar();

    await act(() => router.navigate("/?q=sunset"));

    expect(input()).toHaveValue("sunset");
  });

  it("follows back and forward through earlier searches", async () => {
    const { router } = renderBar("/?q=beach");
    await act(() => router.navigate("/?q=sunset"));

    await act(() => router.navigate(-1));
    expect(input()).toHaveValue("beach");

    await act(() => router.navigate(1));
    expect(input()).toHaveValue("sunset");
  });

  it("empties the input when the filter becomes a color", async () => {
    const { router } = renderBar("/?q=beach");

    await act(() => router.navigate("/?color=%23aabbcc"));

    expect(input()).toHaveValue("");
  });
});

describe("keyboard", () => {
  it("focuses the input when / is pressed", async () => {
    const { user } = renderBar();
    expect(input()).not.toHaveFocus();

    await user.keyboard("/");

    expect(input()).toHaveFocus();
    expect(input()).toHaveValue("");
  });

  it("leaves / alone while another text field has focus", async () => {
    const { user } = renderBar();
    const notes = screen.getByRole("textbox", { name: "Notes" });

    await user.click(notes);
    await user.keyboard("a/b");

    expect(notes).toHaveValue("a/b");
    expect(input()).not.toHaveFocus();
  });

  it("doesn't type the / into the input it focuses", async () => {
    const { user } = renderBar("/?q=beach");

    await user.keyboard("/");

    expect(input()).toHaveValue("beach");
  });

  it("clears the input and blurs it on Esc", async () => {
    const { router, user } = renderBar("/?q=beach");

    await user.click(input());
    await user.keyboard("{Escape}");

    expect(input()).toHaveValue("");
    expect(input()).not.toHaveFocus();
    expect(search(router)).toBe("");
  });
});

describe("clear button", () => {
  it("only shows while there is text", async () => {
    const { user } = renderBar();
    expect(screen.queryByRole("button", { name: "Clear search" })).not.toBeInTheDocument();

    await user.type(input(), "b");

    expect(screen.getByRole("button", { name: "Clear search" })).toBeInTheDocument();
  });

  it("clears the text and the query at once and keeps focus in the input", async () => {
    const { router, user } = renderBar("/?q=beach");

    await user.click(screen.getByRole("button", { name: "Clear search" }));

    expect(input()).toHaveValue("");
    expect(input()).toHaveFocus();
    expect(search(router)).toBe("");
  });
});

describe("spinner", () => {
  /** Starts a list fetch that stays in flight until the returned function is called. */
  function fetchImages(queryClient: QueryClient) {
    let finish!: () => void;
    const pending = new Promise<ImagePage>((resolve) => {
      finish = () => resolve({ items: [], next_offset: null });
    });
    void queryClient.fetchQuery({
      queryKey: queryKeys.images.list({ kind: "all" }),
      queryFn: () => pending,
    });
    return finish;
  }

  it("shows while images are being fetched, and goes away after", async () => {
    const { queryClient } = renderBar();
    expect(screen.queryByRole("status", { name: "Searching" })).not.toBeInTheDocument();

    const finish = fetchImages(queryClient);
    await advance(SPINNER_DELAY_MS);
    expect(screen.getByRole("status", { name: "Searching" })).toBeInTheDocument();

    finish();
    await advance(0);
    expect(screen.queryByRole("status", { name: "Searching" })).not.toBeInTheDocument();
  });

  it("doesn't flash for a fetch that finishes quickly", async () => {
    const { queryClient } = renderBar();

    const finish = fetchImages(queryClient);
    await advance(SPINNER_DELAY_MS - 1);
    finish();
    await advance(SPINNER_DELAY_MS);

    expect(screen.queryByRole("status", { name: "Searching" })).not.toBeInTheDocument();
  });
});

describe("filter chip", () => {
  it("shows for a similar search, with an empty input", async () => {
    renderBar("/?similar_to=42");
    await advance(0);

    expect(screen.getByRole("group", { name: "Active filter" })).toHaveTextContent("Similar to");
    expect(screen.getByRole("img", { name: "photo-42.jpg" })).toBeInTheDocument();
    expect(input()).toHaveValue("");
  });

  it("shows for a color search", () => {
    renderBar("/?color=%23aabbcc");

    expect(screen.getByRole("group", { name: "Active filter" })).toHaveTextContent("#aabbcc");
  });

  it("resets a similar search to all images from the ×", async () => {
    const { router, user } = renderBar("/?similar_to=42");

    await user.click(screen.getByRole("button", { name: "Clear similar images filter" }));

    expect(search(router)).toBe("");
    expect(screen.queryByRole("group", { name: "Active filter" })).not.toBeInTheDocument();
  });

  it("resets a color search to all images from the ×", async () => {
    const { router, user } = renderBar("/?color=%23aabbcc");

    await user.click(screen.getByRole("button", { name: "Clear color filter" }));

    expect(search(router)).toBe("");
  });

  it("keeps the open image when the filter is cleared", async () => {
    const { router, user } = renderBar("/?color=%23aabbcc&image=5");

    await user.click(screen.getByRole("button", { name: "Clear color filter" }));

    expect(search(router)).toBe("?image=5");
  });

  it("can be undone with Back, which brings the filter back", async () => {
    const { router, user } = renderBar("/?color=%23aabbcc");
    await user.click(screen.getByRole("button", { name: "Clear color filter" }));

    await act(() => router.navigate(-1));

    expect(screen.getByRole("group", { name: "Active filter" })).toHaveTextContent("#aabbcc");
  });

  it("is absent for text searches and for all images", () => {
    renderBar("/?q=beach");

    expect(screen.queryByRole("group", { name: "Active filter" })).not.toBeInTheDocument();
  });
});
