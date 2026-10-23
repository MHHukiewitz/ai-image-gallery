import { Link } from "react-router";

import { ErrorState } from "../../components/ui/ErrorState";
import { Skeleton } from "../../components/ui/Skeleton";
import { Switch } from "../../components/ui/Switch";
import { isForbidden, useSetAllowSignups, useSiteSettings } from "./useSiteSettings";

// For example "23 Oct 2026, 21:40", in the reader's locale and time zone.
const CHANGED_AT_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

/**
 * The admin settings: for now, the switch that opens and closes sign-ups. The server decides who
 * may see it, so anyone else who opens the page gets a refusal, never the settings.
 */
export function AdminPage() {
  const settings = useSiteSettings();
  const setAllowSignups = useSetAllowSignups();

  if (settings.isPending) {
    return (
      <section aria-busy="true" className="mx-auto max-w-2xl space-y-4">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24" />
      </section>
    );
  }

  if (settings.isError) {
    if (isForbidden(settings.error)) {
      return (
        <div className="mx-auto max-w-2xl">
          <ErrorState
            title="Admins only"
            message="This page is for the site's admins. Your account isn't one."
          />
          <p className="text-center text-sm">
            <Link to="/" className="font-medium text-zinc-900 underline dark:text-zinc-100">
              Back to the gallery
            </Link>
          </p>
        </div>
      );
    }
    return (
      <ErrorState
        title="Couldn't load the settings"
        message={settings.error.message}
        onRetry={() => void settings.refetch()}
      />
    );
  }

  const allowSignups = setAllowSignups.isPending
    ? setAllowSignups.variables
    : settings.data.allow_signups;

  return (
    <section aria-labelledby="admin-heading" className="mx-auto max-w-2xl space-y-6">
      <h1 id="admin-heading" className="text-2xl font-semibold tracking-tight">
        Admin settings
      </h1>
      <div className="flex items-start justify-between gap-6 rounded-xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="space-y-1">
          <h2 id="allow-signups-label" className="text-base font-medium">
            Allow new sign-ups
          </h2>
          <p id="allow-signups-hint" className="text-sm text-zinc-500 dark:text-zinc-400">
            {allowSignups
              ? "Anyone can create an account."
              : "Nobody can create an account. Existing users can still sign in."}
          </p>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            Last changed{" "}
            <time dateTime={settings.data.updated_at}>
              {CHANGED_AT_FORMAT.format(new Date(settings.data.updated_at))}
            </time>
            .
          </p>
        </div>
        <Switch
          aria-labelledby="allow-signups-label"
          aria-describedby="allow-signups-hint"
          checked={allowSignups}
          disabled={setAllowSignups.isPending}
          onCheckedChange={(checked) => setAllowSignups.mutate(checked)}
          className="mt-0.5"
        />
      </div>
    </section>
  );
}
