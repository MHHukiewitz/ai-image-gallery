import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import { newAccount } from "./support/accounts";
import { ALL_FIXTURES, BLUE, RED, RED2 } from "./support/fixtures";
import {
  analyzedTile,
  expectAnalyzed,
  expectPictureLoaded,
  imageModal,
  imagesRegion,
  searchBox,
  signOut,
  signUp,
  tiles,
  uploadFixtures,
  uploadProgress,
} from "./support/gallery";
import { saveScreenshot } from "./support/screenshots";

type MarkedWindow = Window & { e2eMarker?: string };

/** Sets a value on `window` that only the current document can still have. */
async function markDocument(page: Page, marker: string): Promise<void> {
  await page.evaluate((value) => Object.assign(window, { e2eMarker: value }), marker);
}

async function readMarker(page: Page): Promise<string | undefined> {
  return page.evaluate(() => (window as MarkedWindow).e2eMarker);
}

/**
 * The whole product in one run: a new user uploads three pictures, waits for the analysis,
 * searches three ways, signs out, and a second user sees none of it. The steps share the
 * browser, the account and the uploaded images, so they live in one test. README.md maps each
 * challenge requirement to the step that asserts it.
 */
test("sign up, upload, analyze, search by text, similarity and color, sign out, stay isolated", async ({
  page,
}) => {
  const userA = newAccount("a");
  const userB = newAccount("b");
  const grid = imagesRegion(page);
  let redImageId = "";

  await test.step("1. sign up and land on the empty gallery", async () => {
    // Nobody is signed in, so the gallery sends the visitor to sign in.
    await page.goto("/");
    await expect(page).toHaveURL(/\/sign-in$/);
    await page.getByRole("link", { name: "Create an account" }).click();
    await expect(page).toHaveURL(/\/sign-up$/);

    await signUp(page, userA);
    await expect(page.getByRole("heading", { name: "Upload your first image" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Drop your photos here" })).toBeVisible();
    await expect(grid).toHaveCount(0);
    await page.getByRole("button", { name: "Account menu" }).click();
    await expect(page.getByText(userA.email, { exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
  });

  await test.step("2. upload three images at once and see the progress", async () => {
    await uploadFixtures(page, ALL_FIXTURES);
    for (const fixture of ALL_FIXTURES) {
      await expect(uploadProgress(page, fixture)).toBeVisible();
    }
    // A finished upload stays in the list for 3 s before it clears itself.
    for (const fixture of ALL_FIXTURES) {
      await expect(uploadProgress(page, fixture)).toHaveAttribute("aria-valuenow", "100");
    }
  });

  await test.step("3. the three tiles appear, then are analyzed", async () => {
    await expect(tiles(page)).toHaveCount(3);
    await expectAnalyzed(page, ALL_FIXTURES);
    // The thumbnails come from Storage through signed URLs, so check they really render.
    for (const fixture of ALL_FIXTURES) {
      await expectPictureLoaded(analyzedTile(page, fixture).getByRole("img"));
    }

    // Hovering a tile shows its first tags. The tags are decoration over the picture, so the
    // visible state is the opacity of the strip that holds them.
    await analyzedTile(page, RED).hover();
    const hoveredTag = analyzedTile(page, RED).getByText("red", { exact: true });
    await expect(hoveredTag.locator("..")).toHaveCSS("opacity", "1");
    await saveScreenshot(page, "desktop-gallery");
  });

  const marker = randomUUID();
  await test.step("4. typing in the search filters the grid without a reload", async () => {
    await markDocument(page, marker);

    await searchBox(page).pressSequentially("red");
    await expect(page).toHaveURL(/[?&]q=red(&|$)/);
    // The two red images stay and the blue one goes.
    await expect(tiles(page)).toHaveCount(2);
    await expect(analyzedTile(page, RED)).toBeVisible();
    await expect(analyzedTile(page, RED2)).toBeVisible();
    await expect(analyzedTile(page, BLUE)).toHaveCount(0);

    // Still the document the marker was set on: the search never navigated away.
    expect(await readMarker(page)).toBe(marker);
  });

  await test.step("5. the modal shows the description, 5 to 10 tags and 1 to 3 swatches", async () => {
    await analyzedTile(page, RED).click();
    await expect(page).toHaveURL(/[?&]image=\d+/);
    redImageId = new URL(page.url()).searchParams.get("image") ?? "";
    expect(redImageId).toMatch(/^\d+$/);

    const modal = imageModal(page);
    await expect(modal.getByRole("heading", { name: RED.file })).toBeVisible();
    await expect(modal.getByText(RED.description, { exact: true })).toBeVisible();
    await expectPictureLoaded(modal.getByRole("img", { name: RED.description }));

    // The tag list is the one that holds the "red" tag; the color list has swatches instead.
    const tagButtons = modal
      .getByRole("list")
      .filter({ has: page.getByRole("button", { name: "red", exact: true }) })
      .getByRole("button");
    await expect.poll(() => tagButtons.count()).toBeGreaterThanOrEqual(5);
    await expect.poll(() => tagButtons.count()).toBeLessThanOrEqual(10);

    const swatches = modal.getByRole("button", { name: /^Filter by color #[0-9a-f]{6}$/i });
    await expect.poll(() => swatches.count()).toBeGreaterThanOrEqual(1);
    await expect.poll(() => swatches.count()).toBeLessThanOrEqual(3);
    await saveScreenshot(page, "desktop-modal");
  });

  await test.step("6. find similar lists red2 and leaves red out", async () => {
    await imageModal(page).getByRole("button", { name: "Find similar" }).click();
    await expect(imageModal(page)).toBeHidden();
    await expect(page).toHaveURL(new RegExp(`[?&]similar_to=${redImageId}(&|$)`));

    const filterChip = page.getByRole("group", { name: "Active filter" });
    await expect(filterChip).toContainText("Similar to");
    await expect(filterChip.getByRole("img", { name: RED.file })).toBeVisible();

    // The text-search results are still on screen until the similar ones arrive, so wait for
    // red to leave before looking for red2.
    await expect(analyzedTile(page, RED)).toHaveCount(0);
    await expect(analyzedTile(page, RED2)).toBeVisible();
    // The closest match comes first.
    await expect(tiles(page).first().getByRole("button")).toHaveAccessibleName(RED2.description);
    await saveScreenshot(page, "desktop-similar");
  });

  await test.step("7. a swatch of the blue image finds the blue image", async () => {
    await page.getByRole("button", { name: "Clear similar images filter" }).click();
    await expect(page).not.toHaveURL(/similar_to=/);
    await expect(tiles(page)).toHaveCount(3);

    await analyzedTile(page, BLUE).click();
    const modal = imageModal(page);
    await expect(modal.getByRole("heading", { name: BLUE.file })).toBeVisible();
    const firstSwatch = modal
      .getByRole("button", { name: /^Filter by color #[0-9a-f]{6}$/i })
      .first();
    const swatchLabel = await firstSwatch.getAttribute("aria-label");
    const hex = swatchLabel?.match(/#[0-9a-f]{6}/i)?.[0].toLowerCase();
    expect(hex).toBeDefined();
    await firstSwatch.click();

    await expect(modal).toBeHidden();
    await expect(page).toHaveURL(
      new RegExp(`[?&]color=${encodeURIComponent(hex ?? "")}(&|$)`, "i"),
    );
    await expect(page.getByRole("group", { name: "Active filter" })).toContainText("Color");

    // The earlier, unfiltered grid stays on screen until the color results arrive.
    await expect(analyzedTile(page, RED)).toHaveCount(0);
    await expect(analyzedTile(page, BLUE)).toBeVisible();
    await expect(tiles(page)).toHaveCount(1);

    // Nothing from step 4 to here reloaded the page.
    expect(await readMarker(page)).toBe(marker);
  });

  await test.step("8. sign out, and a second user sees none of it", async () => {
    await signOut(page);

    await page.getByRole("link", { name: "Create an account" }).click();
    await expect(page).toHaveURL(/\/sign-up$/);
    await signUp(page, userB);
    // Sign-up returns to the page the first user left, filter included, so nothing about the
    // second user's gallery is known from the URL. It has no tiles either way.
    await expect(tiles(page)).toHaveCount(0);
    await expect(grid).toHaveCount(0);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Upload your first image" })).toBeVisible();
    await expect(grid).toHaveCount(0);

    // Neither a search, nor the first user's image address, reaches into their gallery.
    await page.goto("/?q=red");
    await expect(page.getByRole("heading", { name: "No matches for “red”" })).toBeVisible();
    await page.goto(`/?image=${redImageId}`);
    await expect(page.getByRole("heading", { name: "Image not found" })).toBeVisible();
  });
});
