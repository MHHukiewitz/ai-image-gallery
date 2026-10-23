import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { createQueryClient } from "./queryClient";
import { routes } from "./router";

// Under Vitest, "react-router/dom" loads a second copy of react-router whose contexts the
// rest of the app can't see. Vite's bundler dedupes them, so only the tests need this.
vi.mock("react-router/dom", async () => ({
  RouterProvider: (await import("react-router")).RouterProvider,
}));

// app/ only composes features, so each feature is swapped for a named marker here. These tests
// then check routing and layout, and a feature's real implementation can never break them.
const { marker } = vi.hoisted(() => ({
  marker:
    (name: string) =>
    ({ children }: { children?: ReactNode }) => (
      <div data-feature={name} className="contents">
        {children ?? name}
      </div>
    ),
}));
vi.mock("../features/admin/AdminPage", () => ({ AdminPage: marker("AdminPage") }));
vi.mock("../features/auth/RequireAuth", () => ({ RequireAuth: marker("RequireAuth") }));
vi.mock("../features/auth/SignInPage", () => ({ SignInPage: marker("SignInPage") }));
vi.mock("../features/auth/SignUpPage", () => ({ SignUpPage: marker("SignUpPage") }));
vi.mock("../features/auth/UserMenu", () => ({ UserMenu: marker("UserMenu") }));
vi.mock("../features/search/SearchBar", () => ({ SearchBar: marker("SearchBar") }));
vi.mock("../features/sharing/ShareGalleryDialog", () => ({
  ShareGalleryDialog: marker("ShareGalleryDialog"),
}));
vi.mock("../features/sharing/ShareRequests", () => ({ ShareRequests: marker("ShareRequests") }));
vi.mock("../features/upload/UploadZone", () => ({ UploadZone: marker("UploadZone") }));
// The grid marker also shows what the layout told it about the analysis channel.
vi.mock("../features/gallery/ImageGrid", async () => {
  const { useRealtimeLive } = await import("../lib/realtimeStatus");
  return {
    ImageGrid: () => (
      <div data-feature="ImageGrid" data-realtime-live={String(useRealtimeLive())}>
        ImageGrid
      </div>
    ),
  };
});
vi.mock("../features/image-detail/ImageModal", () => ({ ImageModal: marker("ImageModal") }));

// The layout opens the analysis channel for the signed-in user. Its own tests cover the channel.
const { useSession, useAnalysisUpdates } = vi.hoisted(() => ({
  useSession: vi.fn(),
  useAnalysisUpdates: vi.fn(),
}));
vi.mock("../features/auth/useSession", () => ({ useSession }));
vi.mock("../lib/useAnalysisUpdates", () => ({ useAnalysisUpdates }));

const USER_ID = "00000000-0000-4000-8000-0000000087a1";

beforeEach(() => {
  useSession.mockReturnValue({ session: {}, user: { id: USER_ID }, loading: false });
  useAnalysisUpdates.mockReturnValue(false);
});

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const features = () =>
  Array.from(document.querySelectorAll("[data-feature]")).map((el) =>
    el.getAttribute("data-feature"),
  );

describe("routes", () => {
  it("lays out the gallery with the header, search, user menu and every gallery feature", () => {
    renderAt("/");

    expect(features()).toEqual([
      "RequireAuth",
      "SearchBar",
      "ShareGalleryDialog",
      "UserMenu",
      "ShareRequests",
      "UploadZone",
      "ImageGrid",
      "ImageModal",
    ]);
    const header = screen.getByRole("banner");
    expect(within(header).getByRole("link", { name: "AI Image Gallery" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(within(screen.getByRole("main")).getByText("ImageGrid")).toBeInTheDocument();
  });

  it("opens the analysis channel for the signed-in user, once", () => {
    renderAt("/");

    expect(useAnalysisUpdates).toHaveBeenCalledWith(USER_ID);
  });

  it("tells the gallery whether the analysis channel is live", () => {
    useAnalysisUpdates.mockReturnValue(true);
    renderAt("/");

    expect(document.querySelector("[data-feature=ImageGrid]")).toHaveAttribute(
      "data-realtime-live",
      "true",
    );
  });

  it("tells the gallery the channel is down until it is live", () => {
    renderAt("/");

    expect(document.querySelector("[data-feature=ImageGrid]")).toHaveAttribute(
      "data-realtime-live",
      "false",
    );
  });

  it("keeps the channel closed while there is no user yet", () => {
    useSession.mockReturnValue({ session: null, user: null, loading: true });
    renderAt("/");

    expect(useAnalysisUpdates).toHaveBeenCalledWith(undefined);
  });

  it("renders the sign-in and sign-up pages without the layout", () => {
    renderAt("/sign-in");
    expect(features()).toEqual(["SignInPage"]);
  });

  it("renders sign-up on its own route", () => {
    renderAt("/sign-up");
    expect(features()).toEqual(["SignUpPage"]);
  });

  it("lays out the admin settings behind sign-in, without the gallery's search", () => {
    renderAt("/admin");

    expect(features()).toEqual(["RequireAuth", "ShareGalleryDialog", "UserMenu", "AdminPage"]);
    expect(within(screen.getByRole("main")).getByText("AdminPage")).toBeInTheDocument();
  });

  it("redirects unknown paths to the gallery", () => {
    const router = renderAt("/no/such/page");

    expect(router.state.location.pathname).toBe("/");
    expect(features()).toContain("ImageGrid");
  });
});

describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("mounts the providers and the router", () => {
    // jsdom has no matchMedia, which the toaster reads for the OS color scheme.
    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));

    render(<App />);

    expect(features()).toContain("ImageGrid");
  });
});

describe("createQueryClient", () => {
  it("doesn't refetch on window focus and retries once", () => {
    const options = createQueryClient().getDefaultOptions().queries;

    expect(options).toMatchObject({ refetchOnWindowFocus: false, retry: 1 });
  });

  it("returns a new cache each time", () => {
    expect(createQueryClient()).not.toBe(createQueryClient());
  });
});
