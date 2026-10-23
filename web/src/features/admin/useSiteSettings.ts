import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { toast } from "../../components/ui/toast";
import { ApiError, getSiteSettings, updateSiteSettings } from "../../lib/api";
import type { SiteSettingsOut } from "../../lib/api";
import { queryKeys } from "../../lib/queryKeys";

/** The site-wide settings. Fails with ApiError `forbidden` for anyone but an admin. */
export function useSiteSettings() {
  return useQuery<SiteSettingsOut>({
    queryKey: queryKeys.siteSettings,
    queryFn: ({ signal }) => getSiteSettings({ signal }),
    // A 403 won't turn into an answer on a second try.
    retry: (failures, error) => !isForbidden(error) && failures < 1,
  });
}

export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** Opens or closes sign-ups, then shows the server's answer and says what changed. */
export function useSetAllowSignups() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (allowSignups: boolean) => updateSiteSettings({ allow_signups: allowSignups }),
    onSuccess: async (settings) => {
      queryClient.setQueryData(queryKeys.siteSettings, settings);
      toast.success(
        settings.allow_signups ? "New sign-ups are allowed." : "New sign-ups are closed.",
      );
      await queryClient.invalidateQueries({ queryKey: queryKeys.signups });
    },
    onError: (error) =>
      toast.error(error instanceof ApiError ? error.message : "Couldn't change the setting."),
  });
}
