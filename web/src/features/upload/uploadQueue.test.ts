import { describe, expect, it } from "vitest";

import {
  initialUploadQueue,
  MAX_CONCURRENT_UPLOADS,
  nextToStart,
  summarizeQueue,
  uploadQueueReducer,
} from "./uploadQueue";
import type { UploadAction, UploadItem, UploadQueueState } from "./uploadQueue";

function file(name: string): File {
  return new File(["x"], name, { type: "image/png" });
}

function run(actions: UploadAction[], from: UploadQueueState = initialUploadQueue) {
  return actions.reduce(uploadQueueReducer, from);
}

function added(count: number): UploadAction {
  return {
    type: "add",
    items: Array.from({ length: count }, (_, index) => ({
      id: `f${index + 1}`,
      file: file(`photo-${index + 1}.png`),
    })),
  };
}

/** Starts everything `nextToStart` offers, like the hook's effect does. */
function startAvailable(state: UploadQueueState): UploadQueueState {
  return run(
    nextToStart(state).map((item) => ({ type: "start", id: item.id })),
    state,
  );
}

const statusOf = (state: UploadQueueState, id: string) =>
  state.find((item) => item.id === id)?.status;
const ids = (items: UploadItem[]) => items.map((item) => item.id);

describe("add", () => {
  it("queues each file with no progress, in order", () => {
    const state = run([added(2)]);

    expect(state.map(({ id, status, progress }) => ({ id, status, progress }))).toEqual([
      { id: "f1", status: "queued", progress: 0 },
      { id: "f2", status: "queued", progress: 0 },
    ]);
    expect(state[0].file.name).toBe("photo-1.png");
  });

  it("ignores an id that is already in the queue", () => {
    const state = run([added(2)]);

    expect(run([added(2)], state)).toBe(state);
  });
});

describe("concurrency", () => {
  it("offers at most 2 of 6 queued files, oldest first", () => {
    const state = run([added(6)]);

    expect(MAX_CONCURRENT_UPLOADS).toBe(2);
    expect(ids(nextToStart(state))).toEqual(["f1", "f2"]);
  });

  it("offers nothing once 2 are uploading, then one file per freed slot", () => {
    let state = startAvailable(run([added(6)]));
    expect(state.filter((item) => item.status === "uploading")).toHaveLength(2);
    expect(nextToStart(state)).toEqual([]);

    state = run([{ type: "succeed", id: "f2" }], state);

    expect(ids(nextToStart(state))).toEqual(["f3"]);
  });

  it("frees a slot when an upload fails or is canceled", () => {
    const state = startAvailable(run([added(5)]));

    const afterFail = run([{ type: "fail", id: "f1", error: "boom" }], state);
    const afterCancel = run([{ type: "cancel", id: "f1" }], state);

    expect(ids(nextToStart(afterFail))).toEqual(["f3"]);
    expect(ids(nextToStart(afterCancel))).toEqual(["f3"]);
  });

  it("takes the limit as a parameter and never goes negative", () => {
    const state = startAvailable(run([added(6)]));

    expect(ids(nextToStart(run([added(1)]), 1))).toEqual(["f1"]);
    expect(nextToStart(state, 2)).toEqual([]);
  });
});

describe("start", () => {
  it("moves a queued item to uploading", () => {
    const state = run([added(1), { type: "start", id: "f1" }]);

    expect(statusOf(state, "f1")).toBe("uploading");
  });

  it("does not restart an item that is finished", () => {
    const state = run([added(1), { type: "start", id: "f1" }, { type: "succeed", id: "f1" }]);

    expect(run([{ type: "start", id: "f1" }], state)).toBe(state);
  });
});

describe("progress", () => {
  it("records the percentage of an uploading item", () => {
    const state = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "progress", id: "f1", percent: 42 },
    ]);

    expect(state[0].progress).toBe(42);
  });

  it.each([
    [-5, 0],
    [0, 0],
    [100, 100],
    [250, 100],
    [Number.NaN, 0],
    [Number.POSITIVE_INFINITY, 100],
  ])("clamps %s to %s", (percent, expected) => {
    const state = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "progress", id: "f1", percent },
    ]);

    expect(state[0].progress).toBe(expected);
  });

  it("ignores a late tick after the upload was canceled", () => {
    const state = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "progress", id: "f1", percent: 30 },
      { type: "cancel", id: "f1" },
    ]);

    const after = run([{ type: "progress", id: "f1", percent: 60 }], state);

    expect(after).toBe(state);
    expect(after[0].progress).toBe(30);
  });

  it("ignores a queued item, which has nothing in flight", () => {
    const state = run([added(1)]);

    expect(run([{ type: "progress", id: "f1", percent: 50 }], state)).toBe(state);
  });
});

describe("succeed and fail", () => {
  it("marks success as done at 100%", () => {
    const state = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "progress", id: "f1", percent: 80 },
      { type: "succeed", id: "f1" },
    ]);

    expect(state[0]).toMatchObject({ status: "done", progress: 100 });
  });

  it("keeps the error message on a failed item", () => {
    const state = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "fail", id: "f1", error: "That file is larger than the upload limit." },
    ]);

    expect(state[0]).toMatchObject({
      status: "error",
      error: "That file is larger than the upload limit.",
    });
  });

  it("does not turn a canceled item into a success or a failure", () => {
    const state = run([added(1), { type: "start", id: "f1" }, { type: "cancel", id: "f1" }]);

    expect(run([{ type: "succeed", id: "f1" }], state)).toBe(state);
    expect(run([{ type: "fail", id: "f1", error: "x" }], state)).toBe(state);
  });
});

