import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useReducer, useRef } from "react";

import { toast } from "../../components/ui/toast";
import { ApiError, uploadImage } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { prependToGallery } from "./galleryCache";
import {
  initialUploadQueue,
  MAX_CONCURRENT_UPLOADS,
  nextToStart,
  uploadQueueReducer,
} from "./uploadQueue";
import type { UploadItem } from "./uploadQueue";

/** How long a finished upload stays in the list before it clears itself. */
export const AUTO_DISMISS_MS = 3000;

const FALLBACK_ERROR = "The upload failed. Please try again.";

export interface Uploads {
  items: readonly UploadItem[];
  addFiles: (files: readonly File[]) => void;
  cancel: (id: string) => void;
  retry: (id: string) => void;
  dismiss: (id: string) => void;
}

/**
 * Runs the upload queue: at most 3 files in flight, each with its own AbortController. A
 * finished upload goes into the grid's cache at once and the image lists are refetched. Failures
 * toast the server's message, and finished items clear themselves after 3 s. Leaving the page
 * aborts whatever is still going.
 */
export function useUploads(): Uploads {
  const queryClient = useQueryClient();
  const [items, dispatch] = useReducer(uploadQueueReducer, initialUploadQueue);
  const controllers = useRef(new Map<string, AbortController>());
  const dismissTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  // Not a UUID: `crypto.randomUUID` is missing on a phone that opens the dev server over http.
  const nextId = useRef(0);

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
        .then((image) => {
          release();
          prependToGallery(queryClient, image);
          dispatch({ type: "succeed", id: item.id });
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

  // Fills free slots whenever the queue changes. The controllers map guards against starting
  // the same item twice if the effect runs again before the `start` action has been applied.
  useEffect(() => {
    for (const item of nextToStart(items, MAX_CONCURRENT_UPLOADS)) {
      if (!controllers.current.has(item.id)) startUpload(item);
    }
  }, [items, startUpload]);

  useEffect(() => {
    for (const item of items) {
      if (item.status !== "done" || dismissTimers.current.has(item.id)) continue;
      const timer = setTimeout(() => {
        dismissTimers.current.delete(item.id);
        dispatch({ type: "dismiss", id: item.id });
      }, AUTO_DISMISS_MS);
      dismissTimers.current.set(item.id, timer);
    }
  }, [items]);

  useEffect(() => {
    const activeControllers = controllers.current;
    const activeTimers = dismissTimers.current;
    return () => {
      for (const controller of activeControllers.values()) controller.abort();
      activeControllers.clear();
      for (const timer of activeTimers.values()) clearTimeout(timer);
      activeTimers.clear();
    };
  }, []);

  const addFiles = useCallback((files: readonly File[]) => {
    if (files.length === 0) return;
    dispatch({
      type: "add",
      items: files.map((file) => ({ id: `upload-${nextId.current++}`, file })),
    });
  }, []);

  const cancel = useCallback((id: string) => {
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
