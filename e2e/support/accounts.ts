import { randomUUID } from "node:crypto";

export interface Account {
  email: string;
  password: string;
}

/** The email prefix that global teardown matches, so it only ever deletes this run's users. */
export function accountPrefix(runId: string): string {
  return `e2e-${runId}-`;
}

/**
 * A fresh account for one test. The email is unique per call, so nothing depends on the order of
 * tests or on what an earlier run left behind. The password is generated, never a literal.
 */
export function newAccount(label: string): Account {
  const runId = process.env.E2E_RUN_ID ?? "local";
  return {
    email: `${accountPrefix(runId)}${label}-${randomUUID().slice(0, 8)}@example.test`,
    password: `pw-${randomUUID()}`,
  };
}
