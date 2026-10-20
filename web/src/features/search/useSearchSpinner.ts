import { useIsFetching } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { queryKeys } from "../../lib/queryKeys";

/** A fetch faster than this shouldn't flash a spinner, and neither should a 2 s analysis poll. */
export const SPINNER_DELAY_MS = 150;

/** True while any image request has been in flight for a moment. */
export function useSearchSpinner(): boolean {
  const fetching = useIsFetching({ queryKey: queryKeys.images.all }) > 0;
  const [elapsed, setElapsed] = useState(false);

  useEffect(() => {
    if (!fetching) return;
    const timer = setTimeout(() => setElapsed(true), SPINNER_DELAY_MS);
    return () => {
      clearTimeout(timer);
      setElapsed(false);
    };
  }, [fetching]);

  return fetching && elapsed;
}
