// Capacity planner (#149): a plan gives hand-checkable figures with the assumptions listed, the page says it is an estimate and not a certification, starting from the
// installation's cameras labels what had to be assumed, and the result can be exported.
const fs = require("node:fs");
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

test("a plan gives the worked figures, lists its assumptions, says what it is, and exports", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/settings/capacity");
  await expect(page.getByRole("note")).toContainText("not a certification of capacity");

  await page.getByLabel("Cameras in row 1").fill("10");
  await page.getByLabel("Bitrate of row 1").fill("4096");
  await page.getByLabel("Days kept for row 1").fill("30");
  await page.getByLabel("People watching Live at once").fill("5");
  const metric = key => page.locator(`tr[data-metric="${key}"]`);
  // 10 x 4096 kbps for 30 days is 13.27 TB; with overhead 1.05 and headroom 1.2, 16.72 TB.
  await expect(metric("disk_footage")).toContainText("13.27 TB", { timeout: 15_000 });
  await expect(metric("disk_recommended")).toContainText("16.72 TB");
  await expect(metric("network_in")).toContainText("40.96 Mbps");
  await expect(metric("network_out")).toContainText("20.48 Mbps");
  await expect(metric("disk_recommended")).toContainText("headroom");  // Each figure names what it assumes
  await expect(page.getByRole("region", { name: "Assumptions" })).toContainText("Headroom added to the recommended disk size");

  const [download] = await Promise.all([page.waitForEvent("download"), page.getByRole("button", { name: "Download CSV" }).click()]);
  const csv = fs.readFileSync(await download.path(), "utf8");
  expect(csv).toContain("not a certification of capacity");
  expect(csv).toContain("13271040000000");
  expect(csv).toContain("1920 x 1080");  // The plan's own inputs are in it, not just the results
  expect(csv).toContain("People watching Live at once");
  expect(csv).toContain("Assumes");
  expect(download.suggestedFilename()).toBe("capacity-estimate.csv");
});

test("starting from the cameras here labels the bitrates that had to be assumed", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  await page.goto("/settings/capacity");
  await page.getByRole("button", { name: "Start from current cameras" }).click();
  await expect(page.getByRole("table", { name: "Cameras in the plan" }).locator("tbody tr")).toHaveCount(3);
  await expect(page.getByTestId("source-0")).toHaveText(/assumed/, { timeout: 15_000 });  // No footage was measured for the fixture cameras
  await expect(page.getByRole("region", { name: "Assumptions" })).toContainText("assumed:");
});
