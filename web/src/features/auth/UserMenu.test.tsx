import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Session } from "@supabase/supabase-js";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserMenu } from "./UserMenu";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
  signOut: vi.fn(),
}));

vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));

const session = {
  access_token: "test-token",
  user: { id: "user-1", email: "ada@example.com" },
} as unknown as Session;

function renderMenu(queryClient: QueryClient) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<UserMenu />} />
          <Route path="/sign-in" element={<p>Sign-in route</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.getSession.mockResolvedValue({ data: { session }, error: null });
  auth.onAuthStateChange.mockReturnValue({ data: { subscription: { unsubscribe: vi.fn() } } });
  auth.signOut.mockResolvedValue({ error: null });
});

describe("UserMenu", () => {
  it("shows the signed-in email in the menu, behind an initial", async () => {
    const user = userEvent.setup();
    renderMenu(new QueryClient());

    const trigger = await screen.findByRole("button", { name: "Account menu" });
    expect(trigger).toHaveTextContent("A");
    await user.click(trigger);

    expect(await screen.findByText("ada@example.com")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });

  it("signs out, clears the cached images and goes to sign-in", async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient();
    queryClient.setQueryData(["images", "list", { kind: "all" }], { items: [] });
    renderMenu(queryClient);

    await user.click(await screen.findByRole("button", { name: "Account menu" }));
    await user.click(await screen.findByRole("menuitem", { name: "Sign out" }));

    expect(auth.signOut).toHaveBeenCalledOnce();
    expect(await screen.findByText("Sign-in route")).toBeInTheDocument();
    await waitFor(() =>
      expect(queryClient.getQueryData(["images", "list", { kind: "all" }])).toBeUndefined(),
    );
  });

  it("renders nothing once it knows there is no user", async () => {
    auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
    renderMenu(new QueryClient());

    await waitFor(() => expect(screen.queryByRole("button")).not.toBeInTheDocument());
  });
});
