import { randomBytes } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import type { FullConfig } from "@playwright/test";

const READY_TIMEOUT_MS = 60_000;
const POLL_INTERVAL_MS = 1_000;

async function isUp(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3_000) });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Names this run, so teardown can find its users, and fails early with a hint when the stack
 * isn't up. Without the check every test would time out on a blank page.
 */
export default async function globalSetup(config: FullConfig): Promise<void> {
  process.env.E2E_RUN_ID ??= randomBytes(4).toString("hex");

  const baseURL = config.projects[0]?.use.baseURL;
  if (!baseURL) throw new Error("playwright.config.ts has no baseURL");
  const healthUrl = new URL("/api/health", baseURL).toString();

  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (!(await isUp(healthUrl))) {
    if (Date.now() > deadline) {
      throw new Error(
        `${healthUrl} did not answer within ${READY_TIMEOUT_MS / 1000} s. ` +
          "Start the stack first: `make dev-db`, then `docker compose up -d --build` (see e2e/README.md).",
      );
    }
    await sleep(POLL_INTERVAL_MS);
  }
}
