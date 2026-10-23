import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router";

import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { supabase } from "../../lib/supabase";
import { AuthCard } from "./AuthCard";
import { PasswordInput } from "./PasswordInput";
import { friendlyAuthError } from "./authErrors";
import { postSignInPath } from "./authRedirect";
import { useSession } from "./useSession";
import { useSignupStatus } from "./useSignupStatus";
import { PASSWORD_MIN_LENGTH, validateEmail, validateNewPassword } from "./validation";

/**
 * Creates an account. Email confirmation is off, so the sign-up returns a session and the user
 * goes straight in. If confirmation is ever turned on, there's no session: show "check your inbox".
 * While an admin has closed sign-ups it says so instead of showing the form.
 */
export function SignUpPage() {
  const { session } = useSession();
  const signupStatus = useSignupStatus();
  const location = useLocation();
  const navigate = useNavigate();
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const alertRef = useRef<HTMLParagraphElement>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [emailError, setEmailError] = useState<string>();
  const [passwordError, setPasswordError] = useState<string>();
  const [formError, setFormError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);
  const [awaitingConfirmation, setAwaitingConfirmation] = useState<string>();

  const target = postSignInPath(location.state);
  if (session) return <Navigate to={target} replace />;

  // Until the answer comes, or if asking fails, the form shows: Supabase Auth refuses on its own.
  if (signupStatus.data?.allow_signups === false) {
    return (
      <AuthCard title="Sign-ups are closed">
        <div role="status" className="space-y-4 text-sm text-zinc-600 dark:text-zinc-300">
          <p>New accounts can't be created right now. If you already have one, you can sign in.</p>
          <p>
            <Link
              to="/sign-in"
              state={location.state}
              className="font-medium text-zinc-900 underline dark:text-zinc-100"
            >
              Sign in
            </Link>
          </p>
        </div>
      </AuthCard>
    );
  }

  if (awaitingConfirmation !== undefined) {
    return (
      <AuthCard title="Check your inbox">
        <div role="status" className="space-y-4 text-sm text-zinc-600 dark:text-zinc-300">
          <p>
            We sent a confirmation link to{" "}
            <span className="font-medium">{awaitingConfirmation}</span>. Open it to finish creating
            your account.
          </p>
          <p>
            <Link
              to="/sign-in"
              state={location.state}
              className="font-medium text-zinc-900 underline dark:text-zinc-100"
            >
              Back to sign in
            </Link>
          </p>
        </div>
      </AuthCard>
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const nextEmailError = validateEmail(email);
    const nextPasswordError = validateNewPassword(password);
    setEmailError(nextEmailError);
    setPasswordError(nextPasswordError);
    setFormError(undefined);
    // Focus the first problem so a keyboard or screen-reader user lands on it.
    if (nextEmailError || nextPasswordError) {
      (nextEmailError ? emailRef : passwordRef).current?.focus();
      return;
    }

    const trimmedEmail = email.trim();
    setSubmitting(true);
    const { data, error } = await supabase.auth.signUp({ email: trimmedEmail, password });
    if (error) {
      // Supabase Auth refuses closed sign-ups with a generic database error. Ask again: if sign-ups
      // were closed since the page loaded, the closed notice replaces the form.
      const { data: status } = await signupStatus.refetch();
      setSubmitting(false);
      if (status?.allow_signups === false) return;
      setFormError(friendlyAuthError(error));
      // The button was disabled while we waited, which drops focus. Move it to the message.
      alertRef.current?.focus();
      return;
    }
    if (data.session) {
      navigate(target, { replace: true });
      return;
    }
    setAwaitingConfirmation(trimmedEmail);
  }

  return (
    <AuthCard title="Create your account" description="Sign up with your email and a password.">
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="sign-up-email" className="text-sm font-medium">
            Email
          </label>
          <Input
            ref={emailRef}
            id="sign-up-email"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            invalid={Boolean(emailError)}
            aria-describedby={emailError ? "sign-up-email-error" : undefined}
          />
          {emailError && (
            <p id="sign-up-email-error" className="text-sm text-red-600 dark:text-red-400">
              {emailError}
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <label htmlFor="sign-up-password" className="text-sm font-medium">
            Password
          </label>
          <PasswordInput
            ref={passwordRef}
            id="sign-up-password"
            name="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            invalid={Boolean(passwordError)}
            aria-describedby={
              passwordError
                ? "sign-up-password-hint sign-up-password-error"
                : "sign-up-password-hint"
            }
          />
          <p id="sign-up-password-hint" className="text-xs text-zinc-500 dark:text-zinc-400">
            At least {PASSWORD_MIN_LENGTH} characters.
          </p>
          {passwordError && (
            <p id="sign-up-password-error" className="text-sm text-red-600 dark:text-red-400">
              {passwordError}
            </p>
          )}
        </div>

        <p
          ref={alertRef}
          role="alert"
          tabIndex={-1}
          className="text-sm text-red-600 outline-none dark:text-red-400"
        >
          {formError}
        </p>

        <Button type="submit" className="w-full" loading={submitting}>
          Create account
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-zinc-500 dark:text-zinc-400">
        Already have an account?{" "}
        <Link
          to="/sign-in"
          state={location.state}
          className="font-medium text-zinc-900 underline dark:text-zinc-100"
        >
          Sign in
        </Link>
      </p>
    </AuthCard>
  );
}
