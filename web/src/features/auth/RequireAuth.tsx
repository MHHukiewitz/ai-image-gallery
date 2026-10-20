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

/**
 * Shaped like the gallery page so it doesn't jump when the session check finishes. The header
 * block is as tall as the real header: two rows on a phone, one from `sm` up.
 */
function SessionSkeleton() {
  return (
    <div role="status">
      <span className="sr-only">Checking your session</span>
      <div className="h-[117px] border-b border-zinc-200 sm:h-16 dark:border-zinc-800" />
      <div className="mx-auto w-full max-w-7xl space-y-3 px-4 py-6">
        <Skeleton className="h-[72px] w-full" />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6">
          {Array.from({ length: 8 }, (_, index) => (
            <Skeleton key={index} className="aspect-square" />
          ))}
        </div>
      </div>
    </div>
  );
}
