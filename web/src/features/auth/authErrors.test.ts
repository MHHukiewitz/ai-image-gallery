import { AuthApiError, AuthRetryableFetchError } from "@supabase/supabase-js";
import { describe, expect, it } from "vitest";

import { friendlyAuthError } from "./authErrors";

describe("friendlyAuthError", () => {
  it.each([
    ["invalid_credentials", "That email and password don't match an account."],
    ["user_already_exists", "An account with this email already exists. Try signing in instead."],
    ["email_not_confirmed", "Confirm your email first. Open the link we sent you, then sign in."],
    ["weak_password", "That password is too easy to guess. Choose a longer one."],
  ])("maps %s to a sentence a person can act on", (code, message) => {
    expect(friendlyAuthError(new AuthApiError("raw server text", 400, code))).toBe(message);
  });

  it("explains rate limits, whichever code the server uses", () => {
    const expected = "Too many attempts. Wait a minute, then try again.";

    expect(friendlyAuthError(new AuthApiError("slow down", 429, "over_request_rate_limit"))).toBe(
      expected,
    );
    expect(friendlyAuthError(new AuthApiError("slow down", 429, undefined))).toBe(expected);
  });

  it("says the server could not be reached when no response came back", () => {
    expect(friendlyAuthError(new AuthRetryableFetchError("Failed to fetch", 0))).toBe(
      "Couldn't reach the server. Check your connection and try again.",
    );
  });

  it("never shows the raw server message for a code it doesn't know", () => {
    const error = new AuthApiError("internal stack trace detail", 500, "unexpected_failure");

    expect(friendlyAuthError(error)).toBe("Something went wrong. Please try again.");
  });

  it("falls back to a generic message for anything that isn't an auth error", () => {
    expect(friendlyAuthError(new Error("boom"))).toBe("Something went wrong. Please try again.");
    expect(friendlyAuthError(undefined)).toBe("Something went wrong. Please try again.");
  });
});
