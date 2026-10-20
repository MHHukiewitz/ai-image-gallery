/**
 * How many files go out at once. The browser allows more, but the API ingests synchronously, and
 * each upload makes Storage write two objects. More than this saturated the free tier's small
 * Storage connection pool during bursts.
 */
export const MAX_CONCURRENT_UPLOADS = 2;

export type UploadStatus = "queued" | "uploading" | "done" | "error" | "canceled";

export interface UploadItem {
  id: string;
  file: File;
  /** 0..100 */
  progress: number;
  status: UploadStatus;
  /** The message to show, set while `status` is "error". */
  error?: string;
}

export type UploadQueueState = readonly UploadItem[];

export type UploadAction =
  | { type: "add"; items: readonly { id: string; file: File }[] }
  | { type: "start"; id: string }
  | { type: "progress"; id: string; percent: number }
  | { type: "succeed"; id: string }
  | { type: "fail"; id: string; error: string }
  | { type: "cancel"; id: string }
  | { type: "retry"; id: string }
  | { type: "dismiss"; id: string };

export const initialUploadQueue: UploadQueueState = [];

const FINISHED: readonly UploadStatus[] = ["done", "error", "canceled"];

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
      const added = action.items
        .filter(({ id }) => !known.has(id))
        .map(({ id, file }): UploadItem => ({ id, file, progress: 0, status: "queued" }));
      return added.length === 0 ? state : [...state, ...added];
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
      return transition(state, action.id, ["queued", "uploading"], (item) => ({
        ...item,
        status: "canceled",
      }));
    case "retry":
      return transition(state, action.id, ["error", "canceled"], (item) => ({
        ...item,
        status: "queued",
        progress: 0,
        error: undefined,
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
  concurrency: number = MAX_CONCURRENT_UPLOADS,
): UploadItem[] {
  const running = state.filter((item) => item.status === "uploading").length;
  const free = Math.max(0, concurrency - running);
  return state.filter((item) => item.status === "queued").slice(0, free);
}

/**
 * One sentence for the live region. It changes when a file starts, finishes or fails, not on
 * every progress tick, so a screen reader isn't flooded.
 */
export function summarizeQueue(state: UploadQueueState): string {
  const count = (status: UploadStatus) => state.filter((item) => item.status === status).length;
  const plural = (n: number, noun: string) => `${n} ${noun}${n === 1 ? "" : "s"}`;

  const active = count("uploading") + count("queued");
  const failed = count("error");
  const parts: string[] = [];
  if (active > 0) parts.push(`Uploading ${plural(active, "image")}`);
  if (failed > 0) parts.push(`${plural(failed, "upload")} failed`);
  if (parts.length > 0) return parts.join(", ");
  return count("done") > 0 ? "Upload complete" : "";
}
