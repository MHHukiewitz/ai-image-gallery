import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { acceptShare, deleteShare, listShares } from "../../lib/api";
import type { ShareList, ShareOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { ShareRequests } from "./ShareRequests";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  listShares: vi.fn(),
  acceptShare: vi.fn(),
  deleteShare: vi.fn(),
}));

function makeShare(id: number, overrides: Partial<ShareOut> = {}): ShareOut {
  return {
    id,
    email: `person-${id}@example.test`,
    status: "pending",
    created_at: "2026-10-22T09:00:00Z",
    accepted_at: null,
    ...overrides,
  };
}

function renderRequests(shares: ShareList) {
  vi.mocked(listShares).mockResolvedValue(shares);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <ShareRequests />
    </QueryClientProvider>,
  );
  return { invalidate };
}

describe("ShareRequests", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("shows one notice per pending request, and none for accepted galleries", async () => {
    renderRequests({
      outgoing: [makeShare(9)],
      incoming: [makeShare(1), makeShare(2, { status: "accepted" }), makeShare(3)],
    });

    const region = await screen.findByRole("region", { name: "Share requests" });

    expect(within(region).getByText("person-1@example.test")).toBeInTheDocument();
    expect(within(region).getByText("person-3@example.test")).toBeInTheDocument();
    expect(within(region).queryByText("person-2@example.test")).not.toBeInTheDocument();
    expect(within(region).queryByText("person-9@example.test")).not.toBeInTheDocument();
    expect(within(region).getAllByRole("button", { name: "Accept" })).toHaveLength(2);
  });

  it("renders nothing without pending requests", async () => {
    renderRequests({ outgoing: [], incoming: [makeShare(2, { status: "accepted" })] });

    await waitFor(() => expect(listShares).toHaveBeenCalled());
    expect(screen.queryByRole("region", { name: "Share requests" })).not.toBeInTheDocument();
  });

  it("accepting refreshes the shares and the images, which now hold the owner's", async () => {
    vi.mocked(acceptShare).mockResolvedValue(undefined);
    const { invalidate } = renderRequests({ outgoing: [], incoming: [makeShare(1)] });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Accept" }));

    await waitFor(() => expect(acceptShare).toHaveBeenCalledWith(1));
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.images.all }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.shares });
    expect(toast.success).toHaveBeenCalledWith(
      "You can now see the images of person-1@example.test.",
    );
  });

  it("declining ends the share", async () => {
    vi.mocked(deleteShare).mockResolvedValue(undefined);
    renderRequests({ outgoing: [], incoming: [makeShare(1)] });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Decline" }));

    await waitFor(() => expect(deleteShare).toHaveBeenCalledWith(1));
    expect(acceptShare).not.toHaveBeenCalled();
  });
});
