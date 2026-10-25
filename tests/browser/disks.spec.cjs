// Real-browser journeys for disk health and the retention forecast (#123).
const { test, expect, seedFixtures, signInNext, writeHistory, smartReading } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

async function openStorage(page) {
  await nav(page).getByRole("link", { name: "Settings" }).click();
  await page.getByRole("navigation", { name: "Settings categories" }).getByRole("link", { name: "Recording & storage" }).click();
}

test("disk health says 'not available' when the disk cannot be read, and a failing disk raises an incident", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openStorage(page);
  const health = page.getByRole("region", { name: "Disk health" });
  await expect(health).toContainText("Disk health is not available");

  // The browser environment has no disk passed through to read, so asking now is answered honestly.
  await health.getByRole("button", { name: "Check disks now" }).click();
  await expect(health.getByRole("table", { name: "Disk health by volume" })).toContainText("Not available");
  await expect(health.getByRole("table")).toContainText("Not available:");
  expect((await (await page.request.get("/api/incidents")).json()).incidents.filter(item => item.fault_type === "disk_health")).toEqual([]);

  smartReading("failing");
  await page.reload();
  await openStorage(page);
  const row = page.getByRole("region", { name: "Disk health" }).getByRole("row", { name: /default/ });
  await expect(row).toContainText("Failing");
  await expect(row).toContainText("Fixture HDD 4TB");
  await expect(row).toContainText("The drive's own health assessment is FAILED");
  await expect(row).toContainText("24 sectors pending reallocation");
  const incidents = (await (await page.request.get("/api/incidents")).json()).incidents.filter(item => item.fault_type === "disk_health" && item.fault_active);
  expect(incidents).toHaveLength(1);

  smartReading("healthy");
  await page.reload();
  await openStorage(page);
  await expect(page.getByRole("region", { name: "Disk health" }).getByRole("row", { name: /default/ })).toContainText("No problems reported");
  expect((await (await page.request.get("/api/incidents")).json()).incidents.filter(item => item.fault_type === "disk_health" && item.fault_active)).toEqual([]);
});

test("a camera that will not keep its policy is shown in Settings and on its Recording tab, with what to change", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page);
  writeHistory(1, 20); // 20 TB a day: more than any disk holds for 30 days.
  await openStorage(page);
  const forecast = page.getByRole("region", { name: "Retention forecast" });
  const table = forecast.getByRole("table", { name: "Retention forecast by camera" });
  await expect(table.getByRole("row", { name: /Camera 001/ })).toContainText(/keeps about \d+ days, below its 30-day policy/);
  await expect(table.getByRole("row", { name: /Camera 002/ })).toContainText("Collecting data");
  await expect(forecast).toContainText("To fix Camera 001:");
  await expect(forecast).toContainText("Lower Camera 001's bitrate");

  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "Recording" }).click();
  const section = page.getByRole("region", { name: "Retention forecast" });
  await expect(section.getByRole("note")).toContainText(/Camera 001 keeps about \d+ days, below its 30-day policy/);
  await expect(section.getByRole("list", { name: "Ways to keep the policy" })).toContainText("Lower Camera 001's bitrate");
  const api = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[0]}/retention-forecast`)).json();
  expect(api.camera.below_policy).toBe(true);
  expect(api.camera.days_until_shortfall).toBeLessThanOrEqual(7); // A shortfall that near is an incident.
});
