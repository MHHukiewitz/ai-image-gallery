import clsx from "clsx";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { FOCUS_COLOR, FOCUS_RING } from "../components/ui/focusRing";
import { useSession } from "../features/auth/useSession";
import { UserMenu } from "../features/auth/UserMenu";
import { ActiveFilterRow } from "../features/search/ActiveFilterRow";
import { SearchBar } from "../features/search/SearchBar";
import { ShareGalleryDialog } from "../features/sharing/ShareGalleryDialog";
import { RealtimeLiveContext } from "../lib/realtimeStatus";
import { useAnalysisUpdates } from "../lib/useAnalysisUpdates";

/**
 * Header with the app name, search, sharing and user menu, around the page in `children`. It also keeps the
 * analysis channel open for the signed-in user, and tells the page whether it is live (the grid
 * and the modal poll less while it is).
 */
export function AppLayout({ children }: { children: ReactNode }) {
  const { user } = useSession();
  const realtimeLive = useAnalysisUpdates(user?.id);

  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-xl focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow-md dark:focus:bg-zinc-900"
      >
        Skip to content
      </a>
      {/* Phones get two rows: logo and account on top, the search full width below. From `sm` up it
          is one row, with the search taking the space between them. */}
      <header className="sticky top-0 z-30 border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
        <div className="mx-auto flex w-full max-w-7xl flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 sm:h-16 sm:flex-nowrap sm:py-0">
          <Link
            to="/"
            className={clsx(
              "-ml-2 flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-2 rounded-xl font-semibold tracking-tight sm:-ml-0 sm:min-h-0 sm:min-w-0 sm:justify-start",
              FOCUS_RING,
              FOCUS_COLOR,
            )}
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-6">
              <rect
                x="3"
                y="4"
                width="18"
                height="16"
                rx="3"
                stroke="currentColor"
                strokeWidth="2"
              />
              <circle cx="9" cy="10" r="1.5" fill="currentColor" />
              <path
                d="M4 17l5-4 4 3 3-2 4 3"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinejoin="round"
              />
            </svg>
            <span className="sr-only sm:not-sr-only">AI Image Gallery</span>
          </Link>
          <div className="order-last min-w-0 basis-full sm:order-none sm:flex-1 sm:basis-auto">
            <SearchBar />
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-1 sm:ml-0">
            <ShareGalleryDialog />
            <UserMenu />
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        <ActiveFilterRow />
        <RealtimeLiveContext value={realtimeLive}>{children}</RealtimeLiveContext>
      </main>
    </div>
  );
}
