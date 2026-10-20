import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeIntersectionObserver } from "./testFixtures";
import { useLoadMoreSentinel } from "./useLoadMoreSentinel";

const live = () => FakeIntersectionObserver.live;

function Probe({ onReach, enabled }: { onReach: () => void; enabled: boolean }) {
  const ref = useLoadMoreSentinel(onReach, enabled);
  return <div ref={ref} data-testid="sentinel" />;
}

beforeEach(() => {
  FakeIntersectionObserver.instances = [];
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
});

afterEach(() => vi.unstubAllGlobals());

describe("useLoadMoreSentinel", () => {
  it("calls back when the sentinel comes into view, with a margin so loading starts early", () => {
    const onReach = vi.fn();
    const { getByTestId } = render(<Probe onReach={onReach} enabled />);

    const [observer] = live();
    expect(observer.observed).toEqual([getByTestId("sentinel")]);
    expect(observer.options?.rootMargin).toBe("0px 0px 600px 0px");

    observer.report(false);
    expect(onReach).not.toHaveBeenCalled();
    observer.report(true);
    expect(onReach).toHaveBeenCalledTimes(1);
  });

  it("observes nothing while disabled, and starts again with a fresh observer when re-enabled", () => {
    const onReach = vi.fn();
    const { rerender } = render(<Probe onReach={onReach} enabled={false} />);
    expect(FakeIntersectionObserver.instances).toHaveLength(0);

    rerender(<Probe onReach={onReach} enabled />);
    expect(live()).toHaveLength(1);

    // A page loaded and the sentinel may still be on screen: only a fresh observer reports that.
    rerender(<Probe onReach={onReach} enabled={false} />);
    expect(live()).toHaveLength(0);
    rerender(<Probe onReach={onReach} enabled />);
    expect(live()).toHaveLength(1);
    expect(FakeIntersectionObserver.instances).toHaveLength(2);
  });

  it("calls the latest callback without rebuilding the observer", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Probe onReach={first} enabled />);
    const [observer] = live();

    rerender(<Probe onReach={second} enabled />);
    observer.report(true);

    expect(FakeIntersectionObserver.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("disconnects on unmount", () => {
    const { unmount } = render(<Probe onReach={vi.fn()} enabled />);

    unmount();

    expect(live()).toHaveLength(0);
  });

  it("does nothing, without throwing, where IntersectionObserver doesn't exist", () => {
    vi.stubGlobal("IntersectionObserver", undefined);

    expect(() => render(<Probe onReach={vi.fn()} enabled />)).not.toThrow();
  });
});
