import clsx from "clsx";
import { useEffect, useRef } from "react";
import type { RefObject } from "react";

import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { Input } from "../../components/ui/Input";
import { Spinner } from "../../components/ui/Spinner";
import { ActiveFilterChip } from "./ActiveFilterChip";
import { MAX_SEARCH_LENGTH } from "./searchText";
import { useSearchSpinner } from "./useSearchSpinner";
import { useSearchText } from "./useSearchText";

/** True for fields where a typed `/` is text, not a shortcut. */
function isTextField(element: EventTarget | null): boolean {
  if (!(element instanceof HTMLElement)) return false;
  return element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName);
}

/** Pressing `/` anywhere on the page, outside a text field, moves focus to the search input. */
function useSlashToFocus(inputRef: RefObject<HTMLInputElement | null>) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "/" || event.defaultPrevented || event.isComposing) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTextField(event.target)) return;
      event.preventDefault(); // Otherwise the `/` is typed into the input we're about to focus.
      inputRef.current?.focus();
      inputRef.current?.select();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [inputRef]);
}

/**
 * The header's search: results follow the input as you type, without a reload. The text lives in
 * the URL (see `useSearchText`), and a similar-image or color filter shows as a chip under the
 * header, since there is no text to put in the input for those.
 */
export function SearchBar() {
  const { query, setQuery, text, onTextChange, submit, clear } = useSearchText();
  const searching = useSearchSpinner();
  const inputRef = useRef<HTMLInputElement>(null);
  useSlashToFocus(inputRef);

  return (
    <>
      <form
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        className="group relative mx-auto w-full max-w-xl"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
          className="pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2 text-zinc-400 dark:text-zinc-500"
        >
          <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
          <path d="M20 20l-3.5-3.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>

        <Input
          ref={inputRef}
          type="search"
          value={text}
          onChange={(event) => onTextChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault(); // Browsers' own Esc-clears-a-search-field would double up with ours.
            clear();
            event.currentTarget.blur();
          }}
          placeholder="Search by tag or description"
          aria-label="Search images"
          aria-keyshortcuts="/"
          maxLength={MAX_SEARCH_LENGTH}
          autoComplete="off"
          enterKeyHint="search"
          spellCheck={false}
          // The browser's own clear button is replaced by ours, which also refocuses the input.
          className="ps-9 pe-16 [&::-webkit-search-cancel-button]:hidden"
        />

        <div className="absolute inset-y-0 end-0 flex items-center gap-1 sm:end-1.5">
          {searching && <Spinner label="Searching" className="text-zinc-500 dark:text-zinc-400" />}
          {text !== "" ? (
            <button
              type="button"
              onClick={() => {
                clear();
                inputRef.current?.focus();
              }}
              aria-label="Clear search"
              className={clsx(
                "grid size-11 place-items-center rounded-full text-zinc-500 motion-safe:transition-colors hover:bg-zinc-100 hover:text-zinc-900 sm:size-7 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
                FOCUS_RING,
                FOCUS_COLOR,
              )}
            >
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-4">
                <path
                  d="M6 6l12 12M18 6L6 18"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          ) : (
            // Phones have no `/` key, so the hint is only for a mouse-and-keyboard pointer.
            <kbd
              aria-hidden="true"
              className="mr-1.5 hidden h-5 min-w-5 items-center justify-center rounded border border-zinc-300 px-1 font-sans text-xs text-zinc-500 pointer-fine:inline-flex group-focus-within:hidden dark:border-zinc-700 dark:text-zinc-400"
            >
              /
            </kbd>
          )}
        </div>
      </form>

      {(query.kind === "similar" || query.kind === "color") && (
        // Hangs from the bottom of the sticky header, which AppLayout makes the nearest
        // positioned ancestor, and lines up with the page's content column. It floats over the
        // top of the page, so it stays in view while scrolling.
        <div className="pointer-events-none absolute inset-x-0 top-full z-20">
          <div className="mx-auto w-full max-w-7xl px-4 pt-1">
            <ActiveFilterChip query={query} onClear={() => setQuery({ kind: "all" })} />
          </div>
        </div>
      )}
    </>
  );
}
