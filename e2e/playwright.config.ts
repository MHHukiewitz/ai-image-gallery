import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

// The repo-root .env holds the Supabase URL and secret key that global teardown needs to delete
// the test users. A variable that is already set wins over the file.
const rootEnvFile = fileURLToPath(new URL("../.env", import.meta.url));
if (existsSync(rootEnvFile)) process.loadEnvFile(rootEnvFile);

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  globalSetup: "./support/global-setup.ts",
  globalTeardown: "./support/global-teardown.ts",

  // One worker: every test signs up its own accounts, but the stack is a single API and worker,
  // and a steady load keeps the analysis waits short.
  workers: 1,
  fullyParallel: false,
  // A retry would hide exactly the flakiness this suite must not have.
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  // The journey is long, and each analysis wait has its own, longer, timeout.
  timeout: 120_000,
  expect: { timeout: 10_000 },

  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",

  use: {
    // The compose stack serves the SPA here and proxies /api to the API container.
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      // The API signs image URLs with the Supabase address it was given, and inside compose that
      // is host.docker.internal. Without this the browser can't resolve it, and no picture loads.
      args: ["--host-resolver-rules=MAP host.docker.internal 127.0.0.1"],
    },
  },

  projects: [
    {
      name: "chromium",
      testMatch: "journey.spec.ts",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "pixel-7",
      testMatch: "mobile.spec.ts",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
