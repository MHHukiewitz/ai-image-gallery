/** The minimum for new passwords. Sign-in doesn't check it, so accounts made before a change still work. */
export const PASSWORD_MIN_LENGTH = 8;

// Deliberately loose. The server has the final say, and a strict pattern rejects real addresses.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function validateEmail(email: string): string | undefined {
  if (!email.trim()) return "Enter your email address.";
  if (!EMAIL_PATTERN.test(email.trim())) return "Enter an email address like name@example.com.";
  return undefined;
}

export function validatePassword(password: string): string | undefined {
  return password ? undefined : "Enter your password.";
}

export function validateNewPassword(password: string): string | undefined {
  if (password.length < PASSWORD_MIN_LENGTH) {
    return `Use at least ${PASSWORD_MIN_LENGTH} characters.`;
  }
  return undefined;
}
