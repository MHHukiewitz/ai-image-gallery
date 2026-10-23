import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AuthApiError } from "@supabase/supabase-js";
import type { Session } from "@supabase/supabase-js";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getSignupStatus } from "../../lib/api";
import { SignUpPage } from "./SignUpPage";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
  signUp: vi.fn(),
}));

vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));
vi.mock("../../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  getSignupStatus: vi.fn(),
}));

const session = {
  access_token: "test-token",
  user: { id: "user-1", email: "ada@example.com" },
} as unknown as Session;

function renderSignUp(state?: unknown) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[{ pathname: "/sign-up", state }]}>
        <Routes>
          <Route path="/sign-up" element={<SignUpPage />} />
          <Route path="/sign-in" element={<p>Sign-in route</p>} />
          <Route path="/" element={<p>Gallery route</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function submitValidForm() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), "ada@example.com");
  await user.type(screen.getByLabelText("Password"), "long-enough-pw");
  await user.click(screen.getByRole("button", { name: "Create account" }));
}

/** How Supabase Auth answers while sign-ups are closed: the trigger's error, passed through. */
const closedError = new AuthApiError("Sign-ups are closed.", 500, "P0001");

beforeEach(() => {
  vi.resetAllMocks();
  auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
  auth.onAuthStateChange.mockReturnValue({ data: { subscription: { unsubscribe: vi.fn() } } });
  vi.mocked(getSignupStatus).mockResolvedValue({ allow_signups: true });
});

describe("SignUpPage", () => {
  it("tells the visitor what the password needs before they submit", () => {
    renderSignUp();

    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("At least 8 characters.");
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
  });

  it("rejects a short password and focuses it, without calling Supabase", async () => {
    const user = userEvent.setup();
    renderSignUp();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "short");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("aria-invalid", "true");
    expect(password).toHaveAccessibleDescription(
      "At least 8 characters. Use at least 8 characters.",
    );
    expect(password).toHaveFocus();
    expect(auth.signUp).not.toHaveBeenCalled();
  });

  it("creates the account with the entered values and goes straight in when a session comes back", async () => {
    const user = userEvent.setup();
    auth.signUp.mockResolvedValue({ data: { user: session.user, session }, error: null });
    renderSignUp({ from: "/?image=7" });

    await user.type(screen.getByLabelText("Email"), " ada@example.com");
    await user.type(screen.getByLabelText("Password"), "long-enough-pw");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(auth.signUp).toHaveBeenCalledWith({
      email: "ada@example.com",
      password: "long-enough-pw",
    });
    expect(await screen.findByText("Gallery route")).toBeInTheDocument();
  });

  it("asks the visitor to check their inbox when no session comes back", async () => {
    const user = userEvent.setup();
    auth.signUp.mockResolvedValue({ data: { user: session.user, session: null }, error: null });
    renderSignUp();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "long-enough-pw");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("heading", { name: "Check your inbox" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "We sent a confirmation link to ada@example.com.",
    );
    expect(screen.getByRole("link", { name: "Back to sign in" })).toHaveAttribute(
      "href",
      "/sign-in",
    );
  });

  it("explains that the email already has an account and suggests signing in", async () => {
    const user = userEvent.setup();
    auth.signUp.mockResolvedValue({
      data: { user: null, session: null },
      error: new AuthApiError("User already registered", 422, "user_already_exists"),
    });
    renderSignUp();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "long-enough-pw");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "An account with this email already exists. Try signing in instead.",
    );
    expect(alert).toHaveFocus();
  });
  it("says sign-ups are closed instead of showing the form, and offers sign-in", async () => {
    vi.mocked(getSignupStatus).mockResolvedValue({ allow_signups: false });
    renderSignUp({ from: "/?image=7" });

    expect(await screen.findByRole("heading", { name: "Sign-ups are closed" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "New accounts can't be created right now.",
    );
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/sign-in");
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
  });

  it("switches to the closed notice when sign-ups closed after the page loaded", async () => {
    auth.signUp.mockResolvedValue({ data: { user: null, session: null }, error: closedError });
    vi.mocked(getSignupStatus)
      .mockResolvedValueOnce({ allow_signups: true })
      .mockResolvedValueOnce({ allow_signups: false });
    renderSignUp();

    await submitValidForm();

    expect(await screen.findByRole("heading", { name: "Sign-ups are closed" })).toBeInTheDocument();
    expect(getSignupStatus).toHaveBeenCalledTimes(2);
  });

  it("still shows the form when it can't learn whether sign-ups are open", async () => {
    vi.mocked(getSignupStatus).mockRejectedValue(new ApiError(0, "network_error", "Offline."));
    auth.signUp.mockResolvedValue({ data: { user: null, session: null }, error: closedError });
    renderSignUp();

    await submitValidForm();

    // Supabase Auth refused on its own; the page says something went wrong rather than nothing.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Something went wrong. Please try again.",
    );
    expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled();
  });
});
