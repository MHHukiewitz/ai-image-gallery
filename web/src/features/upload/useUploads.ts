import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useReducer, useRef } from "react";

import { toast } from "../../components/ui/toast";
import { ApiError, bumpImage, LOOKUP_LIMIT, lookupImages, uploadImage } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { collapseIdentical, inChunks } from "./dropCheck";
import type { HashedFile } from "./dropCheck";
import { moveToTopOfGallery, prependToGallery } from "./galleryCache";
import { hashFile } from "./hashFile";
import {
  DEFAULT_UPLOAD_CONCURRENCY,
  initialUploadQueue,
  nextToStart,
  uploadQueueReducer,
} from "./uploadQueue";
import type { UploadItem } from "./uploadQueue";
import { duplicateSummary } from "./uploadRules";

/** How long a finished upload stays in the list before it clears itself. */
export const AUTO_DISMISS_MS = 3000;

/** A duplicate stays longer: its View button is the point, and a phone user needs time to tap it. */
export const DUPLICATE_DISMISS_MS = 8000;

const FALLBACK_ERROR = "The upload failed. Please try again.";

export interface Uploads {
  items: readonly UploadItem[];
  addFiles: (files: readonly File[]) => void;
  cancel: (id: string) => void;
  retry: (id: string) => void;
  dismiss: (id: string) => void;
}

/**
 * Runs the upload queue. A drop is hashed first, and the files the user already has are bumped to
 * the top of the gallery instead of uploaded (see `checkDrop`). The rest go out 2 at a time, each
 * with its own AbortController. A finished upload goes into the grid's cache at once and the image
 * lists are refetched. Failures toast the server's message, and finished items clear themselves
 * after 3 s (8 s for a duplicate). Leaving the page aborts whatever is still going.
 */
