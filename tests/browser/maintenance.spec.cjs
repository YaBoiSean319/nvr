// Real-browser journey for maintenance windows (#234): start one on a camera, see it, raise an alarm that is kept but marked, end it, and see an operator can only open one on cameras.
const { test, expect, seedFixtures, raiseAlarm, signInNext } = require("./helpers.cjs");

test("an administrator starts maintenance on a camera, an alarm in it is kept and marked, and ending it is recorded", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/cameras");
  await page.getByRole("row", { name: /Camera 001/ }).first().click();
  await page.getByRole("button", { name: "Start maintenance (1 hour)" }).click();
  await expect(page.getByRole("link", { name: "In maintenance" })).toBeVisible();

  // The status bar says so, in every workspace, and its panel lists the window with a way to end it.
  const bar = page.getByRole("status", { name: "Operational status" });
  await expect(bar.getByRole("button", { name: "Maintenance: 1 camera" })).toBeVisible();
  await bar.getByRole("button", { name: "Maintenance: 1 camera" }).click();
  const drawer = page.getByRole("dialog", { name: "Operational status" });
  await expect(drawer.getByRole("heading", { name: "Maintenance" })).toBeVisible();
  await expect(drawer).toContainText("Work on Camera 001");
  await expect(drawer).toContainText("held back: 0 alarms");
  await drawer.getByRole("link", { name: "Start or manage maintenance" }).click();
  await expect(page).toHaveURL(/\/cameras\/maintenance$/);
  const active = page.getByRole("list", { name: "Active windows" });
  await expect(active).toContainText("Work on Camera 001");
  await expect(active).toContainText("Cameras: Camera 001");
  await expect(active).toContainText("0 alarms");

  raiseAlarm(1);
  await page.reload();
  await expect(page.getByRole("list", { name: "Active windows" })).toContainText("1 alarm (kept in the inbox, marked)");
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i }).click();
  const inbox = page.getByRole("dialog", { name: "Alarms" });
  await inbox.getByRole("list", { name: "Alarms" }).getByRole("button", { name: /^Gate entry: Vehicle on Camera 001|^Gate entry: Person on Camera 001/ }).click();
  await expect(inbox.getByRole("article", { name: "Alarm: Gate entry" })).toContainText("During maintenance (Work on Camera 001)");
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "End Work on Camera 001 now" }).click();
  await expect(page.getByRole("list", { name: "Recent windows" })).toContainText("Work on Camera 001");
  await expect(page.getByRole("list", { name: "Active windows" })).toHaveCount(0);
  await expect(bar.getByRole("button", { name: /^Maintenance:/ })).toHaveCount(0);  // No window, no chip
});

test("a window can be set for later, covers the whole site, and is cancelled before it starts", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/cameras/maintenance");
  const form = page.getByRole("form", { name: "Open a maintenance window" });
  await form.getByLabel("What is being done").fill("Switch replacement");
  await form.getByLabel("Covers").selectOption("site");
  await form.getByLabel("For how long").selectOption("240");
  const start = new Date(Date.now() + 3 * 3600_000);
  const local = new Date(start.getTime() - start.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
  await form.getByLabel(/^Starts/).fill(local);
  await form.getByRole("button", { name: "Start maintenance" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Switch replacement is set." })).toBeVisible();
  const upcoming = page.getByRole("list", { name: "Upcoming windows" });
  await expect(upcoming).toContainText("The whole site");
  await expect(page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /^Maintenance:/ })).toHaveCount(0);  // Not started: no chip yet
  await page.getByRole("button", { name: "Cancel Switch replacement" }).click();
  await expect(page.getByRole("list", { name: "Upcoming windows" })).toHaveCount(0);
});

test("a window cannot start in the past, and an administrator is offered up to seven days", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await page.goto("/cameras/maintenance");
  const form = page.getByRole("form", { name: "Open a maintenance window" });
  await form.getByLabel("What is being done").fill("Too long");
  await form.getByLabel("Covers").selectOption("site");
  await expect(form.getByLabel("For how long")).toContainText("7 days");
  await form.getByLabel("For how long").selectOption("10080");
  await form.getByLabel(/^Starts/).fill("2000-01-01T00:00");
  await form.getByRole("button", { name: "Start maintenance" }).click();
  await expect(page.getByRole("alert")).toContainText("cannot start in the past");
});

test("a window on the whole site says so in the status bar", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await page.goto("/cameras/maintenance");
  const form = page.getByRole("form", { name: "Open a maintenance window" });
  await form.getByLabel("What is being done").fill("Power work");
  await form.getByLabel("Covers").selectOption("site");
  await form.getByRole("button", { name: "Start maintenance" }).click();
  const bar = page.getByRole("status", { name: "Operational status" });
  await expect(bar.getByRole("button", { name: "Maintenance: whole site" })).toBeVisible();
  await expect(page.getByText("The whole site is in maintenance: no alerts are being sent.")).toBeVisible();
  await page.getByRole("button", { name: "End Power work now" }).click();
  await expect(bar.getByRole("button", { name: /^Maintenance:/ })).toHaveCount(0);
});
