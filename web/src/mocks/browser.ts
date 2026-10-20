import { setupWorker } from "msw/browser";

import { createHandlers } from "./handlers";

/**
 * Serves `/api` from the in-memory mocks through a service worker. Everything else, Supabase
 * auth included, passes through to the network. The worker script is `public/mockServiceWorker.js`.
 */
export async function startMocking(): Promise<void> {
  const worker = setupWorker(...createHandlers());
  await worker.start({ onUnhandledFrame: "bypass" });
}
