import { useCallback, useMemo } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

/** What the grid shows. Exactly one mode at a time, mirroring the API's q / similar_to / color. */
export type GalleryQuery =
  | { kind: "all" }
  | { kind: "text"; q: string }
  | { kind: "similar"; imageId: number }
  | { kind: "color"; hex: string };

/** The API rejects longer `q` values with a 422. */
const MAX_TEXT_LENGTH = 200;
const HEX_COLOR = /^#?[0-9a-f]{6}$/i;
const POSITIVE_INTEGER = /^[1-9]\d*$/;

const QUERY_PARAMS = ["q", "similar_to", "color"] as const;
const IMAGE_PARAM = "image";

function parseImageId(value: string | null): number | null {
  if (value === null || !POSITIVE_INTEGER.test(value)) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) ? id : null;
}

/**
 * Reads the gallery mode from URL params. The URL is user-editable, so anything invalid is
 * ignored (`?color=red` is just `all`). If several modes are present, `q` wins, then
 * `similar_to`, then `color`.
 */
export function parseGalleryQuery(params: URLSearchParams): GalleryQuery {
  const q = params.get("q");
  if (q !== null && q.trim() !== "") {
    return { kind: "text", q: q.slice(0, MAX_TEXT_LENGTH) };
  }

  const imageId = parseImageId(params.get("similar_to"));
  if (imageId !== null) return { kind: "similar", imageId };

  const color = params.get("color");
  if (color !== null && HEX_COLOR.test(color)) {
    return { kind: "color", hex: `#${color.replace("#", "").toLowerCase()}` };
  }

  return { kind: "all" };
}

/**
 * Returns a copy of `current` with the query params replaced by `query`. Every other param,
 * notably `image`, is left alone, so changing the filter doesn't close the open modal.
 */
export function toSearchParams(query: GalleryQuery, current: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const name of QUERY_PARAMS) next.delete(name);

  switch (query.kind) {
    case "text":
      if (query.q.trim() !== "") next.set("q", query.q);
      break;
    case "similar":
      next.set("similar_to", String(query.imageId));
      break;
    case "color":
      next.set("color", query.hex.toLowerCase());
      break;
    case "all":
      break;
  }
  return next;
}

/**
 * Params queued by an update that hasn't reached the router yet. `useSearchParams` hands out the
 * params of the last render, so two updates in one event handler (`setQuery(...)` then
 * `close()`) would otherwise each start from the old URL and the second would undo the first.
 * Cleared in a microtask, once the handler's synchronous work is done.
 */
let queuedParams: URLSearchParams | null = null;

/** Marks history entries pushed by `open()`, so `close()` knows going back leaves the modal. */
interface ModalState {
  galleryModal?: boolean;
}

function useUpdateSearchParams() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  return useCallback(
    (
      update: (params: URLSearchParams) => URLSearchParams,
      options: { replace?: boolean; state?: ModalState } = {},
    ) => {
      const base = queuedParams ?? searchParams;
      const next = update(base);
      if (next.toString() === base.toString()) return;

      queuedParams = next;
      queueMicrotask(() => {
        queuedParams = null;
      });
      void navigate({ search: next.toString() }, options);
    },
    [navigate, searchParams],
  );
}

type SetGalleryQuery = (query: GalleryQuery, options?: { replace?: boolean }) => void;

/**
 * The grid's query, kept in the URL. Setting it pushes a history entry unless `replace` is set
 * (use that for typing, so each keystroke isn't a back-button stop). The returned query keeps
 * its identity while the filter is unchanged, even when other params such as `image` change.
 */
export function useGalleryQuery(): [GalleryQuery, SetGalleryQuery] {
  const [searchParams] = useSearchParams();
  const updateSearchParams = useUpdateSearchParams();

  const canonical = toSearchParams(parseGalleryQuery(searchParams), new URLSearchParams());
  const key = canonical.toString();
  // Re-parsing the canonical string gives an object that is only rebuilt when the key changes.
  const query = useMemo(() => parseGalleryQuery(new URLSearchParams(key)), [key]);

  const setQuery = useCallback<SetGalleryQuery>(
    (next, options) => {
      updateSearchParams((params) => toSearchParams(next, params), options);
    },
    [updateSearchParams],
  );

  return [query, setQuery];
}

interface SelectedImage {
  /** The image shown in the modal, or null when it is closed. */
  imageId: number | null;
  /** Opens the modal. Adds a history entry so the back button closes it. */
  open: (id: number) => void;
  /** Closes the modal, going back if `open()` added the entry, else dropping the param. */
  close: () => void;
}

/** The modal's open image, kept in `?image=<id>`. */
export function useSelectedImage(): SelectedImage {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const updateSearchParams = useUpdateSearchParams();
  const imageId = parseImageId(searchParams.get(IMAGE_PARAM));
  const openedByApp = (location.state as ModalState | null)?.galleryModal === true;

  const open = useCallback(
    (id: number) => {
      updateSearchParams(
        (params) => {
          const next = new URLSearchParams(params);
          next.set(IMAGE_PARAM, String(id));
          return next;
        },
        // Swapping the image in an open modal must not stack entries that Back would walk through.
        // The entry then still belongs to whoever opened the modal first.
        { replace: imageId !== null, state: { galleryModal: imageId === null || openedByApp } },
      );
    },
    [imageId, openedByApp, updateSearchParams],
  );

  const close = useCallback(() => {
    if (imageId === null) return;
    // Going back is only right when nothing else happened since open(): a queued update in
    // this same handler (a tag click that also sets the query) has to survive.
    if (openedByApp && queuedParams === null) {
      void navigate(-1);
      return;
    }
    updateSearchParams(
      (params) => {
        const next = new URLSearchParams(params);
        next.delete(IMAGE_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [imageId, navigate, openedByApp, updateSearchParams]);

  return { imageId, open, close };
}
