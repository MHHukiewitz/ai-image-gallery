import { useCallback, useEffect, useRef, useState } from "react";

import { useGalleryQuery } from "../../lib/galleryQuery";
import type { GalleryQuery } from "../../lib/galleryQuery";
import { MAX_SEARCH_LENGTH, queryForText, textOf } from "./searchText";

/** Long enough to skip the keystrokes of a word being typed, short enough to feel instant. */
export const SEARCH_DEBOUNCE_MS = 250;

interface SearchText {
  query: GalleryQuery;
  setQuery: (query: GalleryQuery) => void;
  /** What the input shows. Ahead of the URL while the user is typing. */
  text: string;
  onTextChange: (text: string) => void;
  /** Applies the text now instead of waiting out the debounce (Enter). */
  submit: () => void;
  /** Empties the input and shows every image again, with no debounce. */
  clear: () => void;
}

/**
 * The search input's text, kept in step with the URL's query in both directions.
 *
 * Typing is debounced and written with `replace`, so a search doesn't leave one history entry
 * per keystroke. A change from outside (a tag click, back/forward) replaces the text and
 * drops whatever was still waiting out the debounce.
 */
export function useSearchText(): SearchText {
  const [query, setQuery] = useGalleryQuery();
  const urlText = textOf(query);

  const [text, setText] = useState(urlText);
  // The URL text `text` has been reconciled with, so only a *change* of the URL is looked at.
  const [seenUrlText, setSeenUrlText] = useState(urlText);
  // The last text this hook wrote to the URL. When the URL catches up to it, that's our own
  // write coming back, and the input may already hold newer keystrokes that must survive.
  const [sentText, setSentText] = useState(urlText);

  if (urlText !== seenUrlText) {
    setSeenUrlText(urlText);
    if (urlText !== sentText) {
      setText(urlText);
      setSentText(urlText);
    }
  }

  // The debounce timer outlives renders, so it reads the query through a ref.
  const latest = useRef({ query, setQuery });
  useEffect(() => {
    latest.current = { query, setQuery };
  });

  const commit = useCallback((nextText: string) => {
    const { query: current, setQuery: update } = latest.current;
    const wanted = queryForText(nextText);
    // Nothing to change, notably text that is empty while a similar/color filter is active:
    // typing a letter and deleting it again must not wipe that filter.
    if (textOf(wanted) === textOf(current).trim()) return;
    setSentText(textOf(wanted));
    update(wanted, { replace: true });
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => commit(text), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text, commit]);

  const onTextChange = useCallback((next: string) => setText(next.slice(0, MAX_SEARCH_LENGTH)), []);
  const submit = useCallback(() => commit(text), [commit, text]);
  const clear = useCallback(() => {
    setText("");
    commit("");
  }, [commit]);

  return { query, setQuery, text, onTextChange, submit, clear };
}
