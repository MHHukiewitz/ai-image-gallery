import { useQuery } from "@tanstack/react-query";

import { getSignupStatus } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

/**
 * Whether new accounts can be created. Only a hint for the sign-up page: Supabase Auth refuses on
 * its own while sign-ups are closed, so a stale or failed answer can't let anyone in.
 */
export function useSignupStatus() {
  return useQuery({
    queryKey: queryKeys.signups,
    queryFn: ({ signal }) => getSignupStatus({ signal }),
  });
}
