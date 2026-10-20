import { expect, test } from "@playwright/test";
import { newAccount } from "./support/accounts";
import { ALL_FIXTURES, RED } from "./support/fixtures";
import {
  analyzedTile,
  expectAnalyzed,
  expectPictureLoaded,
  expectNoHorizontalScroll,
  imageModal,
  searchBox,
  signIn,
  signOut,
  signUp,
  tiles,
  uploadFixtures,
} from "./support/gallery";
import { saveScreenshot } from "./support/screenshots";

/**
 * The phone layout on a Pixel 7: sign in, browse, open an image. The pages must never scroll
 * sideways, and the modal must fit the screen. The screenshots go to docs/screenshots/ for the
 * README.
 */
test("a phone can sign in, browse the gallery and open an image", async ({ page, viewport }) => {
  const account = newAccount("phone");
  expect(viewport?.width).toBeLessThan(500);

  await test.step("set up the account, then sign out", async () => {
    await page.goto("/sign-up");
    await signUp(page, account);
    await expect(page.getByRole("heading", { name: "Upload your first image" })).toBeVisible();
    await expectNoHorizontalScroll(page);
    await saveScreenshot(page, "mobile-gallery-empty");
    await signOut(page);
  });

  await test.step("sign in on the phone", async () => {
    await expectNoHorizontalScroll(page);
    await saveScreenshot(page, "mobile-sign-in");
    await signIn(page, account);
    await expect(page.getByRole("heading", { name: "Upload your first image" })).toBeVisible();
  });

  await test.step("upload and browse", async () => {
    await uploadFixtures(page, ALL_FIXTURES);
    await expect(tiles(page)).toHaveCount(3);
    await expectAnalyzed(page, ALL_FIXTURES);
    for (const fixture of ALL_FIXTURES) {
      await expectPictureLoaded(analyzedTile(page, fixture).getByRole("img"));
    }
    await expectNoHorizontalScroll(page);
    await saveScreenshot(page, "mobile-gallery");

    await searchBox(page).fill("red");
    await expect(page).toHaveURL(/[?&]q=red(&|$)/);
    await expect(tiles(page)).toHaveCount(2);
    await expectNoHorizontalScroll(page);
    await saveScreenshot(page, "mobile-search");
  });

  await test.step("open an image", async () => {
    await analyzedTile(page, RED).tap();
    const modal = imageModal(page);
    await expect(modal).toBeVisible();
    await expect(modal.getByRole("heading", { name: RED.file })).toBeVisible();
    await expect(modal.getByText(RED.description, { exact: true })).toBeVisible();
    await expectPictureLoaded(modal.getByRole("img", { name: RED.description }));
    await expect(modal.getByRole("button", { name: "Find similar" })).toBeVisible();

    // The dialog fits the screen, and neither it nor the page behind it scrolls sideways.
    await expectNoHorizontalScroll(page);
    await expect
      .poll(() => modal.evaluate((element) => element.scrollWidth - element.clientWidth))
      .toBeLessThanOrEqual(0);
    const box = await modal.boundingBox();
    expect(box).not.toBeNull();
    expect(box?.x).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(viewport?.width ?? 0);
    await saveScreenshot(page, "mobile-modal");

    await modal.getByRole("button", { name: "Close" }).tap();
    await expect(modal).toBeHidden();
  });
});
