// Phone layouts for the flows people use away from the desk (#110): handling an alarm, exporting
// its clip, and a focused Live camera. Every step must fit the screen without sideways scrolling.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const PHONE = { width: 360, height: 740 };

/** Nothing on the page, or inside the open dialog, scrolls sideways; the element is fully on screen. */
async function expectOnScreen(page, locator, label) {
  const overflow = await page.evaluate(() => {
    const dialog = document.querySelector("[role=dialog]");
    return Math.max(document.documentElement.scrollWidth - window.innerWidth, dialog ? dialog.scrollWidth - dialog.clientWidth : 0);
  });
  expect(overflow, `${label}: scrolls sideways`).toBeLessThanOrEqual(0);
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  expect(box, `${label}: not rendered`).not.toBeNull();
  expect(box.x, `${label}: starts off screen`).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width, `${label}: ends off screen`).toBeLessThanOrEqual(PHONE.width + 0.5);
  // Touch targets stay comfortably tappable.
  expect(box.height, `${label}: too small to tap`).toBeGreaterThanOrEqual(32);
}

test.use({ viewport: PHONE, hasTouch: true, isMobile: true });

test("on a phone an operator handles an alarm and exports its clip", async ({ page }) => {
  seedFixtures(2, { alarm: true });
  await signInNext(page, "camera-operator");
  const chip = page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i });
  await chip.scrollIntoViewIfNeeded();
  await chip.tap();
  const inbox = page.getByRole("dialog", { name: "Alarms" });
  const row = inbox.getByRole("button", { name: /^Gate entry: Vehicle on Camera 001/ });
  await expectOnScreen(page, row, "alarm row");
  await row.tap();
  const detail = inbox.getByRole("article", { name: "Alarm: Gate entry" });
  for (const name of ["Open in Playback", "Acknowledge", "Export clip"]) await expectOnScreen(page, detail.getByRole("button", { name }), name);
  await expectOnScreen(page, detail.getByRole("img"), "snapshot");
  await expectOnScreen(page, detail.getByLabel("Alarm clip"), "clip");

  await detail.getByRole("button", { name: "Acknowledge" }).tap();
  await expect(detail).toContainText("Acknowledged");
  await detail.getByRole("button", { name: "Export clip" }).tap();
  await expect(detail.getByRole("status")).toContainText("Clip export queued.");
  await detail.getByRole("button", { name: "View export jobs" }).tap();
  const jobs = page.getByRole("list", { name: "Export jobs" });
  await expect(jobs.getByRole("listitem")).toHaveCount(1);
  await expectOnScreen(page, jobs.getByRole("listitem").first(), "export job");
});

test("on a phone a focused Live camera fills the width", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page, "camera-operator");
  await page.getByRole("button", { name: /^Camera 001\b/ }).first().tap();
  const tile = page.getByRole("group", { name: /^Camera 001:/ });
  await expect(tile).toBeVisible();
  await tile.getByRole("button", { name: "Enlarge Camera 001" }).tap();
  await expect(page.locator(".grid")).toHaveAttribute("data-columns", "1");
  await expectOnScreen(page, tile, "focused tile");
  const box = await tile.boundingBox();
  expect(box.width, "the focused camera uses the screen width").toBeGreaterThanOrEqual(PHONE.width - 32);
  await expectOnScreen(page, tile.getByRole("button", { name: "Restore grid" }), "restore");
});