describe("cancel", () => {
  it("stops an upload in progress and frees its slot without ever restarting it", () => {
    const uploading = startAvailable(run([added(3)]));

    const state = run([{ type: "cancel", id: "f1" }], uploading);

    expect(statusOf(state, "f1")).toBe("canceled");
    expect(ids(nextToStart(state))).toEqual(["f3"]);
  });

  it("never starts a queued item that was canceled", () => {
    const state = run([added(4), { type: "cancel", id: "f3" }]);
    const started = startAvailable(state);

    expect(statusOf(started, "f3")).toBe("canceled");
    expect(ids(nextToStart(run([{ type: "succeed", id: "f1" }], started)))).toEqual(["f4"]);
  });

  it("leaves finished items alone", () => {
    const state = run([added(1), { type: "start", id: "f1" }, { type: "succeed", id: "f1" }]);

    expect(run([{ type: "cancel", id: "f1" }], state)).toBe(state);
  });
});

describe("retry", () => {
  it("re-queues a failed item with its progress and error cleared", () => {
    const failed = run([
      added(1),
      { type: "start", id: "f1" },
      { type: "progress", id: "f1", percent: 70 },
      { type: "fail", id: "f1", error: "boom" },
    ]);

    const state = run([{ type: "retry", id: "f1" }], failed);

    expect(state[0]).toMatchObject({ status: "queued", progress: 0, error: undefined });
    expect(ids(nextToStart(state))).toEqual(["f1"]);
  });

  it("re-queues a canceled item", () => {
    const state = run([added(1), { type: "cancel", id: "f1" }, { type: "retry", id: "f1" }]);

    expect(statusOf(state, "f1")).toBe("queued");
  });

  it("puts the retried item back in line behind the running ones", () => {
    const failed = run(
      [{ type: "fail", id: "f1", error: "boom" }],
      startAvailable(run([added(5)])),
    );

    const state = run([{ type: "retry", id: "f1" }], failed);

    // f2 is still uploading, so only one slot is free and f1 is first in line for it.
    expect(ids(nextToStart(state))).toEqual(["f1"]);
  });

  it("ignores items that are queued, uploading or done", () => {
    const queued = run([added(1)]);
    const uploading = run([{ type: "start", id: "f1" }], queued);
    const done = run([{ type: "succeed", id: "f1" }], uploading);

    expect(run([{ type: "retry", id: "f1" }], queued)).toBe(queued);
    expect(run([{ type: "retry", id: "f1" }], uploading)).toBe(uploading);
    expect(run([{ type: "retry", id: "f1" }], done)).toBe(done);
  });
});

describe("dismiss", () => {
  it.each(["done", "error", "canceled"] as const)("removes a %s item", (finish) => {
    const actions: Record<typeof finish, UploadAction[]> = {
      done: [
        { type: "start", id: "f1" },
        { type: "succeed", id: "f1" },
      ],
      error: [
        { type: "start", id: "f1" },
        { type: "fail", id: "f1", error: "boom" },
      ],
      canceled: [{ type: "cancel", id: "f1" }],
    };

    const state = run([added(2), ...actions[finish], { type: "dismiss", id: "f1" }]);

    expect(state.map((item) => item.id)).toEqual(["f2"]);
  });

  it("keeps a queued or uploading item, so it can't escape its abort", () => {
    const queued = run([added(1)]);
    const uploading = run([{ type: "start", id: "f1" }], queued);

    expect(run([{ type: "dismiss", id: "f1" }], queued)).toBe(queued);
    expect(run([{ type: "dismiss", id: "f1" }], uploading)).toBe(uploading);
  });

  it("ignores an unknown id", () => {
    const state = run([added(1)]);

    expect(run([{ type: "dismiss", id: "nope" }], state)).toBe(state);
  });
});

describe("summarizeQueue", () => {
  it("is empty for an empty queue", () => {
    expect(summarizeQueue(initialUploadQueue)).toBe("");
  });

  it("counts the files still going out", () => {
    const state = startAvailable(run([added(6)]));

    expect(summarizeQueue(state)).toBe("Uploading 6 images");
    expect(summarizeQueue(run([{ type: "succeed", id: "f1" }], state))).toBe("Uploading 5 images");
  });

  it("says 'image' for one, and adds failures", () => {
    const state = run([
      added(2),
      { type: "start", id: "f1" },
      { type: "fail", id: "f1", error: "boom" },
    ]);

    expect(summarizeQueue(state)).toBe("Uploading 1 image, 1 upload failed");
  });

  it("announces completion once nothing is left to do", () => {
    const state = run([added(1), { type: "start", id: "f1" }, { type: "succeed", id: "f1" }]);

    expect(summarizeQueue(state)).toBe("Upload complete");
  });
});
