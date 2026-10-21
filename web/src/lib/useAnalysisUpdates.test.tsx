import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "./queryKeys";
import { subscribeToAnalysisUpdates } from "./supabase";
import type { AnalysisUpdate } from "./supabase";
import { useAnalysisUpdates } from "./useAnalysisUpdates";

vi.mock("./supabase", () => ({ subscribeToAnalysisUpdates: vi.fn() }));

/** One open subscription, with the callbacks the hook registered, so a test can play the channel. */
interface FakeSubscription {
  userId: string;
  push: (update: AnalysisUpdate) => void;
  setLive: (live: boolean) => void;
  unsubscribe: ReturnType<typeof vi.fn>;
}

const subscriptions: FakeSubscription[] = [];

beforeEach(() => {
  vi.mocked(subscribeToAnalysisUpdates).mockImplementation((userId, onUpdate, onLiveChange) => {
    const unsubscribe = vi.fn();
    subscriptions.push({ userId, push: onUpdate, setLive: onLiveChange, unsubscribe });
    return unsubscribe;
  });
});

afterEach(() => {
  subscriptions.length = 0;
  vi.mocked(subscribeToAnalysisUpdates).mockReset();
  vi.unstubAllEnvs();
});

const USER_ID = "00000000-0000-4000-8000-0000000087a1";
const ALL_IMAGES = queryKeys.images.list({ kind: "all" });
const SEARCH = queryKeys.images.list({ kind: "text", q: "beach" });
const THIS_DETAIL = queryKeys.images.detail(41);
const OTHER_DETAIL = queryKeys.images.detail(42);

function setup(signedIn = true) {
  const queryClient = new QueryClient();
  for (const key of [ALL_IMAGES, SEARCH, THIS_DETAIL, OTHER_DETAIL]) {
    queryClient.setQueryData(key, { cached: true });
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const hook = renderHook(({ id }) => useAnalysisUpdates(id), {
    wrapper,
    initialProps: { id: signedIn ? USER_ID : undefined },
  });
  const invalidated = (key: readonly unknown[]) =>
    queryClient.getQueryState(key)?.isInvalidated === true;
  return { ...hook, queryClient, invalidated };
}

describe("useAnalysisUpdates", () => {
  it("invalidates every list and that image's detail when an analysis is done", () => {
    const { invalidated } = setup();

    act(() => subscriptions[0].push({ imageId: 41, status: "done" }));

    expect(invalidated(ALL_IMAGES)).toBe(true);
    expect(invalidated(SEARCH)).toBe(true);
    expect(invalidated(THIS_DETAIL)).toBe(true);
    // Another image's analysis didn't change, so its cached detail is still good.
    expect(invalidated(OTHER_DETAIL)).toBe(false);
  });

  it("does the same when an analysis failed", () => {
    const { invalidated } = setup();

    act(() => subscriptions[0].push({ imageId: 42, status: "failed" }));

    expect(invalidated(ALL_IMAGES)).toBe(true);
    expect(invalidated(OTHER_DETAIL)).toBe(true);
    expect(invalidated(THIS_DETAIL)).toBe(false);
  });

  it.each(["pending", "processing"] as const)("leaves the cache alone while %s", (status) => {
    const { invalidated } = setup();

    act(() => subscriptions[0].push({ imageId: 41, status }));

    expect(invalidated(ALL_IMAGES)).toBe(false);
    expect(invalidated(THIS_DETAIL)).toBe(false);
  });

  it("subscribes once for the signed-in user, and not again on a re-render", () => {
    const { rerender } = setup();

    rerender({ id: USER_ID });

    expect(subscriptions.map(({ userId }) => userId)).toEqual([USER_ID]);
  });

  it("is live only while the channel is joined", () => {
    const { result } = setup();
    expect(result.current).toBe(false);

    act(() => subscriptions[0].setLive(true));
    expect(result.current).toBe(true);

    act(() => subscriptions[0].setLive(false));
    expect(result.current).toBe(false);

    act(() => subscriptions[0].setLive(true));
    expect(result.current).toBe(true);
  });

  it("unsubscribes on unmount", () => {
    const { unmount } = setup();

    unmount();

    expect(subscriptions[0].unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("unsubscribes on sign-out, and is no longer live", () => {
    const { result, rerender } = setup();
    act(() => subscriptions[0].setLive(true));

    rerender({ id: undefined });

    expect(subscriptions[0].unsubscribe).toHaveBeenCalledTimes(1);
    expect(result.current).toBe(false);
    expect(subscriptions).toHaveLength(1);
  });

  it("moves to the new user's channel when another user signs in", () => {
    const { rerender } = setup();

    rerender({ id: "00000000-0000-4000-8000-0000000087b1" });

    expect(subscriptions[0].unsubscribe).toHaveBeenCalledTimes(1);
    expect(subscriptions.map(({ userId }) => userId)).toEqual([
      USER_ID,
      "00000000-0000-4000-8000-0000000087b1",
    ]);
  });

  it("doesn't subscribe while signed out", () => {
    const { result } = setup(false);

    expect(subscribeToAnalysisUpdates).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });

  it("doesn't subscribe in mocks mode, which has no Realtime server", () => {
    vi.stubEnv("VITE_USE_MOCKS", "true");

    const { result } = setup();

    expect(subscribeToAnalysisUpdates).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });
});
