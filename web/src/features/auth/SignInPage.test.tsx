import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AuthApiError } from "@supabase/supabase-js";
import type { Session } from "@supabase/supabase-js";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SignInPage } from "./SignInPage";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
  signInWithPassword: vi.fn(),
}));

vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));

const session = {
  access_token: "test-token",
  user: { id: "user-1", email: "ada@example.com" },
} as unknown as Session;

function renderSignIn(state?: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/sign-in", state }]}>
      <Routes>
        <Route path="/sign-in" element={<SignInPage />} />
        <Route path="/sign-up" element={<p>Sign-up route</p>} />
        <Route path="/" element={<p>Gallery route</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
  auth.onAuthStateChange.mockReturnValue({ data: { subscription: { unsubscribe: vi.fn() } } });
});

describe("SignInPage", () => {
  it("labels both fields and links to sign-up", () => {
    renderSignIn();

    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("type", "email");
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute(
      "href",
      "/sign-up",
    );
  });

  it("shows a message for each empty field and focuses the first one", async () => {
    const user = userEvent.setup();
    renderSignIn();

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const email = screen.getByLabelText("Email");
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveAccessibleDescription("Enter your email address.");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Enter your password.");
    expect(email).toHaveFocus();
    expect(auth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("rejects a malformed email before calling Supabase", async () => {
    const user = userEvent.setup();
    renderSignIn();

    await user.type(screen.getByLabelText("Email"), "ada@");
    await user.type(screen.getByLabelText("Password"), "whatever");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByLabelText("Email")).toHaveAccessibleDescription(
      "Enter an email address like name@example.com.",
    );
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(auth.signInWithPassword).not.toHaveBeenCalled();
  });

  it("signs in with the entered values and goes to the page the visitor asked for", async () => {
    const user = userEvent.setup();
    auth.signInWithPassword.mockResolvedValue({ data: { session }, error: null });
    renderSignIn({ from: "/?image=7" });

    await user.type(screen.getByLabelText("Email"), "  ada@example.com ");
    await user.type(screen.getByLabelText("Password"), "correct-horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(auth.signInWithPassword).toHaveBeenCalledWith({
      email: "ada@example.com",
      password: "correct-horse",
    });
    expect(await screen.findByText("Gallery route")).toBeInTheDocument();
  });

  it("shows the server's failure in plain words and moves focus to it", async () => {
    const user = userEvent.setup();
    auth.signInWithPassword.mockResolvedValue({
      data: { session: null },
      error: new AuthApiError("Invalid login credentials", 400, "invalid_credentials"),
    });
    renderSignIn();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("That email and password don't match an account.");
    expect(alert).toHaveFocus();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("disables the button with a spinner while the request runs", async () => {
    const user = userEvent.setup();
    auth.signInWithPassword.mockReturnValue(new Promise(() => undefined));
    renderSignIn();

    await user.type(screen.getByLabelText("Email"), "ada@example.com");
    await user.type(screen.getByLabelText("Password"), "correct-horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
  });

  it("toggles the password between hidden and visible", async () => {
    const user = userEvent.setup();
    renderSignIn();

    await user.click(screen.getByRole("button", { name: "Show password" }));

    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide password" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  });

  it("sends a signed-in visitor on without showing the form", async () => {
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    renderSignIn();

    expect(await screen.findByText("Gallery route")).toBeInTheDocument();
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
  });
});
