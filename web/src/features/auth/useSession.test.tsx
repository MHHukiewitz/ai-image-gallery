import { act, render, screen } from "@testing-library/react";
import type { AuthChangeEvent, Session } from "@supabase/supabase-js";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSession } from "./useSession";

const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  onAuthStateChange: vi.fn(),
}));

vi.mock("../../lib/supabase", () => ({ supabase: { auth } }));

const session = {
  access_token: "test-token",
  user: { id: "user-1", email: "ada@example.com" },
} as unknown as Session;

type AuthListener = (event: AuthChangeEvent, session: Session | null) => void;

/** Shows the hook's state as text, so each test can read it from the DOM. */
function Probe() {
  const state = useSession();
  if (state.loading) return <p>loading</p>;
  return <p>{state.user ? `signed in as ${state.user.email}` : "signed out"}</p>;
}

let listener: AuthListener | undefined;
const unsubscribe = vi.fn();

beforeEach(() => {
  listener = undefined;
  unsubscribe.mockReset();
  auth.getSession.mockResolvedValue({ data: { session: null }, error: null });
  auth.onAuthStateChange.mockImplementation((callback: AuthListener) => {
    listener = callback;
    return { data: { subscription: { unsubscribe } } };
  });
});

describe("useSession", () => {
  it("is loading until the stored session is read, then reports it", async () => {
    auth.getSession.mockResolvedValue({ data: { session }, error: null });
    render(<Probe />);

    expect(screen.getByText("loading")).toBeInTheDocument();
    expect(await screen.findByText("signed in as ada@example.com")).toBeInTheDocument();
  });

  it("reports signed out when nothing is stored", async () => {
    render(<Probe />);

    expect(await screen.findByText("signed out")).toBeInTheDocument();
  });

  it("follows sign-in and sign-out events", async () => {
    render(<Probe />);
    await screen.findByText("signed out");

    act(() => listener?.("SIGNED_IN", session));
    expect(screen.getByText("signed in as ada@example.com")).toBeInTheDocument();

    act(() => listener?.("SIGNED_OUT", null));
    expect(screen.getByText("signed out")).toBeInTheDocument();
  });

  it("doesn't let a slow stored-session read undo an event that arrived first", async () => {
    let resolveRead!: (result: { data: { session: Session } }) => void;
    auth.getSession.mockReturnValue(new Promise((resolve) => (resolveRead = resolve)));
    render(<Probe />);

    act(() => listener?.("SIGNED_OUT", null));
    await act(async () => resolveRead({ data: { session } }));

    expect(screen.getByText("signed out")).toBeInTheDocument();
  });

  it("fails closed when reading the stored session throws", async () => {
    auth.getSession.mockRejectedValue(new Error("storage unavailable"));
    render(<Probe />);

    expect(await screen.findByText("signed out")).toBeInTheDocument();
  });

  it("unsubscribes from auth events when unmounted", async () => {
    const { unmount } = render(<Probe />);
    await screen.findByText("signed out");

    unmount();

    expect(unsubscribe).toHaveBeenCalledOnce();
  });
});
