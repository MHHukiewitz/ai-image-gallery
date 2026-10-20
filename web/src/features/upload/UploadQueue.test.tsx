import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { UploadQueue } from "./UploadQueue.tsx";
import type { UploadItem } from "./uploadQueue";

function item(overrides: Partial<UploadItem> & Pick<UploadItem, "id" | "status">): UploadItem {
  return {
    file: new File(["x"], `${overrides.id}.png`, { type: "image/png" }),
    progress: 0,
    ...overrides,
  };
}

function setup(items: UploadItem[]) {
  const handlers = {
    onCancel: vi.fn(),
    onRetry: vi.fn(),
    onDismiss: vi.fn(),
    onView: vi.fn(),
  };
  render(<UploadQueue items={items} {...handlers} />);
  return handlers;
}

const rowOf = (name: string) => screen.getByText(name).closest("li") as HTMLElement;

describe("UploadQueue", () => {
  it("renders nothing visible for an empty queue, but keeps the live region ready", () => {
    setup([]);

    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Uploads" })).toBeInTheDocument();
  });

  it("shows each file's name, status and a progress bar with its value", () => {
    setup([
      item({ id: "beach", status: "uploading", progress: 42 }),
      item({ id: "forest", status: "queued" }),
      item({ id: "city", status: "done", progress: 100 }),
    ]);

    expect(within(rowOf("beach.png")).getByText("Uploading 42%")).toBeInTheDocument();
    expect(
      within(rowOf("beach.png")).getByRole("progressbar", {
        name: "Upload progress for beach.png",
      }),
    ).toHaveAttribute("aria-valuenow", "42");
    expect(within(rowOf("forest.png")).getByText("Waiting")).toBeInTheDocument();
    expect(within(rowOf("city.png")).getByText("Uploaded")).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("announces a summary politely, not every percent", () => {
    setup([
      item({ id: "a", status: "uploading", progress: 10 }),
      item({ id: "b", status: "queued" }),
    ]);

    const live = screen.getByText("Uploading 2 images");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveAttribute("aria-atomic", "true");
  });

  it("offers Cancel, and only Cancel, while a file is checking, queued or uploading", async () => {
    const { onCancel } = setup([
      item({ id: "a", status: "uploading", progress: 10 }),
      item({ id: "b", status: "queued" }),
      item({ id: "c", status: "checking" }),
    ]);

    expect(screen.queryByRole("button", { name: /retry|dismiss/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel upload of b.png" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel upload of a.png" }));

    await userEvent.click(screen.getByRole("button", { name: "Cancel upload of c.png" }));

    expect(onCancel.mock.calls).toEqual([["b"], ["a"], ["c"]]);
  });

  it("says a file is being checked against the gallery before it uploads", () => {
    setup([item({ id: "a", status: "checking" })]);

    expect(within(rowOf("a.png")).getByText("Checking")).toBeInTheDocument();
    expect(screen.getByText("Checking 1 image")).toHaveAttribute("aria-live", "polite");
  });

  describe("a file that is already in the gallery", () => {
    const duplicate = () => item({ id: "a", status: "duplicate", progress: 100, imageId: 42 });

    it("says so, and that it moved to the top", () => {
      setup([duplicate()]);

      expect(
        within(rowOf("a.png")).getByText("Already in your gallery, moved to top"),
      ).toBeInTheDocument();
      expect(rowOf("a.png")).toHaveAttribute("data-status", "duplicate");
      expect(within(rowOf("a.png")).queryByText("Uploaded")).not.toBeInTheDocument();
    });

    it("opens the existing image with View", async () => {
      const { onView } = setup([duplicate()]);

      await userEvent.click(screen.getByRole("button", { name: "View a.png in your gallery" }));

      expect(onView).toHaveBeenCalledOnce();
      expect(onView).toHaveBeenCalledWith(42);
    });

    it("offers View and Dismiss, but neither Cancel nor Retry", async () => {
      const { onDismiss } = setup([duplicate()]);

      expect(screen.queryByRole("button", { name: /cancel|retry/i })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Dismiss a.png" }));

      expect(onDismiss).toHaveBeenCalledWith("a");
    });

    it("has no View button for an upload that isn't a duplicate", () => {
      setup([item({ id: "b", status: "done", progress: 100 })]);

      expect(screen.queryByRole("button", { name: /view/i })).not.toBeInTheDocument();
    });

    it("is announced in the summary", () => {
      setup([duplicate()]);

      expect(screen.getByText("1 already in your gallery, moved to top")).toHaveAttribute(
        "aria-live",
        "polite",
      );
    });
  });

  it("shows the error and offers Retry and Dismiss for a failed file", async () => {
    const { onRetry, onDismiss } = setup([
      item({
        id: "a",
        status: "error",
        progress: 60,
        error: "That file is larger than the limit.",
      }),
    ]);

    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("That file is larger than the limit.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel/i })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Retry upload of a.png" }));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss a.png" }));

    expect(onRetry).toHaveBeenCalledWith("a");
    expect(onDismiss).toHaveBeenCalledWith("a");
  });

  it("offers Retry and Dismiss for a canceled file, and only Dismiss for a finished one", () => {
    setup([
      item({ id: "a", status: "canceled" }),
      item({ id: "b", status: "done", progress: 100 }),
    ]);

    expect(within(rowOf("a.png")).getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(within(rowOf("a.png")).getByRole("button", { name: /dismiss/i })).toBeInTheDocument();
    expect(
      within(rowOf("b.png")).queryByRole("button", { name: /retry/i }),
    ).not.toBeInTheDocument();
    expect(within(rowOf("b.png")).getByRole("button", { name: /dismiss/i })).toBeInTheDocument();
  });
});
