// Real-browser journeys for the Operational status panel (#257): one section at a time, counts on the tabs, the worst first and the rest folded.
const { test, expect, seedFixtures, seedStatus, signInNext } = require("./helpers.cjs");

const bar = page => page.getByRole("status", { name: "Operational status" });
const drawer = page => page.getByRole("dialog", { name: "Operational status" });

test("the panel opens on the section of the chip pressed, shows one section at a time and counts what needs a look", async ({ page }) => {
  seedFixtures(1);
  seedStatus();
  await signInNext(page);
  await bar(page).getByRole("button", { name: "Incidents: 11" }).click();
  const panel = drawer(page);
  await expect(panel.getByRole("tab", { name: /^Incidents/ })).toHaveAttribute("aria-selected", "true");
  await expect(panel.getByRole("tab", { name: /^Incidents/ })).toContainText("11");
  await expect(panel.getByRole("tab", { name: /^Exports/ })).toContainText("2"); // One running, one failed in the last day
  // Only the open section is on the page.
  await expect(panel.getByRole("heading", { name: "Incidents" })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "Export jobs" })).toHaveCount(0);
  await expect(panel.getByRole("heading", { name: "Recording" })).toHaveCount(0);
  // Another section is one press away, and arrow keys move between tabs.
  await panel.getByRole("tab", { name: /^Exports/ }).click();
  await expect(panel.getByRole("heading", { name: "Export jobs" })).toBeVisible();
  await expect(panel.getByRole("heading", { name: "Incidents" })).toHaveCount(0);
  await panel.getByRole("tab", { name: /^Exports/ }).press("ArrowLeft");
  await expect(panel.getByRole("tab", { name: /^Incidents/ })).toBeFocused();
});

test("incidents: the active ones first, a filter, the rest behind a button, and all the active ones acknowledged at once", async ({ page }) => {
  seedFixtures(1);
  seedStatus();
  await signInNext(page);
  await bar(page).getByRole("button", { name: "Incidents: 11" }).click();
  const panel = drawer(page);
  const rows = panel.getByRole("list", { name: "Open incidents" }).getByRole("listitem");
  await expect(panel.getByText("6 active · 5 acknowledged")).toBeVisible();
  await expect(rows).toHaveCount(8); // The first eight of eleven
  await expect(rows.first()).toContainText("Active"); // Active before acknowledged
  await panel.getByRole("button", { name: "Show 3 more" }).click();
  await expect(rows).toHaveCount(11);
  await panel.getByRole("button", { name: "Acknowledged", exact: true }).click();
  await expect(rows).toHaveCount(5);
  await panel.getByRole("button", { name: "Active", exact: true }).click();
  await expect(rows).toHaveCount(6);
  await panel.getByRole("button", { name: "Acknowledge all 6 active" }).click();
  await expect(panel.getByText("0 active · 11 acknowledged")).toBeVisible();
  await expect(panel.getByRole("button", { name: "Acknowledge all" })).toHaveCount(0);
});

test("export jobs: what is running or failed first, and only the newest finished ones until asked", async ({ page }) => {
  seedFixtures(1);
  seedStatus();
  await signInNext(page);
  await bar(page).getByRole("button", { name: /^Exporting/ }).click();
  const jobs = drawer(page).getByRole("list", { name: "Export jobs" }).getByRole("listitem");
  await expect(jobs).toHaveCount(5); // Running, failed and the three newest finished
  await expect(jobs.nth(0)).toHaveAttribute("data-status", "running");
  await expect(jobs.nth(1)).toHaveAttribute("data-status", "failed");
  await drawer(page).getByRole("button", { name: "Show 3 older finished exports" }).click();
  await expect(jobs).toHaveCount(8);
});

test("on a phone the tabs sit at the top, scroll sideways inside their own strip and the page does not", async ({ page }) => {
  seedFixtures(1);
  seedStatus();
  await page.setViewportSize({ width: 390, height: 800 });
  await signInNext(page);
  await bar(page).getByRole("button", { name: "Incidents: 11" }).click();
  const panel = drawer(page);
  await expect(panel.getByRole("tab", { name: /^Incidents/ })).toBeVisible();
  await panel.getByRole("tab", { name: /^Maintenance/ }).click();
  await expect(panel.getByRole("heading", { name: "Maintenance" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
