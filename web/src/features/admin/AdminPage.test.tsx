import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { toast } from "../../components/ui/toast";
import { ApiError, getSiteSettings, updateSiteSettings } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { AdminPage } from "./AdminPage";

vi.mock("../../lib/supabase", () => ({ supabase: { auth: {} } }));
vi.mock("../../components/ui/toast", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getSiteSettings: vi.fn(),
  updateSiteSettings: vi.fn(),
}));

const UPDATED_AT = "2026-10-23T09:00:00Z";

function renderPage() {
  // No pause before the hook's own retry, so a test that waits for it doesn't time out.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <AdminPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), queryClient };
}

describe("AdminPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("shows the sign-up switch in its current state", async () => {
    vi.mocked(getSiteSettings).mockResolvedValue({ allow_signups: true, updated_at: UPDATED_AT });
    renderPage();

    const toggle = await screen.findByRole("switch", { name: "Allow new sign-ups" });
    expect(toggle).toBeChecked();
    expect(toggle).toHaveAccessibleDescription("Anyone can create an account.");
    expect(screen.getByRole("heading", { level: 1, name: "Admin settings" })).toBeInTheDocument();
  });

  it("closes sign-ups, shows the server's answer and refreshes the sign-up page's status", async () => {
    vi.mocked(getSiteSettings).mockResolvedValue({ allow_signups: true, updated_at: UPDATED_AT });
    vi.mocked(updateSiteSettings).mockResolvedValue({
      allow_signups: false,
      updated_at: "2026-10-23T10:00:00Z",
    });
    const { user, queryClient } = renderPage();
    queryClient.setQueryData(queryKeys.signups, { allow_signups: true });

    await user.click(await screen.findByRole("switch", { name: "Allow new sign-ups" }));

    expect(updateSiteSettings).toHaveBeenCalledWith({ allow_signups: false });
    const toggle = screen.getByRole("switch", { name: "Allow new sign-ups" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(
      "Nobody can create an account. Existing users can still sign in.",
    );
    expect(toast.success).toHaveBeenCalledWith("New sign-ups are closed.");
    expect(queryClient.getQueryState(queryKeys.signups)?.isInvalidated).toBe(true);
  });

  it("puts the switch back and says why when the change fails", async () => {
    vi.mocked(getSiteSettings).mockResolvedValue({ allow_signups: false, updated_at: UPDATED_AT });
    vi.mocked(updateSiteSettings).mockRejectedValue(
      new ApiError(0, "network_error", "Couldn't reach the server."),
    );
    const { user } = renderPage();

    await user.click(await screen.findByRole("switch", { name: "Allow new sign-ups" }));

    expect(toast.error).toHaveBeenCalledWith("Couldn't reach the server.");
    expect(screen.getByRole("switch", { name: "Allow new sign-ups" })).not.toBeChecked();
  });

  it("tells anyone but an admin that the page isn't theirs, without retrying", async () => {
    vi.mocked(getSiteSettings).mockRejectedValue(
      new ApiError(403, "forbidden", "Only an admin can do this."),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "Admins only" })).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the gallery" })).toHaveAttribute("href", "/");
    expect(getSiteSettings).toHaveBeenCalledOnce();
  });

  it("offers a retry when the settings fail to load", async () => {
    const unavailable = new ApiError(503, "http_503", "Something went wrong on our side.");
    // The hook retries anything but a 403 once on its own, so the page gives up on the second.
    vi.mocked(getSiteSettings)
      .mockRejectedValueOnce(unavailable)
      .mockRejectedValueOnce(unavailable)
      .mockResolvedValueOnce({ allow_signups: true, updated_at: UPDATED_AT });
    const { user } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("switch", { name: "Allow new sign-ups" })).toBeChecked();
  });
});
