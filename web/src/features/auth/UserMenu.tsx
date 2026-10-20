import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";

import { Skeleton } from "../../components/ui/Skeleton";
import { supabase } from "../../lib/supabase";
import { useSession } from "./useSession";

/** An initial in a round button. Its menu shows the signed-in email and a sign-out item. */
export function UserMenu() {
  const { user, loading } = useSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  if (loading) return <Skeleton className="size-9 shrink-0 rounded-full" />;
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
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-full bg-zinc-900 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300 dark:focus-visible:outline-zinc-100"
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
          <DropdownMenu.Item
            onSelect={() => void signOut()}
            className="flex cursor-pointer items-center rounded-lg px-3 py-2 text-sm outline-none data-[highlighted]:bg-zinc-100 dark:data-[highlighted]:bg-zinc-800"
          >
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
