import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";

const SCREENSHOT_DIR = fileURLToPath(new URL("../../docs/screenshots/", import.meta.url));

/**
 * Saves the page under docs/screenshots/ for the README. Sizes are in CSS pixels, so a phone
 * screenshot stays small whatever the device's pixel ratio. Animations are fast-forwarded to
 * their end, so a fade-in doesn't leave a half-visible dialog in the picture.
 */
export async function saveScreenshot(page: Page, name: string): Promise<void> {
  // Closing a menu with Esc leaves a keyboard focus ring on its button, which isn't what the
  // README should show.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.screenshot({
    path: `${SCREENSHOT_DIR}${name}.png`,
    animations: "disabled",
    scale: "css",
  });
}
