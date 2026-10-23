import { useQuery } from "@tanstack/react-query";

import { getAccount } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

/**
 * Whether the signed-in user is an admin, as the server says. It only decides whether the menu
 * offers the admin settings; the server refuses the settings to anyone else either way. False
 * while loading, after an error, and without a user.
 */
export function useIsAdmin(userId: string | undefined): boolean {
  const { data } = useQuery({
    queryKey: queryKeys.account(userId ?? ""),
    queryFn: ({ signal }) => getAccount({ signal }),
    enabled: userId !== undefined,
    // Who is an admin hardly ever changes, so one answer per sign-in is enough.
    staleTime: Infinity,
  });
  return data?.is_admin === true;
}
