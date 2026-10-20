import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getImage } from "../../lib/api";
import type { ImageOut } from "../../lib/api";
import { ActiveFilterRow } from "./ActiveFilterRow";

vi.mock("../../lib/supabase", () => ({ supabase: {} }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getImage: vi.fn(),
}));

const mockGetImage = vi.mocked(getImage);
const chip = () => screen.queryByRole("group", { name: "Active filter" });

/** The image a similarity filter starts from, as the detail endpoint returns it. */
function image(id: number): ImageOut {
  return {
    id,
    filename: `photo-${id}.jpg`,
    uploaded_at: "2026-10-20T10:00:00Z",
    width: 1600,
    height: 1067,
    thumbnail_url: `https://storage.test/thumb-${id}.webp`,
    analysis: { status: "done", tags: [], description: null, colors: [], error: null },
  };
}

function renderRow(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/", element: <ActiveFilterRow /> }], {
    initialEntries: [path],
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router };
}

beforeEach(() => {
  mockGetImage.mockReset();
  mockGetImage.mockImplementation(async (id) => image(id));
});

describe("ActiveFilterRow", () => {
  it("shows the similar-image filter", () => {
    renderRow("/?similar_to=7");

    expect(chip()).toHaveTextContent("Similar to");
  });

  it("shows the color filter", () => {
    renderRow("/?color=aabbcc");

    expect(chip()).toHaveTextContent("#aabbcc");
  });

  it.each([["/"], ["/?q=beach"]])("renders nothing at %s", (path) => {
    renderRow(path);

    expect(chip()).not.toBeInTheDocument();
  });

  it("clears the filter back to every image from its ×", async () => {
    const user = userEvent.setup();
    const { router } = renderRow("/?similar_to=7");

    await user.click(screen.getByRole("button", { name: "Clear similar images filter" }));

    expect(router.state.location.search).toBe("");
  });
});
