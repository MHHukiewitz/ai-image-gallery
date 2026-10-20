/* eslint-disable react-refresh/only-export-components -- startup code that is never hot-reloaded; the scrubbers are exported for tests. */
import * as Sentry from "@sentry/react";
import type { ReactNode } from "react";

import { ErrorState } from "../components/ui/ErrorState";

const REDACTED = "[redacted]";
/**
 * Signed image URLs carry a `token=` value that works as a password for an hour. This also
 * catches `access_token=` and `refresh_token=`, which Supabase puts in a URL fragment.
 */
const TOKEN_PARAM = /([?&#;][\w-]*token=)[^&#\s"']*/gi;
const SENSITIVE_HEADERS = new Set(["authorization", "cookie"]);
const MAX_DEPTH = 10;

/** Redacts the value of every `token=` parameter in `text`, leaving the other parameters alone. */
export function scrubUrl(text: string): string {
  return text.replace(TOKEN_PARAM, `$1${REDACTED}`);
}

/**
 * Copies `value`, redacting tokens in every string and dropping Authorization and Cookie
 * entries. Walks the whole structure because URLs turn up in request, breadcrumb, exception and
 * extra data alike.
 */
function scrub<T>(value: T, depth = 0, seen = new WeakSet<object>()): T {
  if (typeof value === "string") return scrubUrl(value) as T;
  if (typeof value !== "object" || value === null) return value;
  if (depth >= MAX_DEPTH || seen.has(value)) return value;
  seen.add(value);

  if (Array.isArray(value)) {
    return value.map((item) => scrub(item, depth + 1, seen)) as T;
  }
  const copy: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (SENSITIVE_HEADERS.has(key.toLowerCase())) continue;
    copy[key] = scrub(entry, depth + 1, seen);
  }
  return copy as T;
}

export function scrubEvent(event: Sentry.ErrorEvent): Sentry.ErrorEvent {
  return scrub(event);
}

export function scrubBreadcrumb(breadcrumb: Sentry.Breadcrumb): Sentry.Breadcrumb {
  return scrub(breadcrumb);
}

/**
 * Starts Sentry error reporting. Does nothing without `VITE_SENTRY_DSN`, so local dev and
 * forks send nothing anywhere. No Replay and no tracing: a replay would record users' private
 * photos. Call this first in `main.tsx`.
 */
export function initObservability(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN;
  if (!dsn) return;

  Sentry.init({
    dsn,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT,
    release: import.meta.env.VITE_RELEASE || undefined,
    // Sentry 11 replaced `sendDefaultPii` with `dataCollection`. These are its PII-off settings:
    // no user info, cookies, headers or bodies.
    dataCollection: { userInfo: false, cookies: false, httpHeaders: false, httpBodies: [] },
    beforeSend: scrubEvent,
    beforeBreadcrumb: scrubBreadcrumb,
  });
}

/** Catches render errors, reports them and shows a reload prompt instead of a blank page. */
export function AppErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <Sentry.ErrorBoundary
      fallback={
        <ErrorState
          title="Something went wrong"
          message="An unexpected error stopped the app. Reloading the page usually fixes it."
          retryLabel="Reload"
          onRetry={() => window.location.reload()}
          className="min-h-dvh justify-center"
        />
      }
    >
      {children}
    </Sentry.ErrorBoundary>
  );
}
