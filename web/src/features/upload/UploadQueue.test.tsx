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
  const handlers = { onCancel: vi.fn(), onRetry: vi.fn(), onDismiss: vi.fn() };
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

  it("offers Cancel, and only Cancel, while a file is queued or uploading", async () => {
    const { onCancel } = setup([
      item({ id: "a", status: "uploading", progress: 10 }),
      item({ id: "b", status: "queued" }),
    ]);

    expect(screen.queryByRole("button", { name: /retry|dismiss/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel upload of b.png" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel upload of a.png" }));

    expect(onCancel.mock.calls).toEqual([["b"], ["a"]]);
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
