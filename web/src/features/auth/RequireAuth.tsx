import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { Skeleton } from "../../components/ui/Skeleton";
import { useSession } from "./useSession";

/**
 * Renders `children` only for a signed-in user. Anyone else goes to sign-in, with the page they
 * asked for in `state.from` so sign-in can return them there.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, loading } = useSession();
  const location = useLocation();

  if (loading) return <SessionSkeleton />;

  if (!session) {
    const from = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate to="/sign-in" replace state={{ from }} />;
  }

  return <>{children}</>;
}

/** Shaped like the gallery so the page doesn't jump when the session check finishes. */
function SessionSkeleton() {
  return (
    <div role="status" className="mx-auto w-full max-w-7xl space-y-6 px-4 py-6">
      <span className="sr-only">Checking your session</span>
      <Skeleton className="h-16 w-full" />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {Array.from({ length: 8 }, (_, index) => (
          <Skeleton key={index} className="aspect-square" />
        ))}
      </div>
    </div>
  );
}
