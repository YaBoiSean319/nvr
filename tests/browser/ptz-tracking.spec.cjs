// PTZ auto-tracking (#145): an operator picks a person, the server steers the camera under the control lease, manual control interrupts it at once,
// and a lost target sends the camera to its home preset.
const { test, expect, seedFixtures, seedTracking, signInNext } = require("./helpers.cjs");
const { startPtzDevice } = require("./ptz_device.cjs");

let device;
test.beforeEach(async () => { device = await startPtzDevice(); });
test.afterEach(async () => { await device.close(); });

async function openPtzTab(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "PTZ" }).click();
}

test("an operator has the camera follow a person, a lost person sends it home", async ({ page }) => {
  seedFixtures(2, { devices: { 1: device.url } });
  await signInNext(page);
  await openPtzTab(page);

  const presets = page.getByRole("region", { name: "Presets" });
  await presets.getByLabel("Save the current position as").fill("Home");
  await presets.getByRole("button", { name: "Save preset" }).click();
  await expect(presets.getByLabel("Name of preset Home")).toBeVisible();

  const settings = page.getByRole("form", { name: "Auto-tracking" });
  await settings.getByLabel("Home preset").selectOption({ label: "Home" });
  await settings.getByLabel("Object counts as lost after (seconds)").fill("1");
  await settings.getByLabel("Return home after being lost for (seconds)").fill("5");
  await settings.getByRole("button", { name: "Save auto-tracking" }).click();
  await expect(page.getByText("Auto-tracking saved.")).toBeVisible();

  seedTracking();
  const pad = page.getByRole("group", { name: "Camera movement" });
  await pad.getByRole("button", { name: "Follow" }).click();
  await page.getByRole("list", { name: "Objects in view" }).getByRole("button", { name: /^Follow person/ }).click();
  await expect(pad.getByRole("status").filter({ hasText: "Following the person" })).toBeVisible();
  // The person is to the right of the picture: the camera is told to pan right.
  await expect.poll(() => device.commands.some(command => command.op === "ContinuousMove" && Number(command.pan_tilt?.x) > 0)).toBe(true);

  // Nobody sees the person any more: the camera stops, then goes home and lets go of the control.
  await expect(pad.getByRole("status").filter({ hasText: "Lost the person" })).toBeVisible({ timeout: 20_000 });
  await expect.poll(() => device.commands.filter(command => command.op === "GotoPreset").map(command => command.token), { timeout: 20_000 }).toEqual(["preset-1"]);
  await expect(pad.getByRole("status").filter({ hasText: /the person/ })).toHaveCount(0, { timeout: 15_000 });
});

test("moving the camera by hand ends the tracking at once", async ({ page }) => {
  seedFixtures(2, { devices: { 1: device.url } });
  await signInNext(page);
  await openPtzTab(page);
  seedTracking();
  const pad = page.getByRole("group", { name: "Camera movement" });
  await pad.getByRole("button", { name: "Follow" }).click();
  await page.getByRole("list", { name: "Objects in view" }).getByRole("button", { name: /^Follow person/ }).click();
  await expect(pad.getByRole("status").filter({ hasText: "Following the person" })).toBeVisible();

  await pad.getByRole("button", { name: "Pan left" }).tap();
  await expect(pad.getByRole("status").filter({ hasText: /the person/ })).toHaveCount(0, { timeout: 10_000 });
  const sent = device.commands.length;
  await page.waitForTimeout(2_000);
  // Only the person's own moves reached the camera after that: nothing from the tracker.
  expect(device.commands.slice(sent).every(command => command.op === "Stop")).toBe(true);
});
