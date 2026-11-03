// Live start-up times (#216): the tile records how long its first picture took, the server records how long each viewer waited and where the time went, and Settings > Diagnostics shows both.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("how long live video took to start is measured in the tile and reported under Diagnostics", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(1);
  await signInNext(page);
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const tile = page.getByRole("group", { name: /^Camera 001:/ });
  await expect(tile).toHaveAttribute("data-phase", "live", { timeout: 25_000 });
  const firstFrame = Number(await tile.getAttribute("data-first-frame-ms"));
  expect(firstFrame).toBeGreaterThan(0);
  expect(firstFrame).toBeLessThan(25_000);

  const report = await (await page.request.get("/api/system/live-startup")).json();
  expect(report.summary.count).toBeGreaterThan(0);
  const first = report.recent.find(entry => entry.camera === "Camera 001");
  expect(first.first_media_ms).toBeGreaterThan(0);
  expect(first.first_media_ms).toBeLessThanOrEqual(firstFrame + 1_000);  // The browser's wait includes the server's

  await page.goto("/settings/diagnostics");
  const section = page.getByRole("region", { name: "Live video start-up" });
  await expect(section.getByRole("table", { name: "Live start-up times" })).toContainText("Started the camera's stream");
  await expect(section.getByRole("table", { name: "Recent live starts" })).toContainText("Camera 001");
  await expect(section.getByRole("table", { name: "Recent live starts" })).toContainText(/until its first keyframe|Joined a stream that was already running/);
});

test("before any live video has been opened, Diagnostics says so", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/settings/diagnostics");
  await expect(page.getByRole("region", { name: "Live video start-up" })).toContainText("No live video has been opened since the NVR started.");
});
