import { render, screen } from "@testing-library/react";
import type { Session } from "@supabase/supabase-js";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RequireAuth } from "./RequireAuth";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
}));

vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));

const session = {
  access_token: "test-token",
  user: { id: "user-1", email: "ada@example.com" },
} as unknown as Session;

/** Shows where the redirect put the visitor, and the page they were asking for. */
function SignInStub() {
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  return <p>Sign-in route, from {from ?? "nowhere"}</p>;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/sign-in" element={<SignInStub />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <p>Private gallery</p>
            </RequireAuth>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.onAuthStateChange.mockReturnValue({ data: { subscription: { unsubscribe: vi.fn() } } });
});

describe("RequireAuth", () => {
  it("shows a skeleton and hides the page while the session is being checked", () => {
    auth.getSession.mockReturnValue(new Promise(() => undefined));
    renderAt("/");

    expect(screen.getByRole("status")).toHaveTextContent("Checking your session");
    expect(screen.queryByText("Private gallery")).not.toBeInTheDocument();
    expect(screen.queryByText(/Sign-in route/)).not.toBeInTheDocument();
  });

  it("sends a signed-out visitor to sign-in, remembering the page they asked for", async () => {
    auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
    renderAt("/?image=7");

    expect(await screen.findByText("Sign-in route, from /?image=7")).toBeInTheDocument();
    expect(screen.queryByText("Private gallery")).not.toBeInTheDocument();
  });

  it("renders the page for a signed-in user", async () => {
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    renderAt("/");

    expect(await screen.findByText("Private gallery")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
