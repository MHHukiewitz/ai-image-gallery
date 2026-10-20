import { describe, expect, it } from "vitest";

import {
  DEFAULT_UPLOAD_CONCURRENCY,
  initialUploadQueue,
  nextToStart,
  summarizeQueue,
  TOUCH_UPLOAD_CONCURRENCY,
  uploadConcurrency,
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

/** A drop whose files are still being hashed and looked up, like the hook adds them. */
function checkingAdded(count: number): UploadAction {
  const action = added(count);
  if (action.type !== "add") throw new Error("added() builds an add action");
  return { ...action, status: "checking" };
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

    expect(DEFAULT_UPLOAD_CONCURRENCY).toBe(2);
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
  it.each(["done", "duplicate", "error", "canceled"] as const)("removes a %s item", (finish) => {
    const actions: Record<typeof finish, UploadAction[]> = {
      done: [
        { type: "start", id: "f1" },
        { type: "succeed", id: "f1" },
      ],
      duplicate: [
        { type: "start", id: "f1" },
        { type: "duplicate", id: "f1", imageId: 9 },
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

  it("keeps a checking, queued or uploading item, so it can't escape its abort", () => {
    const checking = run([checkingAdded(1)]);
    const queued = run([added(1)]);
    const uploading = run([{ type: "start", id: "f1" }], queued);

    expect(run([{ type: "dismiss", id: "f1" }], checking)).toBe(checking);
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

describe("uploadConcurrency", () => {
  const withPointer = (coarse: boolean) => ({
    matchMedia: (query: string) => ({ matches: coarse && query === "(pointer: coarse)" }),
  });

  it("uploads one file at a time on a touch device", () => {
    expect(uploadConcurrency(withPointer(true))).toBe(1);
    expect(TOUCH_UPLOAD_CONCURRENCY).toBe(1);
  });

  it("uploads two at a time with a fine pointer", () => {
    expect(uploadConcurrency(withPointer(false))).toBe(2);
  });

  it("asks about the primary pointer", () => {
    const asked: string[] = [];

    uploadConcurrency({
      matchMedia: (query) => {
        asked.push(query);
        return { matches: false };
      },
    });

    expect(asked).toEqual(["(pointer: coarse)"]);
  });

  it("falls back to two where there is no matchMedia", () => {
    expect(uploadConcurrency({})).toBe(2);
  });

  it("is what nextToStart needs: one file starts on a touch device, two elsewhere", () => {
    const state = run([added(6)]);

    expect(ids(nextToStart(state, uploadConcurrency(withPointer(true))))).toEqual(["f1"]);
    expect(ids(nextToStart(state, uploadConcurrency(withPointer(false))))).toEqual(["f1", "f2"]);
  });

  it("starts the next file only after the single running one finishes", () => {
    const touch = uploadConcurrency(withPointer(true));
    const running = run([{ type: "start", id: "f1" }], run([added(3)]));

    expect(nextToStart(running, touch)).toEqual([]);
    expect(ids(nextToStart(run([{ type: "succeed", id: "f1" }], running), touch))).toEqual(["f2"]);
  });
});

describe("checking", () => {
  it("adds files as checking, with no progress, when asked to", () => {
    const state = run([checkingAdded(2)]);

    expect(state.map(({ id, status, progress }) => ({ id, status, progress }))).toEqual([
      { id: "f1", status: "checking", progress: 0 },
      { id: "f2", status: "checking", progress: 0 },
    ]);
  });

  it("does not offer a checking file for upload", () => {
    const state = run([checkingAdded(3)]);

    expect(nextToStart(state)).toEqual([]);
  });

  it("does not count a checking file against the upload slots", () => {
    const state = run([checkingAdded(2), added(0)]);
    const withUploads = run(
      [
        {
          type: "add",
          items: [
            { id: "u1", file: file("u1.png") },
            { id: "u2", file: file("u2.png") },
            { id: "u3", file: file("u3.png") },
          ],
        },
      ],
      state,
    );

    expect(ids(nextToStart(withUploads))).toEqual(["u1", "u2"]);
  });

  it("releases a file to the queue with ready", () => {
    const state = run([checkingAdded(2), { type: "ready", id: "f2" }]);

    expect(statusOf(state, "f1")).toBe("checking");
    expect(statusOf(state, "f2")).toBe("queued");
    expect(ids(nextToStart(state))).toEqual(["f2"]);
  });

  it("ignores ready for a file that was canceled while it waited", () => {
    const canceled = run([checkingAdded(1), { type: "cancel", id: "f1" }]);

    expect(statusOf(canceled, "f1")).toBe("canceled");
    expect(run([{ type: "ready", id: "f1" }], canceled)).toBe(canceled);
  });

  it("ignores ready for a file that is already uploading or unknown", () => {
    const uploading = run([added(1), { type: "start", id: "f1" }]);

    expect(run([{ type: "ready", id: "f1" }], uploading)).toBe(uploading);
    expect(run([{ type: "ready", id: "nope" }], uploading)).toBe(uploading);
  });

  it("can be canceled, and then retried like any canceled file", () => {
    const state = run([
      checkingAdded(1),
      { type: "cancel", id: "f1" },
      { type: "retry", id: "f1" },
    ]);

    expect(statusOf(state, "f1")).toBe("queued");
  });
});

describe("collapse", () => {
  it("removes the checking files it names and keeps the rest in order", () => {
    const state = run([checkingAdded(4), { type: "collapse", ids: ["f2", "f4"] }]);

    expect(ids([...state])).toEqual(["f1", "f3"]);
  });

  it("never removes a file that has moved on", () => {
    const state = run([
      checkingAdded(2),
      { type: "ready", id: "f1" },
      { type: "start", id: "f1" },
      { type: "collapse", ids: ["f1", "f2"] },
    ]);

    expect(ids([...state])).toEqual(["f1"]);
  });

  it("returns the same state when it removes nothing", () => {
    const state = run([checkingAdded(1)]);

    expect(run([{ type: "collapse", ids: ["nope"] }], state)).toBe(state);
    expect(run([{ type: "collapse", ids: [] }], state)).toBe(state);
  });
});

describe("hashed", () => {
  it("marks a checking file as read, and leaves it checking", () => {
    const state = run([checkingAdded(2), { type: "hashed", id: "f1" }]);

    expect(state.map(({ status, hashed }) => ({ status, hashed }))).toEqual([
      { status: "checking", hashed: true },
      { status: "checking", hashed: undefined },
    ]);
  });

  it("does not release the file: only ready does", () => {
    const state = run([checkingAdded(1), { type: "hashed", id: "f1" }]);

    expect(ids(nextToStart(state))).toEqual([]);
    expect(statusOf(run([{ type: "ready", id: "f1" }], state), "f1")).toBe("queued");
  });

  it("is a no-op for a file that was canceled, is already queued or is unknown", () => {
    const canceled = run([checkingAdded(1), { type: "cancel", id: "f1" }]);
    const queued = run([added(1)]);

    expect(run([{ type: "hashed", id: "f1" }], canceled)).toBe(canceled);
    expect(run([{ type: "hashed", id: "f1" }], queued)).toBe(queued);
    expect(run([{ type: "hashed", id: "nope" }], queued)).toBe(queued);
  });
});

describe("duplicate", () => {
  it("marks a checking file as already in the gallery, and remembers which image", () => {
    const state = run([checkingAdded(1), { type: "duplicate", id: "f1", imageId: 42 }]);

    expect(state[0]).toMatchObject({ status: "duplicate", progress: 100, imageId: 42 });
  });

  it("marks an uploading file the server answered 200 for, and frees its slot", () => {
    const uploading = startAvailable(run([added(3)]));

    const state = run([{ type: "duplicate", id: "f1", imageId: 7 }], uploading);

    expect(state[0]).toMatchObject({ status: "duplicate", imageId: 7 });
    expect(ids(nextToStart(state))).toEqual(["f3"]);
  });

  it("is a no-op for a file that was canceled, finished or is unknown", () => {
    const canceled = run([checkingAdded(1), { type: "cancel", id: "f1" }]);
    const done = run([added(1), { type: "start", id: "f1" }, { type: "succeed", id: "f1" }]);

    expect(run([{ type: "duplicate", id: "f1", imageId: 1 }], canceled)).toBe(canceled);
    expect(run([{ type: "duplicate", id: "f1", imageId: 1 }], done)).toBe(done);
    expect(run([{ type: "duplicate", id: "nope", imageId: 1 }], done)).toBe(done);
  });

  it("cannot be canceled or started again", () => {
    const state = run([checkingAdded(1), { type: "duplicate", id: "f1", imageId: 1 }]);

    expect(run([{ type: "cancel", id: "f1" }], state)).toBe(state);
    expect(run([{ type: "start", id: "f1" }], state)).toBe(state);
  });

  it("is not retried: the image is already in the gallery", () => {
    const state = run([checkingAdded(1), { type: "duplicate", id: "f1", imageId: 5 }]);

    expect(run([{ type: "retry", id: "f1" }], state)).toBe(state);
  });
});

/** Marks every file of a checking drop as hashed, like the hook does one by one. */
function hashedAll(count: number): UploadAction[] {
  return Array.from({ length: count }, (_, index) => ({ type: "hashed", id: `f${index + 1}` }));
}

describe("summarizeQueue with duplicates", () => {
  it("counts files being checked once they are all hashed", () => {
    expect(summarizeQueue(run([checkingAdded(3), ...hashedAll(3)]))).toBe("Checking 3 images");
    expect(summarizeQueue(run([checkingAdded(1), ...hashedAll(1)]))).toBe("Checking 1 image");
  });

  it("shows how many files of the drop have been hashed so far", () => {
    const drop = run([checkingAdded(30)]);
    expect(summarizeQueue(drop)).toBe("Checking 0 of 30…");

    const twelve = run(hashedAll(12), drop);
    expect(summarizeQueue(twelve)).toBe("Checking 12 of 30…");

    const all = run(hashedAll(30), drop);
    expect(summarizeQueue(all)).toBe("Checking 30 images");
  });

  it("drops a canceled file from the count", () => {
    const state = run([
      checkingAdded(3),
      { type: "hashed", id: "f1" },
      { type: "cancel", id: "f2" },
    ]);

    expect(summarizeQueue(state)).toBe("Checking 1 of 2…");
  });

  it("drops files that were collapsed as repeats", () => {
    const state = run([checkingAdded(3), ...hashedAll(3), { type: "collapse", ids: ["f3"] }]);

    expect(summarizeQueue(state)).toBe("Checking 2 images");
  });

  it("counts a file whose hash was null like any other: it was read", () => {
    // The hook dispatches "hashed" whether or not hashing produced a value.
    const state = run([
      checkingAdded(2),
      { type: "hashed", id: "f1" },
      { type: "hashed", id: "f2" },
    ]);

    expect(summarizeQueue(state)).toBe("Checking 2 images");
  });

  it("lists checking and uploading files together", () => {
    const state = run([
      checkingAdded(1),
      ...hashedAll(1),
      {
        type: "add",
        items: [{ id: "u1", file: file("u1.png") }],
      },
    ]);

    expect(summarizeQueue(state)).toBe("Checking 1 image, Uploading 1 image");
  });

  it("lists hashing progress and uploading files together", () => {
    const state = run([
      checkingAdded(4),
      { type: "hashed", id: "f1" },
      {
        type: "add",
        items: [{ id: "u1", file: file("u1.png") }],
      },
    ]);

    expect(summarizeQueue(state)).toBe("Checking 1 of 4…, Uploading 1 image");
  });

  it("announces duplicates once nothing is left to do", () => {
    const state = run([checkingAdded(3)]);
    const bumped = run(
      [
        { type: "duplicate", id: "f1", imageId: 1 },
        { type: "duplicate", id: "f2", imageId: 2 },
        { type: "duplicate", id: "f3", imageId: 3 },
      ],
      state,
    );

    expect(summarizeQueue(bumped)).toBe("3 already in your gallery, moved to top");
  });

  it("announces uploads and duplicates together", () => {
    const state = run([
      checkingAdded(2),
      { type: "duplicate", id: "f1", imageId: 1 },
      { type: "ready", id: "f2" },
      { type: "start", id: "f2" },
      { type: "succeed", id: "f2" },
    ]);

    expect(summarizeQueue(state)).toBe("Upload complete, 1 already in your gallery, moved to top");
  });
});
