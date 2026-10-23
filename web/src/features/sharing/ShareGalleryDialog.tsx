import { useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useId, useState } from "react";
import type { FormEvent } from "react";

import { Button } from "../../components/ui/Button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "../../components/ui/Dialog";
import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import { ApiError, shareGallery } from "../../lib/api";
import type { ShareOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";
import { acceptedGalleries, useEndShare, useShares } from "./useShares";

/**
 * The header's Share button and its dialog: share the gallery by e-mail, see who it is shared
 * with, and end a share from either side.
 */
export function ShareGalleryDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          aria-label="Share gallery"
          className={clsx(
            "inline-flex size-11 shrink-0 items-center justify-center gap-2 rounded-xl text-sm font-medium text-zinc-700 hover:bg-zinc-100 sm:h-9 sm:w-auto sm:px-3 dark:text-zinc-300 dark:hover:bg-zinc-800",
            FOCUS_RING,
            FOCUS_COLOR,
          )}
        >
          <ShareIcon />
          <span className="hidden sm:inline" aria-hidden="true">
            Share
          </span>
        </button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Share your gallery</DialogTitle>
        <DialogDescription>
          The people you add see all your images, including new ones, once they accept. They
          can&rsquo;t change or delete them.
        </DialogDescription>
        <ShareForm />
        <ShareLists />
      </DialogContent>
    </Dialog>
  );
}

function ShareForm() {
  const queryClient = useQueryClient();
  const inputId = useId();
  const errorId = useId();
  const [email, setEmail] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  const share = useMutation({
    mutationFn: (address: string) => shareGallery(address),
    onSuccess: (created) => {
      toast.success(`Asked ${created.email} to accept your gallery.`);
      setEmail("");
      return queryClient.invalidateQueries({ queryKey: queryKeys.shares });
    },
    // Refusals (no account, already shared, own address) are about what was typed, so they stay
    // next to the field instead of in a toast that disappears.
    onError: (error) => {
      setProblem(
        error instanceof ApiError ? error.message : "Couldn't share your gallery. Try again.",
      );
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address.includes("@")) {
      setProblem("Enter an e-mail address.");
      return;
    }
    setProblem(null);
    share.mutate(address);
  }

  return (
    <form noValidate onSubmit={submit} className="mt-4 space-y-2">
      <label htmlFor={inputId} className="block text-sm font-medium">
        E-mail address
      </label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          type="email"
          autoComplete="email"
          placeholder="friend@example.com"
          value={email}
          invalid={problem !== null}
          aria-describedby={problem ? errorId : undefined}
          onChange={(event) => {
            setEmail(event.target.value);
            setProblem(null);
          }}
        />
        <Button type="submit" loading={share.isPending}>
          Share
        </Button>
      </div>
      {problem && (
        <p id={errorId} role="alert" className="text-sm text-red-600 dark:text-red-400">
          {problem}
        </p>
      )}
    </form>
  );
}

function ShareLists() {
  const { data, isPending, error } = useShares();

  if (isPending) {
    return (
      <div role="status" aria-label="Loading shares" className="mt-6 space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    );
  }
  if (error) {
    return (
      <p className="mt-6 text-sm text-zinc-500 dark:text-zinc-400">
        Couldn&rsquo;t load whom you share with. Close this and try again.
      </p>
    );
  }

  const received = acceptedGalleries(data);
  return (
    <div className="mt-6 space-y-6">
      <ShareSection
        title="Shared with"
        empty="You haven't shared your gallery with anyone yet."
        shares={data.outgoing}
        actionLabel="Remove"
        success={(share) => `${share.email} no longer sees your gallery.`}
      />
      {received.length > 0 && (
        <ShareSection
          title="Galleries shared with you"
          shares={received}
          actionLabel="Leave"
          success={(share) => `Removed the images of ${share.email} from your gallery.`}
        />
      )}
    </div>
  );
}

interface ShareSectionProps {
  title: string;
  empty?: string;
  shares: ShareOut[];
  actionLabel: string;
  success: (share: ShareOut) => string;
}

function ShareSection({ title, empty, shares, actionLabel, success }: ShareSectionProps) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <h3
        id={headingId}
        className="text-xs font-medium tracking-wide text-zinc-500 uppercase dark:text-zinc-400"
      >
        {title}
      </h3>
      {shares.length === 0 ? (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">{empty}</p>
      ) : (
        <ul className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {shares.map((share) => (
            <ShareRow
              key={share.id}
              share={share}
              actionLabel={actionLabel}
              success={success(share)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function ShareRow({
  share,
  actionLabel,
  success,
}: {
  share: ShareOut;
  actionLabel: string;
  success: string;
}) {
  const end = useEndShare();
  return (
    <li className="flex items-center gap-3 py-2">
      <span className="min-w-0 flex-1 truncate text-sm" title={share.email}>
        {share.email}
      </span>
      {share.status === "pending" && (
        <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          Pending
        </span>
      )}
      <Button
        size="sm"
        variant="ghost"
        loading={end.isPending}
        aria-label={`${actionLabel} ${share.email}`}
        onClick={() => end.mutate({ share, success })}
      >
        {actionLabel}
      </Button>
    </li>
  );
}

function ShareIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" className="size-5 sm:size-4">
      <circle cx="18" cy="5" r="2.5" stroke="currentColor" strokeWidth="2" />
      <circle cx="6" cy="12" r="2.5" stroke="currentColor" strokeWidth="2" />
      <circle cx="18" cy="19" r="2.5" stroke="currentColor" strokeWidth="2" />
      <path d="M8.2 10.8l7.6-4.4M8.2 13.2l7.6 4.4" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}
