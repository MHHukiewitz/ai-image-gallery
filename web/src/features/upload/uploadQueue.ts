/**
 * How many files go out at once. The browser allows more, but the API ingests synchronously, and
 * each upload makes Storage write two objects. More than this saturated the free tier's small
 * Storage connection pool during bursts.
 */
export const DEFAULT_UPLOAD_CONCURRENCY = 2;

/**
 * On a phone the uplink is the bottleneck: parallel uploads split it, and each request ran past
 * the proxy's read timeout. One at a time keeps every request short.
 */
export const TOUCH_UPLOAD_CONCURRENCY = 1;

/** The part of `window` that `uploadConcurrency` reads. Absent where `matchMedia` is (old tests, SSR). */
export interface UploadEnvironment {
  matchMedia?: (query: string) => { matches: boolean };
}

/** How many files to upload at once: 1 when the primary pointer is coarse (a touch device), else 2. */
export function uploadConcurrency(env: UploadEnvironment): number {
  const touch = env.matchMedia?.("(pointer: coarse)").matches === true;
  return touch ? TOUCH_UPLOAD_CONCURRENCY : DEFAULT_UPLOAD_CONCURRENCY;
}

export type UploadStatus =
  "checking" | "queued" | "uploading" | "done" | "duplicate" | "error" | "canceled";

export interface UploadItem {
  id: string;
  file: File;
  /** 0..100 */
  progress: number;
  status: UploadStatus;
  /** The message to show, set while `status` is "error". */
  error?: string;
  /** The image the user already has, set while `status` is "duplicate". */
  imageId?: number;
  /** True once the file's bytes have been hashed, while `status` is "checking". */
  hashed?: boolean;
}

export type UploadQueueState = readonly UploadItem[];

export type UploadAction =
  // "checking" holds a file back while the drop is hashed and looked up; "queued" (the default)
  // makes it ready to upload.
  | {
      type: "add";
      items: readonly { id: string; file: File }[];
      status?: "checking" | "queued";
    }
  // The file's bytes have been read and hashed. The drop is still being checked.
  | { type: "hashed"; id: string }
  // The file is new to the user: let it upload.
  | { type: "ready"; id: string }
  // The user already has this image (found by lookup, or the server answered 200).
  | { type: "duplicate"; id: string; imageId: number }
  // Drops files that are identical to another file in the same drop, before anything is sent.
  | { type: "collapse"; ids: readonly string[] }
  | { type: "start"; id: string }
  | { type: "progress"; id: string; percent: number }
  | { type: "succeed"; id: string }
  | { type: "fail"; id: string; error: string }
  | { type: "cancel"; id: string }
  | { type: "retry"; id: string }
  | { type: "dismiss"; id: string };

export const initialUploadQueue: UploadQueueState = [];

const FINISHED: readonly UploadStatus[] = ["done", "duplicate", "error", "canceled"];

function clampPercent(percent: number): number {
  if (Number.isNaN(percent)) return 0;
  return Math.min(100, Math.max(0, percent));
}

/**
 * Applies `change` to the item with `id`, but only while it is in one of the `from` states.
 * Events arrive late (a progress tick after a cancel), so out-of-order ones must be no-ops. A
 * no-op returns the same array so React skips the re-render.
 */
function transition(
  state: UploadQueueState,
  id: string,
  from: readonly UploadStatus[],
  change: (item: UploadItem) => UploadItem,
): UploadQueueState {
  const index = state.findIndex((item) => item.id === id);
  if (index === -1 || !from.includes(state[index].status)) return state;
  return state.map((item, position) => (position === index ? change(item) : item));
}

/** The upload queue's state machine. Pure: ids come in with `add`, and aborting is the hook's job. */
export function uploadQueueReducer(
  state: UploadQueueState,
  action: UploadAction,
): UploadQueueState {
  switch (action.type) {
    case "add": {
      const known = new Set(state.map((item) => item.id));
      const status = action.status ?? "queued";
      const added = action.items
        .filter(({ id }) => !known.has(id))
        .map(({ id, file }): UploadItem => ({ id, file, progress: 0, status }));
      return added.length === 0 ? state : [...state, ...added];
    }
    case "hashed":
      return transition(state, action.id, ["checking"], (item) => ({ ...item, hashed: true }));
    case "ready":
      return transition(state, action.id, ["checking"], (item) => ({ ...item, status: "queued" }));
    case "duplicate":
      return transition(state, action.id, ["checking", "uploading"], (item) => ({
        ...item,
        status: "duplicate",
        progress: 100,
        error: undefined,
        imageId: action.imageId,
      }));
    case "collapse": {
      const ids = new Set(action.ids);
      const remaining = state.filter((item) => !(ids.has(item.id) && item.status === "checking"));
      return remaining.length === state.length ? state : remaining;
    }
    case "start":
      return transition(state, action.id, ["queued"], (item) => ({
        ...item,
        status: "uploading",
        progress: 0,
      }));
    case "progress":
      return transition(state, action.id, ["uploading"], (item) => ({
        ...item,
        progress: clampPercent(action.percent),
      }));
    case "succeed":
      return transition(state, action.id, ["uploading"], (item) => ({
        ...item,
        status: "done",
        progress: 100,
        error: undefined,
      }));
    case "fail":
      return transition(state, action.id, ["uploading"], (item) => ({
        ...item,
        status: "error",
        error: action.error,
      }));
    case "cancel":
      return transition(state, action.id, ["checking", "queued", "uploading"], (item) => ({
        ...item,
        status: "canceled",
      }));
    case "retry":
      return transition(state, action.id, ["error", "canceled"], (item) => ({
        ...item,
        status: "queued",
        progress: 0,
        error: undefined,
        imageId: undefined,
      }));
    case "dismiss": {
      // Only finished items go away; an active one has to be canceled first so it gets aborted.
      const target = state.find((item) => item.id === action.id);
      if (!target || !FINISHED.includes(target.status)) return state;
      return state.filter((item) => item.id !== action.id);
    }
  }
}

/** The queued items to start now, oldest first, so that at most `concurrency` are uploading. */
export function nextToStart(
  state: UploadQueueState,
  concurrency: number = DEFAULT_UPLOAD_CONCURRENCY,
): UploadItem[] {
  const running = state.filter((item) => item.status === "uploading").length;
  const free = Math.max(0, concurrency - running);
  return state.filter((item) => item.status === "queued").slice(0, free);
}

/**
 * One sentence for the live region. It changes when a file starts, finishes or fails, not on
 * every progress tick, so a screen reader isn't flooded. While a drop is being hashed it counts
 * the files read so far ("Checking 12 of 30…"), because that is the one wait with nothing else
 * moving on screen.
 */
export function summarizeQueue(state: UploadQueueState): string {
  const count = (status: UploadStatus) => state.filter((item) => item.status === status).length;
  const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

  const checking = count("checking");
  const hashed = state.filter((item) => item.status === "checking" && item.hashed).length;
  const active = count("uploading") + count("queued");
  const failed = count("error");
  const parts: string[] = [];
  if (checking > hashed) parts.push(`Checking ${hashed} of ${checking}…`);
  else if (checking > 0) parts.push(`Checking ${plural(checking, "image")}`);
  if (active > 0) parts.push(`Uploading ${plural(active, "image")}`);
  if (failed > 0) parts.push(`${plural(failed, "upload")} failed`);
  if (parts.length > 0) return parts.join(", ");

  const duplicates = count("duplicate");
  if (count("done") > 0) parts.push("Upload complete");
  if (duplicates > 0) parts.push(`${duplicates} already in your gallery, moved to top`);
  return parts.join(", ");
}
