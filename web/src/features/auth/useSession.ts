import type { Session, User } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { supabase } from "../../lib/supabase";

export interface SessionState {
  session: Session | null;
  user: User | null;
  /** True until the first session check finishes. A guard must wait for it before it redirects. */
  loading: boolean;
}

interface Settled {
  session: Session | null;
  loading: boolean;
}

/** The signed-in user, kept in step with Supabase's auth events (sign-in, sign-out, token refresh). */
export function useSession(): SessionState {
  const [state, setState] = useState<Settled>({ session: null, loading: true });

  useEffect(() => {
    let active = true;

    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      if (active) setState({ session, loading: false });
    });

    // Reading the stored session can finish after an auth event already answered the question.
    // Only fill in the answer if nothing has yet, so a stale read can't undo a newer event.
    const fillIfLoading = (session: Session | null) => {
      if (active) setState((current) => (current.loading ? { session, loading: false } : current));
    };
    supabase.auth
      .getSession()
      .then(({ data: result }) => fillIfLoading(result.session))
      // Failing closed: a broken read sends the visitor to sign-in rather than leaving a spinner.
      .catch(() => fillIfLoading(null));

    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, []);

  return {
    session: state.session,
    user: state.session?.user ?? null,
    loading: state.loading,
  };
}
