import { useEffect, useState } from "react";

/**
 * True once `active` has stayed true for `delayMs`. Used for "still loading" feedback, so a
 * response that comes back quickly never flashes a spinner.
 */
export function useDelayedFlag(active: boolean, delayMs: number): boolean {
  const [elapsed, setElapsed] = useState(false);

  useEffect(() => {
    if (!active) return;
    const timer = setTimeout(() => setElapsed(true), delayMs);
    return () => {
      clearTimeout(timer);
      setElapsed(false);
    };
  }, [active, delayMs]);

  return active && elapsed;
}
