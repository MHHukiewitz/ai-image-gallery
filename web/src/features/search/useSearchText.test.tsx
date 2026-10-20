import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SEARCH_DEBOUNCE_MS, useSearchText } from "./useSearchText";

function setup(path = "/") {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
  );
  return renderHook(
    () => ({
      search: useSearchText(),
      location: useLocation(),
      navigationType: useNavigationType(),
      navigate: useNavigate(),
    }),
    { wrapper },
  );
}

const advance = (ms: number) => act(() => vi.advanceTimersByTime(ms));

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("debounce", () => {
  it("leaves the URL alone before 250 ms and updates it once after", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("beach"));
    advance(SEARCH_DEBOUNCE_MS - 1);
    expect(result.current.location.search).toBe("");
    expect(result.current.search.query).toEqual({ kind: "all" });

    advance(1);
    expect(result.current.location.search).toBe("?q=beach");
    expect(result.current.search.query).toEqual({ kind: "text", q: "beach" });
  });

  it("restarts the wait on every keystroke and sends only the last text", () => {
    const { result } = setup();

    for (const text of ["b", "be", "bea"]) {
      act(() => result.current.search.onTextChange(text));
      advance(SEARCH_DEBOUNCE_MS - 50);
    }
    expect(result.current.location.search).toBe("");

    advance(50);
    expect(result.current.location.search).toBe("?q=bea");
  });

  it("replaces the history entry instead of pushing one", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("beach"));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.navigationType).toBe("REPLACE");
  });

  it("sends the text without its surrounding spaces and doesn't refetch for a trailing space", () => {
    const { result } = setup("/?q=beach");

    act(() => result.current.search.onTextChange("  beach "));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.location.search).toBe("?q=beach");
    expect(result.current.navigationType).toBe("POP");
    expect(result.current.search.text).toBe("  beach ");
  });

  it("stops the text at the API's length limit", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("a".repeat(250)));

    expect(result.current.search.text).toHaveLength(200);
  });
});

describe("clearing", () => {
  it("shows every image again when the text is emptied", () => {
    const { result } = setup("/?q=beach");
    expect(result.current.search.text).toBe("beach");

    act(() => result.current.search.onTextChange(""));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.location.search).toBe("");
    expect(result.current.search.query).toEqual({ kind: "all" });
  });

  it("treats blank text as no search", () => {
    const { result } = setup("/?q=beach");

    act(() => result.current.search.onTextChange("   "));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.location.search).toBe("");
  });

  it("clear() empties the input and the URL at once, with no debounce", () => {
    const { result } = setup("/?q=beach");

    act(() => result.current.search.clear());

    expect(result.current.search.text).toBe("");
    expect(result.current.location.search).toBe("");
    expect(result.current.navigationType).toBe("REPLACE");
  });

  it("submit() applies the text at once", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("beach"));
    act(() => result.current.search.submit());

    expect(result.current.location.search).toBe("?q=beach");
  });
});

describe("outside changes", () => {
  it("shows the text of a query set from outside, such as a tag click", () => {
    const { result } = setup();

    act(() => result.current.navigate("/?q=sunset"));

    expect(result.current.search.text).toBe("sunset");
  });

  it("empties the input when the query leaves text mode", () => {
    const { result } = setup("/?q=beach");

    act(() => result.current.navigate("/?similar_to=7"));

    expect(result.current.search.text).toBe("");
    expect(result.current.search.query).toEqual({ kind: "similar", imageId: 7 });
  });

  it("follows back and forward through earlier searches", () => {
    const { result } = setup("/?q=beach");
    act(() => result.current.navigate("/?q=sunset"));
    expect(result.current.search.text).toBe("sunset");

    act(() => result.current.navigate(-1));
    expect(result.current.search.text).toBe("beach");

    act(() => result.current.navigate(1));
    expect(result.current.search.text).toBe("sunset");
  });

  it("drops a half-typed search instead of overwriting the outside change", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("abc"));
    act(() => result.current.navigate("/?q=sunset"));
    advance(SEARCH_DEBOUNCE_MS * 2);

    expect(result.current.location.search).toBe("?q=sunset");
    expect(result.current.search.text).toBe("sunset");
  });

  it("keeps typing that is ahead of the URL when the debounced write lands", () => {
    const { result } = setup();

    act(() => result.current.search.onTextChange("ab"));
    advance(SEARCH_DEBOUNCE_MS);
    act(() => result.current.search.onTextChange("abc"));

    expect(result.current.location.search).toBe("?q=ab");
    expect(result.current.search.text).toBe("abc");

    advance(SEARCH_DEBOUNCE_MS);
    expect(result.current.location.search).toBe("?q=abc");
  });

  it("doesn't wipe a similar filter when a letter is typed and deleted again", () => {
    const { result } = setup("/?similar_to=7");

    act(() => result.current.search.onTextChange("a"));
    act(() => result.current.search.onTextChange(""));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.search.query).toEqual({ kind: "similar", imageId: 7 });
  });

  it("doesn't turn a similar filter into 'all' when it comes back by history", () => {
    const { result } = setup("/?similar_to=7");
    act(() => result.current.navigate("/?q=beach"));
    advance(SEARCH_DEBOUNCE_MS);

    act(() => result.current.navigate(-1));
    advance(SEARCH_DEBOUNCE_MS * 2);

    expect(result.current.search.query).toEqual({ kind: "similar", imageId: 7 });
  });

  it("replaces a similar or color filter when text is typed", () => {
    const { result } = setup("/?color=%23aabbcc");

    act(() => result.current.search.onTextChange("dog"));
    advance(SEARCH_DEBOUNCE_MS);

    expect(result.current.location.search).toBe("?q=dog");
  });

  it("keeps other params, such as the open image, when the search changes", () => {
    const { result } = setup("/?image=5");

    act(() => result.current.search.onTextChange("dog"));
    advance(SEARCH_DEBOUNCE_MS);

    expect(new URLSearchParams(result.current.location.search).get("image")).toBe("5");
    expect(new URLSearchParams(result.current.location.search).get("q")).toBe("dog");
  });
});
