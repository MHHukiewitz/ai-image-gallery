import { QueryClient } from "@tanstack/react-query";

/** A function so tests get a fresh cache. Window focus refetches are off: the grid polls itself while images are analyzing. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
  });
}
