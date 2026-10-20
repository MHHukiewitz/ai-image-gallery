import { isAuthError, isAuthRetryableFetchError } from "@supabase/supabase-js";

const RATE_LIMITED = "Too many attempts. Wait a minute, then try again.";
const NETWORK = "Couldn't reach the server. Check your connection and try again.";
const UNKNOWN = "Something went wrong. Please try again.";

/** Supabase error codes we've seen in practice, mapped to text a person can act on. */
const MESSAGES: Record<string, string> = {
  invalid_credentials: "That email and password don't match an account.",
  email_not_confirmed: "Confirm your email first. Open the link we sent you, then sign in.",
  user_already_exists: "An account with this email already exists. Try signing in instead.",
  email_exists: "An account with this email already exists. Try signing in instead.",
  weak_password: "That password is too easy to guess. Choose a longer one.",
  email_address_invalid: "Enter an email address like name@example.com.",
  over_request_rate_limit: RATE_LIMITED,
  over_email_send_rate_limit: RATE_LIMITED,
  signup_disabled: "Sign-up is closed right now.",
};

/** Turns a failed Supabase auth call into one sentence for the form. Never shows raw server text. */
export function friendlyAuthError(error: unknown): string {
  if (isAuthRetryableFetchError(error)) return NETWORK;
  if (!isAuthError(error)) return UNKNOWN;
  if (error.code && MESSAGES[error.code]) return MESSAGES[error.code];
  if (error.status === 429) return RATE_LIMITED;
  return UNKNOWN;
}
