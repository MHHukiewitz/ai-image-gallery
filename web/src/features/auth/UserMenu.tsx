import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Link, useNavigate } from "react-router";

import { FOCUS_COLOR, FOCUS_RING } from "../../components/ui/focusRing";
import { Skeleton } from "../../components/ui/Skeleton";
import { supabase } from "../../lib/supabase";
import { useIsAdmin } from "./useIsAdmin";
import { useSession } from "./useSession";

const ITEM_CLASS =
  "flex cursor-pointer items-center rounded-lg px-3 py-2 text-sm outline-none data-[highlighted]:bg-zinc-100 dark:data-[highlighted]:bg-zinc-800";

/**
 * An initial in a round button. Its menu shows the signed-in email, a link to the admin settings
 * for an admin, and a sign-out item.
 */
export function UserMenu() {
  const { user, loading } = useSession();
  const isAdmin = useIsAdmin(user?.id);
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  // The same size as the button below, so the header doesn't shift when the session loads.
  if (loading) return <Skeleton className="size-11 shrink-0 rounded-full sm:size-9" />;
  if (!user) return null;

  const email = user.email ?? "";
  const initial = email.charAt(0).toUpperCase() || "?";

  async function signOut() {
    // signOut drops the local session even when the server call fails, so moving on is safe.
    await supabase.auth.signOut();
    // Cached images belong to the account that loaded them. Drop them before the next user sees them.
    queryClient.clear();
    navigate("/sign-in", { replace: true });
  }

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="Account menu"
          className={clsx(
            "inline-flex size-11 shrink-0 items-center justify-center rounded-full bg-zinc-900 text-sm font-medium text-white hover:bg-zinc-700 sm:size-9 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300",
            FOCUS_RING,
            FOCUS_COLOR,
          )}
        >
          <span aria-hidden="true">{initial}</span>
        </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 w-64 max-w-[calc(100vw-2rem)] rounded-xl border border-zinc-200 bg-white p-1 text-zinc-900 shadow-lg dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-100"
        >
          <DropdownMenu.Label className="px-3 py-2">
            <span className="block text-xs text-zinc-500 dark:text-zinc-400">Signed in as</span>
            <span className="block truncate text-sm font-medium" title={email}>
              {email}
            </span>
          </DropdownMenu.Label>
          <DropdownMenu.Separator className="my-1 h-px bg-zinc-200 dark:bg-zinc-800" />
          {isAdmin && (
            <DropdownMenu.Item asChild className={ITEM_CLASS}>
              <Link to="/admin">Admin settings</Link>
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={() => void signOut()} className={ITEM_CLASS}>
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
