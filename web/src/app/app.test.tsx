import { render, screen, within } from "@testing-library/react";
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

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const stubs = () =>
  Array.from(document.querySelectorAll("[data-stub]")).map((el) => el.getAttribute("data-stub"));

describe("routes", () => {
  it("lays out the gallery with the header, search, user menu and every gallery stub", () => {
    renderAt("/");

    expect(stubs()).toEqual([
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
    expect(stubs()).toEqual(["SignInPage"]);
  });

  it("renders sign-up on its own route", () => {
    renderAt("/sign-up");
    expect(stubs()).toEqual(["SignUpPage"]);
  });

  it("redirects unknown paths to the gallery", () => {
    const router = renderAt("/no/such/page");

    expect(router.state.location.pathname).toBe("/");
    expect(stubs()).toContain("ImageGrid");
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

    expect(stubs()).toContain("ImageGrid");
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
