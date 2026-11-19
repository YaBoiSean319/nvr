// Real-browser journey for the security log (#367).
const { test, expect, seedFixtures, signInNext, settingsLink } = require("./helpers.cjs");

test("an administrator reads the security log, filters it by action and person, downloads it and sets how long it is kept", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  // Something to find: a change to the display settings, made by this administrator.
  const display = await (await page.request.get("/api/settings/display")).json();
  const changed = { time_zone: display.time_zone === "Europe/London" ? "UTC" : "Europe/London", date_format: display.date_format, clock_format: display.clock_format };
  expect((await page.request.put("/api/settings/display", { data: changed })).status()).toBe(200);

  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Settings" }).click();
  await (await settingsLink(page, "Security log")).click();
  const log = page.getByRole("region", { name: "Security log", exact: true });
  const table = log.getByRole("table", { name: "Security log entries" });
  await expect(table).toBeVisible();

  await log.getByLabel("Action").selectOption("display_settings_changed");
  await log.getByLabel("Person").selectOption({ label: "camera-admin" });
  const row = table.getByRole("row").nth(1);
  await expect(row).toContainText("camera-admin");
  await expect(row).toContainText("Display settings changed");
  await expect(row).toContainText(changed.time_zone);
  await expect(table.getByRole("row")).toHaveCount(2); // The heading and the one change

  await log.getByLabel("Person").selectOption({ label: "camera-operator" });
  await expect(log.getByText("Nothing was recorded in this range.")).toBeVisible();
  await log.getByLabel("Person").selectOption({ label: "camera-admin" });

  const [download] = await Promise.all([page.waitForEvent("download"), log.getByRole("link", { name: "Download as CSV" }).click()]);
  expect(download.suggestedFilename()).toMatch(/^security-log-\d{8}-\d{8}\.csv$/);

  const retention = page.getByRole("region", { name: "Security log retention" });
  await retention.getByLabel("Keep security log entries for (days)").fill("365");
  await retention.getByRole("button", { name: "Save retention" }).click();
  await expect(retention.getByRole("status")).toContainText("Kept for 365 days");
  expect((await page.request.put("/api/settings/display", { data: display })).status()).toBe(200);
});
