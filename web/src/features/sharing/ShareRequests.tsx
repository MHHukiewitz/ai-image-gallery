import { Button } from "../../components/ui/Button";
import type { ShareOut } from "../../lib/api";
import { pendingRequests, useAcceptShare, useEndShare, useShares } from "./useShares";

/**
 * One notice per gallery another user wants to share with this one, with Accept and Decline.
 * Renders nothing while there are none, or while the shares load or fail: the requests wait, and
 * the next focus of the window asks again.
 */
export function ShareRequests() {
  const { data } = useShares();
  const requests = pendingRequests(data);
  if (requests.length === 0) return null;

  return (
    <section aria-label="Share requests" className="space-y-2">
      {requests.map((request) => (
        <ShareRequest key={request.id} request={request} />
      ))}
    </section>
  );
}

function ShareRequest({ request }: { request: ShareOut }) {
  const accept = useAcceptShare();
  const decline = useEndShare();
  const busy = accept.isPending || decline.isPending;

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-sky-200 bg-sky-50 p-4 text-sm text-sky-950 sm:flex-row sm:items-center dark:border-sky-900 dark:bg-sky-950 dark:text-sky-100">
      <PeopleIcon />
      <p className="min-w-0 flex-1">
        <span className="font-medium break-all">{request.email}</span> wants to share their gallery
        with you. Once you accept, their images appear in yours. You can view them but not change
        them.
      </p>
      <div className="flex shrink-0 gap-2">
        <Button
          size="sm"
          loading={accept.isPending}
          disabled={busy}
          onClick={() => accept.mutate(request)}
        >
          Accept
        </Button>
        <Button
          size="sm"
          variant="secondary"
          loading={decline.isPending}
          disabled={busy}
          onClick={() =>
            decline.mutate({ share: request, success: `Declined the request of ${request.email}.` })
          }
        >
          Decline
        </Button>
      </div>
    </div>
  );
}

function PeopleIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className="hidden size-5 shrink-0 sm:block"
    >
      <circle cx="9" cy="8" r="3" stroke="currentColor" strokeWidth="2" />
      <path
        d="M3 19c0-3.3 2.7-5 6-5s6 1.7 6 5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M16 5a3 3 0 010 6M18 14c1.8.6 3 2.1 3 5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
