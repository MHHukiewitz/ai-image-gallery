import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { ApiError, deleteShare, listShares, shareGallery } from "../../lib/api";
import type { ShareList, ShareOut } from "../../lib/api";
import { ShareGalleryDialog } from "./ShareGalleryDialog";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  listShares: vi.fn(),
  shareGallery: vi.fn(),
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

async function openDialog(shares: ShareList) {
  vi.mocked(listShares).mockResolvedValue(shares);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ShareGalleryDialog />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Share gallery" }));
  const dialog = await screen.findByRole("dialog", { name: "Share your gallery" });
  return { user, dialog };
}

describe("ShareGalleryDialog", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("lists whom the gallery is shared with, marking requests not accepted yet", async () => {
    const { dialog } = await openDialog({
      outgoing: [makeShare(1), makeShare(2, { status: "accepted" })],
      incoming: [],
    });

    const list = await within(dialog).findByRole("region", { name: "Shared with" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("person-1@example.test"),
      expect.stringContaining("person-2@example.test"),
    ]);
    expect(within(rows[0]).getByText("Pending")).toBeInTheDocument();
    expect(within(rows[1]).queryByText("Pending")).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("region", { name: "Galleries shared with you" }),
    ).not.toBeInTheDocument();
  });

  it("says so when the gallery is shared with no one", async () => {
    const { dialog } = await openDialog({ outgoing: [], incoming: [] });

    expect(
      await within(dialog).findByText("You haven't shared your gallery with anyone yet."),
    ).toBeInTheDocument();
  });

  it("shares with the address typed in and clears the field", async () => {
    vi.mocked(shareGallery).mockResolvedValue(makeShare(3, { email: "friend@example.test" }));
    const { user, dialog } = await openDialog({ outgoing: [], incoming: [] });
    const field = within(dialog).getByLabelText("E-mail address");

    await user.type(field, " friend@example.test ");
    await user.click(within(dialog).getByRole("button", { name: "Share" }));

    await waitFor(() => expect(shareGallery).toHaveBeenCalledWith("friend@example.test"));
    await waitFor(() => expect(field).toHaveValue(""));
    expect(toast.success).toHaveBeenCalledWith("Asked friend@example.test to accept your gallery.");
  });

  it("shows the server's refusal next to the field", async () => {
    vi.mocked(shareGallery).mockRejectedValue(
      new ApiError(404, "no_account", "No account uses this e-mail address."),
    );
    const { user, dialog } = await openDialog({ outgoing: [], incoming: [] });
    const field = within(dialog).getByLabelText("E-mail address");

    await user.type(field, "nobody@example.test");
    await user.click(within(dialog).getByRole("button", { name: "Share" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("No account uses this e-mail address.");
    expect(field).toHaveAccessibleDescription("No account uses this e-mail address.");
    expect(field).toHaveValue("nobody@example.test");
  });

  it("asks for an address before sending anything", async () => {
    const { user, dialog } = await openDialog({ outgoing: [], incoming: [] });

    await user.click(within(dialog).getByRole("button", { name: "Share" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Enter an e-mail address.");
    expect(shareGallery).not.toHaveBeenCalled();
  });

  it("removes someone from the gallery", async () => {
    vi.mocked(deleteShare).mockResolvedValue(undefined);
    const { user, dialog } = await openDialog({ outgoing: [makeShare(1)], incoming: [] });

    await user.click(
      await within(dialog).findByRole("button", { name: "Remove person-1@example.test" }),
    );

    await waitFor(() => expect(deleteShare).toHaveBeenCalledWith(1));
    expect(toast.success).toHaveBeenCalledWith(
      "person-1@example.test no longer sees your gallery.",
    );
  });

  it("lists the galleries the user accepted, and lets them leave one", async () => {
    vi.mocked(deleteShare).mockResolvedValue(undefined);
    const { user, dialog } = await openDialog({
      outgoing: [],
      incoming: [makeShare(4, { status: "accepted" }), makeShare(5)],
    });

    const received = await within(dialog).findByRole("region", {
      name: "Galleries shared with you",
    });
    // A pending request is answered from the banner on the gallery page, not here.
    expect(within(received).queryByText("person-5@example.test")).not.toBeInTheDocument();
    await user.click(within(received).getByRole("button", { name: "Leave person-4@example.test" }));

    await waitFor(() => expect(deleteShare).toHaveBeenCalledWith(4));
  });
});
