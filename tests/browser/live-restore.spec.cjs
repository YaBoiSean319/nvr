// A camera clicked before the person's saved workspace has come back from the server is kept when it arrives (#205).
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("a camera clicked while the saved workspace is still loading is not dropped when it arrives", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route("**/api/live/workspace", async route => {
    if (route.request().method() !== "GET") return route.continue();
    await held;
    await route.continue();
  });
  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await page.getByRole("button", { name: /^Camera 002\b/ }).click();
  await expect(page.getByRole("status", { name: "Restoring your workspace" }).or(page.getByLabel("Restoring your workspace"))).toBeVisible();
  release();
  await expect(page.locator(".tile[data-camera-id]")).toHaveCount(2);
  await expect(page.getByText("2/16 in view")).toBeVisible();
});

test("opening Live with nothing clicked restores the saved workspace as it was", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  await page.goto("/live");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  await expect(page.locator(".tile[data-camera-id]")).toHaveCount(1);
  await expect.poll(async () => ((await (await page.request.get("/api/live/workspace")).json()).camera_ids ?? []).length).toBe(1);
  await page.reload();
  await expect(page.locator(".tile[data-camera-id]")).toHaveCount(1);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  test("a camera chosen before the workspace arrives is not hidden under the camera browser", async ({ page }) => {
    seedFixtures(2);
    await signInNext(page);
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route("**/api/live/workspace", async route => {
      if (route.request().method() !== "GET") return route.continue();
      await held;
      await route.continue();
    });
    await page.goto("/live");
    await page.getByRole("button", { name: /^Camera 001\b/ }).tap();
    release();
    await expect(page.locator(".tile[data-camera-id]")).toHaveCount(1);
    await expect(page.getByRole("complementary", { name: "Camera browser" })).toBeHidden();
  });
});
