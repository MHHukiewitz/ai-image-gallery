import type { ReactNode } from "react";

/** Stub from #14. Its feature ticket replaces the body and keeps the name and props. */
export function RequireAuth({ children }: { children: ReactNode }) {
  // `contents` keeps the marker out of the layout until the real guard renders children directly.
  return (
    <div data-stub="RequireAuth" className="contents">
      {children}
    </div>
  );
}
