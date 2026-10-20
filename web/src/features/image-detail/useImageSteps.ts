import type { TouchEvent } from "react";
import { useEffect, useRef } from "react";

import type { Step } from "./imageNavigation";
import { swipeStep } from "./imageNavigation";

function stepForKey(key: string): Step | null {
  switch (key) {
    case "ArrowLeft":
      return "previous";
    case "ArrowRight":
      return "next";
    default:
      return null;
  }
}

/**
 * ← and → step through the grid while the modal is open. They listen on the window, because focus
 * sits on whatever was clicked last. Modifier chords are left alone; those are the browser's own.
 */
export function useArrowSteps(onStep: (step: Step) => void, enabled: boolean): void {
  // The listener is bound once per `enabled`, and reads the latest `onStep` through this ref.
  const latest = useRef(onStep);
  useEffect(() => {
    latest.current = onStep;
  });

  useEffect(() => {
    if (!enabled) return;

    function onKeyDown(event: KeyboardEvent) {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.shiftKey) return;
      const step = stepForKey(event.key);
      if (!step) return;
      event.preventDefault();
      latest.current(step);
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}

/**
 * Touch handlers for the picture: a horizontal swipe steps to the previous or next image. Only one
 * finger counts, so a pinch never steps.
 */
export function useSwipeSteps(onStep: (step: Step) => void) {
  const start = useRef<{ x: number; y: number } | null>(null);

  return {
    onTouchStart: (event: TouchEvent<HTMLElement>) => {
      const touch = event.touches.length === 1 ? event.touches[0] : undefined;
      start.current = touch ? { x: touch.clientX, y: touch.clientY } : null;
    },
    onTouchEnd: (event: TouchEvent<HTMLElement>) => {
      const origin = start.current;
      start.current = null;
      const touch = event.changedTouches[0];
      if (!origin || !touch) return;
      const step = swipeStep(origin, { x: touch.clientX, y: touch.clientY });
      if (step) onStep(step);
    },
    onTouchCancel: () => {
      start.current = null;
    },
  };
}
