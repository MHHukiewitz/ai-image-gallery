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
import { validateEmail, validatePassword } from "./validation";

/** Email and password sign-in. A visitor who is already signed in is sent on to the gallery. */
export function SignInPage() {
  const { session } = useSession();
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

  const target = postSignInPath(location.state);
  if (session) return <Navigate to={target} replace />;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const nextEmailError = validateEmail(email);
    const nextPasswordError = validatePassword(password);
    setEmailError(nextEmailError);
    setPasswordError(nextPasswordError);
    setFormError(undefined);
    // Focus the first problem so a keyboard or screen-reader user lands on it.
    if (nextEmailError || nextPasswordError) {
      (nextEmailError ? emailRef : passwordRef).current?.focus();
      return;
    }

    setSubmitting(true);
    const { error } = await supabase.auth.signInWithPassword({
      email: email.trim(),
      password,
    });
    if (error) {
      setSubmitting(false);
      setFormError(friendlyAuthError(error));
      // The button was disabled while we waited, which drops focus. Move it to the message.
      alertRef.current?.focus();
      return;
    }
    navigate(target, { replace: true });
  }

  return (
    <AuthCard title="Sign in" description="Enter your email and password to open your gallery.">
      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="sign-in-email" className="text-sm font-medium">
            Email
          </label>
          <Input
            ref={emailRef}
            id="sign-in-email"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            invalid={Boolean(emailError)}
            aria-describedby={emailError ? "sign-in-email-error" : undefined}
          />
          {emailError && (
            <p id="sign-in-email-error" className="text-sm text-red-600 dark:text-red-400">
              {emailError}
            </p>
          )}
        </div>

        <div className="space-y-1.5">
          <label htmlFor="sign-in-password" className="text-sm font-medium">
            Password
          </label>
          <PasswordInput
            ref={passwordRef}
            id="sign-in-password"
            name="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            invalid={Boolean(passwordError)}
            aria-describedby={passwordError ? "sign-in-password-error" : undefined}
          />
          {passwordError && (
            <p id="sign-in-password-error" className="text-sm text-red-600 dark:text-red-400">
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
          Sign in
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-zinc-500 dark:text-zinc-400">
        New here?{" "}
        <Link
          to="/sign-up"
          state={location.state}
          className="font-medium text-zinc-900 underline dark:text-zinc-100"
        >
          Create an account
        </Link>
      </p>
    </AuthCard>
  );
}