export function useUploads(): Uploads {
  const queryClient = useQueryClient();
  const [items, dispatch] = useReducer(uploadQueueReducer, initialUploadQueue);
  const controllers = useRef(new Map<string, AbortController>());
  const dismissTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  // Not a UUID: `crypto.randomUUID` is missing on a phone that opens the dev server over http.
  const nextId = useRef(0);

  // Files canceled by the user. The async drop check reads it to skip them, because the reducer's
  // state is not yet committed when the check starts.
  const canceled = useRef(new Set<string>());
  // Aborted when the page goes away, to stop a drop check that is still hashing or asking.
  const lifecycle = useRef(new AbortController());

  const startUpload = useCallback(
    (item: UploadItem) => {
      const controller = new AbortController();
      controllers.current.set(item.id, controller);
      // A canceled upload settles a tick late. By then a retry may have put a newer controller
      // under the same id, which must stay.
      const release = () => {
        if (controllers.current.get(item.id) === controller) controllers.current.delete(item.id);
      };
      dispatch({ type: "start", id: item.id });

      uploadImage(item.file, {
        signal: controller.signal,
        onProgress: (percent) => dispatch({ type: "progress", id: item.id, percent }),
      })
        .then(({ image, created }) => {
          release();
          if (created) {
            prependToGallery(queryClient, image);
            dispatch({ type: "succeed", id: item.id });
          } else {
            // The server already had this file (the lookup was skipped or lost a race) and
            // bumped it. Treat it like a duplicate found up front.
            moveToTopOfGallery(queryClient, image);
            dispatch({ type: "duplicate", id: item.id, imageId: image.id });
          }
          void queryClient.invalidateQueries({ queryKey: queryKeys.images.all });
        })
        .catch((error: unknown) => {
          release();
          // A cancel already moved the item on, and the user doesn't need a toast for it.
          if (controller.signal.aborted) return;
          const message = error instanceof ApiError ? error.message : FALLBACK_ERROR;
          toast.error(`${item.file.name}: ${message}`);
          dispatch({ type: "fail", id: item.id, error: message });
        });
    },
    [queryClient],
  );

  /**
   * Hashes a drop, collapses files that repeat within it, asks the API which the user already has
   * and bumps those instead of uploading them. Everything else is released to the upload queue.
   *
   * Every step that can fail falls back to a normal upload, because the server dedupes too: a
   * file that can't be hashed, a lookup that errors and a bump of an image that has since been
   * deleted all end in "queued". Hashing is one file at a time so a phone never holds a whole
   * drop in memory, and bumps are sequential so the last file of the drop ends up on top, as it
   * would after uploads.
   */
  const checkDrop = useCallback(
    async (drop: readonly { id: string; file: File }[]) => {
      const { signal } = lifecycle.current;
      const stillWanted = (id: string) => !canceled.current.has(id);

      const hashed: HashedFile[] = [];
      for (const { id, file } of drop) {
        if (!stillWanted(id)) continue;
        const sha256 = await hashFile(file);
        if (signal.aborted) return;
        hashed.push({ id, file, sha256 });
      }

      const { unique, skipped } = collapseIdentical(hashed);
      if (skipped.length > 0) dispatch({ type: "collapse", ids: skipped.map(({ id }) => id) });

      const matches = new Map<string, number>();
      const hashes = unique.flatMap(({ sha256 }) => (sha256 === null ? [] : [sha256]));
      for (const chunk of inChunks(hashes, LOOKUP_LIMIT)) {
        try {
          for (const [sha256, imageId] of Object.entries(await lookupImages(chunk, { signal }))) {
            matches.set(sha256, imageId);
          }
        } catch {
          // No answer is no reason to hold the files back: the server catches duplicates too.
          if (signal.aborted) return;
        }
      }

      const duplicates = unique.filter(({ sha256 }) => sha256 !== null && matches.has(sha256));
      for (const { id, sha256 } of unique) {
        if (sha256 === null || !matches.has(sha256)) dispatch({ type: "ready", id });
      }

      let bumped = 0;
      for (const { id, sha256 } of duplicates) {
        const imageId = matches.get(sha256 ?? "");
        if (imageId === undefined || !stillWanted(id)) continue;
        try {
          const image = await bumpImage(imageId, { signal });
          moveToTopOfGallery(queryClient, image);
          dispatch({ type: "duplicate", id, imageId: image.id });
          bumped += 1;
        } catch {
          if (signal.aborted) return;
          // Gone since the lookup, or not reachable: upload the file like any other.
          dispatch({ type: "ready", id });
        }
      }

      const summary = duplicateSummary({ bumped, skipped: skipped.length });
      if (summary !== null) toast.success(summary);
      if (bumped > 0) void queryClient.invalidateQueries({ queryKey: queryKeys.images.all });
    },
    [queryClient],
  );

  // Fills free slots whenever the queue changes. The controllers map guards against starting
  // the same item twice if the effect runs again before the `start` action has been applied.
  useEffect(() => {
    for (const item of nextToStart(items, DEFAULT_UPLOAD_CONCURRENCY)) {
      if (!controllers.current.has(item.id)) startUpload(item);
    }
  }, [items, startUpload]);

  useEffect(() => {
    for (const item of items) {
      const isDone = item.status === "done";
      if ((!isDone && item.status !== "duplicate") || dismissTimers.current.has(item.id)) continue;
      const timer = setTimeout(
        () => {
          dismissTimers.current.delete(item.id);
          dispatch({ type: "dismiss", id: item.id });
        },
        isDone ? AUTO_DISMISS_MS : DUPLICATE_DISMISS_MS,
      );
      dismissTimers.current.set(item.id, timer);
    }
  }, [items]);

  useEffect(() => {
    const activeControllers = controllers.current;
    const activeTimers = dismissTimers.current;
    // A fresh controller per mount, so a remount (StrictMode runs the cleanup once) isn't born aborted.
    lifecycle.current = new AbortController();
    const activeLifecycle = lifecycle.current;
    return () => {
      activeLifecycle.abort();
      for (const controller of activeControllers.values()) controller.abort();
      activeControllers.clear();
      for (const timer of activeTimers.values()) clearTimeout(timer);
      activeTimers.clear();
    };
  }, []);

  const addFiles = useCallback(
    (files: readonly File[]) => {
      if (files.length === 0) return;
      const drop = files.map((file) => ({ id: `upload-${nextId.current++}`, file }));
      // Held back as "checking" until the drop has been compared with the gallery.
      dispatch({ type: "add", items: drop, status: "checking" });
      void checkDrop(drop);
    },
    [checkDrop],
  );

  const cancel = useCallback((id: string) => {
    canceled.current.add(id);
    dispatch({ type: "cancel", id });
    controllers.current.get(id)?.abort();
  }, []);

  const retry = useCallback((id: string) => dispatch({ type: "retry", id }), []);

  const dismiss = useCallback((id: string) => {
    clearTimeout(dismissTimers.current.get(id));
    dismissTimers.current.delete(id);
    dispatch({ type: "dismiss", id });
  }, []);

  return { items, addFiles, cancel, retry, dismiss };
}
