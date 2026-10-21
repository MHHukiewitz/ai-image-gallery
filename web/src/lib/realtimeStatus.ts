import { createContext, useContext } from "react";

/**
 * How often to poll for a finished analysis while the Realtime channel is live. Pushes carry the
 * news, so this only catches one that was missed (an event during a reconnect).
 */
export const LIVE_POLL_INTERVAL_MS = 15_000;

/**
 * Whether the analysis channel is live. Provided once by the app layout; the default is `false`,
 * so anything rendered without a channel (mocks mode, tests) keeps polling at its normal pace.
 */
export const RealtimeLiveContext = createContext(false);

export function useRealtimeLive(): boolean {
  return useContext(RealtimeLiveContext);
}
