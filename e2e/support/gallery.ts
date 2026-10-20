import { expect } from "@playwright/test";
import type { Locator, Page } from "@playwright/test";
import type { Account } from "./accounts";
import type { Fixture } from "./fixtures";

/** How long a finished upload may take to be analyzed. The worker polls and the grid polls too. */
export const ANALYSIS_TIMEOUT_MS = 30_000;

/** The region that holds the tiles. It only exists while the current query has results. */
export function imagesRegion(page: Page): Locator {
  return page.getByRole("region", { name: "Images" });
}

/** One tile per image. */
export function tiles(page: Page): Locator {
  return imagesRegion(page).getByRole("listitem");
}

/**
 * The tile of an analyzed image. A tile's name is the description once there is one, and the
 * filename until then, so this also tells that the analysis finished.
 */
export function analyzedTile(page: Page, fixture: Fixture): Locator {
  return imagesRegion(page).getByRole("button", { name: fixture.description, exact: true });
}

export function imageModal(page: Page): Locator {
  return page.getByRole("dialog");
}

export function searchBox(page: Page): Locator {
  return page.getByRole("searchbox", { name: "Search images" });
}

/**
 * Creates an account from the sign-up page and waits until the user is signed in. The page's own
 * heading comes first: after a link click the URL changes before React swaps the page, and the
 * fields on the page being left would take the text. Which gallery URL the user lands on is up to
 * the caller to check, since a sign-up that follows a sign-out is sent back to the last page.
 */
export async function signUp(page: Page, account: Account): Promise<void> {
  await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password", { exact: true }).fill(account.password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
}

/** Signs in from the sign-in page and waits for the header's account menu. */
export async function signIn(page: Page, account: Account): Promise<void> {
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password", { exact: true }).fill(account.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Account menu" })).toBeVisible();
}

/** Opens the account menu and signs out, which ends on the sign-in page. */
export async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Account menu" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/sign-in$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
}

/** Picks the files in the upload zone's file input, as if they were chosen in the picker. */
export async function uploadFixtures(page: Page, fixtures: readonly Fixture[]): Promise<void> {
  await page.getByLabel("Choose images to upload").setInputFiles(fixtures.map((f) => f.path));
}

export function uploadProgress(page: Page, fixture: Fixture): Locator {
  return page.getByRole("progressbar", { name: `Upload progress for ${fixture.file}` });
}

/** Waits until every fixture has a tile whose analysis is done. */
export async function expectAnalyzed(page: Page, fixtures: readonly Fixture[]): Promise<void> {
  for (const fixture of fixtures) {
    await expect(analyzedTile(page, fixture)).toBeVisible({ timeout: ANALYSIS_TIMEOUT_MS });
  }
  await expect(imagesRegion(page).getByText("Analyzing…")).toHaveCount(0);
}

/** Waits until the picture has been fetched and decoded, not just placed in the page. */
export async function expectPictureLoaded(picture: Locator): Promise<void> {
  await expect
    .poll(() => picture.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0))
    .toBe(true);
}

/** Fails if the page can be scrolled sideways, which means something is wider than the screen. */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      ),
    )
    .toBeLessThanOrEqual(0);
}
