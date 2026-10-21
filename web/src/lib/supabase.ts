import { createClient } from "@supabase/supabase-js";

import type { AnalysisStatus } from "./api";

const url = import.meta.env.VITE_SUPABASE_URL;
const publishableKey = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY;

if (!url || !publishableKey) {
  throw new Error(
    "Missing VITE_SUPABASE_URL or VITE_SUPABASE_PUBLISHABLE_KEY. Copy .env.example to .env (or web/.env.local) and fill them in.",
  );
}

/**
 * Auth and the analysis-status channel. Every other request goes through the API (`lib/api.ts`),
 * never straight to Supabase.
 */
export const supabase = createClient(url, publishableKey, {
  auth: { persistSession: true, autoRefreshToken: true },
});

/** One image's analysis status, as pushed by Realtime. */
export interface AnalysisUpdate {
  imageId: number;
  status: AnalysisStatus;
}

const ANALYSIS_STATUSES: readonly string[] = ["pending", "processing", "done", "failed"];

function toAnalysisUpdate(row: Record<string, unknown>): AnalysisUpdate | null {
  const { image_id: imageId, ai_processing_status: status } = row;
  if (typeof imageId !== "number" || typeof status !== "string") return null;
  if (!ANALYSIS_STATUSES.includes(status)) return null;
  return { imageId, status: status as AnalysisStatus };
}

/** Each subscription needs its own topic; see `subscribeToAnalysisUpdates`. */
let subscriptionCount = 0;

/**
 * Calls `onUpdate` whenever one of the user's images changes its analysis status, and
 * `onLiveChange(true)` once the channel is joined, `onLiveChange(false)` whenever it drops
 * (supabase-js rejoins by itself, which reports `true` again). Returns the unsubscribe function.
 *
 * Realtime sends a subscriber only the rows its RLS policy allows, and `filter` narrows the
 * server-side work to this user's rows. Neither callback fires after unsubscribing.
 */
export function subscribeToAnalysisUpdates(
  userId: string,
  onUpdate: (update: AnalysisUpdate) => void,
  onLiveChange: (live: boolean) => void,
): () => void {
  let active = true;

  // supabase-js returns the existing channel for a repeated topic, and a joined channel rejects
  // new listeners. A React effect that re-runs while the old channel is still closing (StrictMode,
  // a quick sign-out and sign-in) would hit both, so every subscription gets a topic of its own.
  const channel = supabase
    .channel(`image-analysis:${userId}:${++subscriptionCount}`)
    .on(
      "postgres_changes",
      {
        event: "UPDATE",
        schema: "public",
        table: "image_metadata",
        filter: `user_id=eq.${userId}`,
      },
      (payload) => {
        const update = toAnalysisUpdate(payload.new);
        if (active && update) onUpdate(update);
      },
    )
    .subscribe((status) => {
      if (active) onLiveChange(status === "SUBSCRIBED");
    });

  return () => {
    active = false;
    void supabase.removeChannel(channel);
  };
}
