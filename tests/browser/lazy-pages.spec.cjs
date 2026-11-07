// Pages that load on demand (#281): what a person sees when a script file is gone because the NVR was upgraded under an open tab.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

// Without the worker, so the page's own requests are the ones the test can see and fail.
test.use({ serviceWorkers: "block" });

test("a page script that is gone reloads once to the new release, then loads", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  let attempts = 0;
  await page.route(/\/assets\/SettingsWorkspace-[\w-]+\.js$/, route => (++attempts === 1 ? route.abort() : route.continue()));
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "General" })).toBeVisible();
  expect(attempts, "refused once, then fetched again after the reload").toBe(2);
});

test("a page script that stays gone is an error, not a reload loop", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  let attempts = 0;
  await page.route(/\/assets\/SettingsWorkspace-[\w-]+\.js$/, route => { attempts += 1; return route.abort(); });
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await expect.poll(() => attempts).toBe(2);
  await page.waitForTimeout(1500);
  expect(attempts, "one retry through a reload, then it stops").toBe(2);
});
