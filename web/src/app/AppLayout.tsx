import type { ReactNode } from "react";
import { Link } from "react-router";

import { UserMenu } from "../features/auth/UserMenu";
import { SearchBar } from "../features/search/SearchBar";

/** Header with the app name, search and user menu, around the page in `children`. */
export function AppLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-3 focus:left-3 focus:z-50 focus:rounded-xl focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow-md dark:focus:bg-zinc-900"
      >
        Skip to content
      </a>
      <header className="sticky top-0 z-30 border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
        <div className="mx-auto flex h-16 w-full max-w-7xl items-center gap-3 px-4">
          <Link to="/" className="flex shrink-0 items-center gap-2 font-semibold tracking-tight">
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
          <div className="min-w-0 flex-1">
            <SearchBar />
          </div>
          <UserMenu />
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        {children}
      </main>
    </div>
  );
}
