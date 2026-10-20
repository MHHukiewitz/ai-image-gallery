import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getImage } from "../../lib/api";
import { makeImage } from "../gallery/testFixtures";
import { ActiveFilterChip } from "./ActiveFilterChip";
import type { FilterQuery } from "./ActiveFilterChip";

vi.mock("../../lib/supabase", () => ({ supabase: {} }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getImage: vi.fn(),
}));

const mockGetImage = vi.mocked(getImage);

function renderChip(query: FilterQuery, onClear = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ActiveFilterChip query={query} onClear={onClear} />
    </QueryClientProvider>,
  );
  return { onClear, queryClient };
}

beforeEach(() => {
  mockGetImage.mockReset();
});

describe("similar", () => {
  it("shows the thumbnail of the image the search started from", async () => {
    mockGetImage.mockResolvedValue(makeImage(42));
    renderChip({ kind: "similar", imageId: 42 });

    const group = screen.getByRole("group", { name: "Active filter" });
    expect(group).toHaveTextContent("Similar to");
    const thumbnail = await screen.findByRole("img", { name: "photo-42.jpg" });
    expect(thumbnail).toHaveAttribute("src", "https://storage.test/thumb-42.webp?token=first");
    expect(mockGetImage).toHaveBeenCalledWith(42, expect.anything());
  });

  it("names the image by its number while the thumbnail loads", () => {
    mockGetImage.mockReturnValue(new Promise(() => undefined));
    renderChip({ kind: "similar", imageId: 42 });

    expect(screen.getByText("image 42")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("still shows the chip and its × when the image can't be loaded", async () => {
    mockGetImage.mockRejectedValue(new ApiError(404, "not_found", "Image not found."));
    renderChip({ kind: "similar", imageId: 42 });

    await waitFor(() => expect(mockGetImage).toHaveBeenCalled());
    expect(screen.getByText("image 42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear similar images filter" })).toBeEnabled();
  });

  it("calls onClear from the ×", async () => {
    mockGetImage.mockResolvedValue(makeImage(42));
    const { onClear } = renderChip({ kind: "similar", imageId: 42 });

    await userEvent.click(screen.getByRole("button", { name: "Clear similar images filter" }));

    expect(onClear).toHaveBeenCalledTimes(1);
  });
});

describe("color", () => {
  it("shows a swatch of the color and its hex code, without fetching anything", () => {
    renderChip({ kind: "color", hex: "#aabbcc" });

    const group = screen.getByRole("group", { name: "Active filter" });
    expect(group).toHaveTextContent("Color");
    expect(group).toHaveTextContent("#aabbcc");
    expect(screen.getByTestId("color-swatch")).toHaveStyle({ backgroundColor: "#aabbcc" });
    expect(mockGetImage).not.toHaveBeenCalled();
  });

  it("calls onClear from the ×", async () => {
    const { onClear } = renderChip({ kind: "color", hex: "#aabbcc" });

    await userEvent.click(screen.getByRole("button", { name: "Clear color filter" }));

    expect(onClear).toHaveBeenCalledTimes(1);
  });
});
