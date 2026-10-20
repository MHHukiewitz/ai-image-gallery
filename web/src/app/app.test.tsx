import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

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
vi.mock("../features/auth/RequireAuth", () => ({ RequireAuth: marker("RequireAuth") }));
vi.mock("../features/auth/SignInPage", () => ({ SignInPage: marker("SignInPage") }));
vi.mock("../features/auth/SignUpPage", () => ({ SignUpPage: marker("SignUpPage") }));
vi.mock("../features/auth/UserMenu", () => ({ UserMenu: marker("UserMenu") }));
vi.mock("../features/search/SearchBar", () => ({ SearchBar: marker("SearchBar") }));
vi.mock("../features/upload/UploadZone", () => ({ UploadZone: marker("UploadZone") }));
vi.mock("../features/gallery/ImageGrid", () => ({ ImageGrid: marker("ImageGrid") }));
vi.mock("../features/image-detail/ImageModal", () => ({ ImageModal: marker("ImageModal") }));

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
      "UserMenu",
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

  it("renders the sign-in and sign-up pages without the layout", () => {
    renderAt("/sign-in");
    expect(features()).toEqual(["SignInPage"]);
  });

  it("renders sign-up on its own route", () => {
    renderAt("/sign-up");
    expect(features()).toEqual(["SignUpPage"]);
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
