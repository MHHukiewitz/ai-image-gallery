import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";

import { ImageCard } from "./ImageCard";
import { makeImage } from "./testFixtures";

function Location() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderCard(image: ReturnType<typeof makeImage>, path = "/", showMatch = false) {
  const view = render(
    <MemoryRouter initialEntries={[path]}>
      <ImageCard image={image} showMatch={showMatch} />
      <Location />
    </MemoryRouter>,
  );
  const rerenderWith = (next: typeof image) =>
    view.rerender(
      <MemoryRouter initialEntries={[path]}>
        <ImageCard image={next} showMatch={showMatch} />
        <Location />
      </MemoryRouter>,
    );
  return { ...view, rerenderWith };
}

describe("ImageCard", () => {
  it.each(["pending", "processing"] as const)(
    "shows an Analyzing pill and no tags while %s",
    (status) => {
      renderCard(makeImage(1, { status, tags: [], description: null }));

      expect(screen.getByText("Analyzing…")).toBeInTheDocument();
      expect(screen.queryByText("Analysis failed")).not.toBeInTheDocument();
    },
  );

  it("shows a small failed badge when the analysis failed", () => {
    renderCard(makeImage(1, { status: "failed", tags: [], description: null, error: "boom" }));

    expect(screen.getByText("Analysis failed")).toBeInTheDocument();
    expect(screen.queryByText("Analyzing…")).not.toBeInTheDocument();
  });

  it("shows only the first 3 tags of a done image, and no status text", () => {
    renderCard(makeImage(1, { tags: ["beach", "sunset", "ocean", "sand", "waves"] }));

    expect(screen.getByText("beach")).toBeInTheDocument();
    expect(screen.getByText("sunset")).toBeInTheDocument();
    expect(screen.getByText("ocean")).toBeInTheDocument();
    expect(screen.queryByText("sand")).not.toBeInTheDocument();
    expect(screen.queryByText("Analyzing…")).not.toBeInTheDocument();
    expect(screen.queryByText("Analysis failed")).not.toBeInTheDocument();
  });

  it("reveals tags on hover and focus through CSS, not state", () => {
    renderCard(makeImage(1));

    const tags = screen.getByText("beach").parentElement;
    expect(tags).toHaveClass(
      "opacity-0",
      "group-hover:opacity-100",
      "group-focus-visible:opacity-100",
    );
    expect(screen.getByRole("button")).toHaveClass("group");
  });

  it("describes the image with its description, or its filename while it has none", () => {
    const { rerenderWith } = renderCard(makeImage(1, { description: "A golden beach." }));
    expect(screen.getByRole("img")).toHaveAttribute("alt", "A golden beach.");

    rerenderWith(makeImage(1, { status: "pending", description: null }));
    expect(screen.getByRole("img")).toHaveAttribute("alt", "photo-1.jpg");
  });

  it("loads the thumbnail lazily, in a square that reserves its space", () => {
    renderCard(makeImage(1));

    const img = screen.getByRole("img");
    expect(img).toHaveAttribute("loading", "lazy");
    expect(img).toHaveAttribute("decoding", "async");
    expect(img).toHaveAttribute("src", "https://storage.test/thumb-1.webp?token=first");
    expect(img).toHaveClass("object-cover");
    expect(screen.getByRole("button")).toHaveClass("aspect-square");
  });

  it("opens the modal by putting the image id in the URL", async () => {
    renderCard(makeImage(7), "/?q=beach");

    await userEvent.click(screen.getByRole("button"));

    expect(screen.getByTestId("search")).toHaveTextContent("?q=beach&image=7");
  });

  it("keeps the first thumbnail URL when a refetch signs a new one", () => {
    const { rerenderWith } = renderCard(makeImage(1));

    rerenderWith(
      makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?token=second" }),
    );

    expect(screen.getByRole("img")).toHaveAttribute(
      "src",
      "https://storage.test/thumb-1.webp?token=first",
    );
  });

  it("switches to the current thumbnail URL when the first one no longer loads", () => {
    const { rerenderWith } = renderCard(makeImage(1));
    rerenderWith(
      makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?token=second" }),
    );

    fireEvent.error(screen.getByRole("img"));

    expect(screen.getByRole("img")).toHaveAttribute(
      "src",
      "https://storage.test/thumb-1.webp?token=second",
    );
  });

  describe("without a usable thumbnail", () => {
    const placeholder = () => screen.queryByTestId("thumbnail-placeholder");

    it("shows no placeholder while the thumbnail is there", () => {
      renderCard(makeImage(1));

      expect(placeholder()).not.toBeInTheDocument();
    });

    it("shows a placeholder, and no empty src, for an empty URL", () => {
      renderCard(makeImage(1, {}, { thumbnail_url: "" }));

      expect(placeholder()).toBeInTheDocument();
      expect(screen.getByRole("img")).not.toHaveAttribute("src");
      expect(screen.getByRole("button")).toHaveClass("aspect-square");
    });

    it("shows a placeholder when the thumbnail fails to load", () => {
      renderCard(makeImage(1));

      fireEvent.error(screen.getByRole("img"));

      expect(placeholder()).toBeInTheDocument();
    });

    it("takes the first URL that arrives after the tile was empty", () => {
      const { rerenderWith } = renderCard(makeImage(1, {}, { thumbnail_url: "" }));

      rerenderWith(makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?t=a" }));
      expect(placeholder()).not.toBeInTheDocument();
      expect(screen.getByRole("img")).toHaveAttribute(
        "src",
        "https://storage.test/thumb-1.webp?t=a",
      );

      // The next poll signs it again; the tile doesn't reload.
      rerenderWith(makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?t=b" }));
      expect(screen.getByRole("img")).toHaveAttribute(
        "src",
        "https://storage.test/thumb-1.webp?t=a",
      );
    });

    it("tries a fresh URL once after a failed load, then gives up on it", () => {
      const { rerenderWith } = renderCard(makeImage(1));
      rerenderWith(
        makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?token=second" }),
      );

      fireEvent.error(screen.getByRole("img"));
      expect(placeholder()).not.toBeInTheDocument();

      fireEvent.error(screen.getByRole("img"));
      expect(placeholder()).toBeInTheDocument();
    });

    it("recovers when a new URL arrives after the thumbnail failed", () => {
      const { rerenderWith } = renderCard(makeImage(1));
      fireEvent.error(screen.getByRole("img"));
      expect(placeholder()).toBeInTheDocument();

      rerenderWith(
        makeImage(1, {}, { thumbnail_url: "https://storage.test/thumb-1.webp?token=second" }),
      );

      expect(placeholder()).not.toBeInTheDocument();
      expect(screen.getByRole("img")).toHaveAttribute(
        "src",
        "https://storage.test/thumb-1.webp?token=second",
      );
    });

    it("keeps the Analyzing pill on top of the placeholder", () => {
      renderCard(makeImage(1, { status: "pending", tags: [] }, { thumbnail_url: "" }));

      expect(placeholder()).toBeInTheDocument();
      expect(screen.getByText("Analyzing…")).toBeInTheDocument();
    });
  });

  describe("match badge", () => {
    const badge = () => screen.queryByRole("img", { name: /% match$/ });

    it("shows the score as a whole percentage, labelled for screen readers", () => {
      renderCard(makeImage(1, {}, { score: 0.874 }), "/", true);

      expect(screen.getByRole("img", { name: "87% match" })).toHaveTextContent("87%");
    });

    it("is absent unless the grid asks for it, as in the text and all modes", () => {
      renderCard(makeImage(1, {}, { score: 0.874 }));

      expect(badge()).not.toBeInTheDocument();
    });

    it("is absent for an image that carries no score", () => {
      renderCard(makeImage(1), "/", true);

      expect(badge()).not.toBeInTheDocument();
    });
  });

  describe("while the thumbnail loads", () => {
    it("shows the image's first dominant color as the tile's background", () => {
      renderCard(makeImage(1, { colors: ["#aabbcc", "#112233"] }));

      expect(screen.getByRole("button")).toHaveStyle({ backgroundColor: "#aabbcc" });
    });

    it("keeps the default background when the analysis has no color yet", () => {
      renderCard(makeImage(1, { status: "pending", colors: [] }));

      expect(screen.getByRole("button").style.backgroundColor).toBe("");
    });

    it("lets the dominant color show through a placeholder for a missing thumbnail", () => {
      renderCard(makeImage(1, { colors: ["#aabbcc"] }, { thumbnail_url: "" }));

      expect(screen.getByTestId("thumbnail-placeholder")).not.toHaveClass("bg-zinc-200");
      expect(screen.getByRole("button")).toHaveStyle({ backgroundColor: "#aabbcc" });
    });

    it("fades the thumbnail in over the color, and only when motion is allowed", () => {
      renderCard(makeImage(1));
      const img = screen.getByRole("img");

      expect(img).toHaveClass("opacity-0", "motion-safe:transition-[opacity,transform]");
      fireEvent.load(img);
      expect(img).toHaveClass("opacity-100");
    });
  });
});
