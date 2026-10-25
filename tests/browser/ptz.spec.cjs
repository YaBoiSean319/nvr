// PTZ (#114): moving a camera through the server by buttons, keyboard and touch, presets and tours
// on the camera's PTZ tab, and the control lease between people.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");
const { startPtzDevice } = require("./ptz_device.cjs");

let device;
test.beforeEach(async () => { device = await startPtzDevice(); });
test.afterEach(async () => { await device.close(); });

async function openPtzTab(page) {
  await page.getByRole("navigation", { name: "Workspaces" }).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: "Camera 001", exact: true }).click();
  await page.getByRole("tab", { name: "PTZ" }).click();
}

for (const width of [360, 1440]) {
  test(`an administrator moves the camera and sets up presets and a tour at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width === 360 ? 740 : 1000 });
    seedFixtures(2, { devices: { 1: device.url } });
    await signInNext(page);
    await openPtzTab(page);
    const pad = page.getByRole("group", { name: "Camera movement" });

    // Press and hold by keyboard, then by touch: each hold moves, each release stops.
    const right = pad.getByRole("button", { name: "Pan right" });
    await right.focus();
    await page.keyboard.down("Enter");
    await expect.poll(() => device.ops()).toContain("ContinuousMove");
    await page.keyboard.up("Enter");
    await expect.poll(() => device.ops()).toEqual(["ContinuousMove", "Stop"]);
    expect(device.commands[0].pan_tilt).toEqual({ x: "0.5000", y: "0.0000" });
    await pad.getByRole("button", { name: "Zoom in" }).tap();
    await expect.poll(() => device.ops().slice(2)).toEqual(["ContinuousMove", "Stop"]);
    expect(device.commands[2].zoom).toEqual({ x: "0.5000" });
    for (const control of await pad.getByRole("button").all()) {
      const box = await control.boundingBox();
      expect(box.width).toBeGreaterThanOrEqual(40);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }

    const presets = page.getByRole("region", { name: "Presets" });
    for (const name of ["Gate", "Yard"]) {
      await presets.getByLabel("Save the current position as").fill(name);
      await presets.getByRole("button", { name: "Save preset" }).click();
      await expect(presets.getByLabel(`Name of preset ${name}`)).toBeVisible();
    }
    await presets.getByLabel("Name of preset Gate").fill("Front gate");
    await presets.getByRole("button", { name: "Rename" }).first().click();
    await expect(presets.getByLabel("Name of preset Front gate")).toBeVisible();
    await presets.getByRole("listitem").filter({ has: page.getByLabel("Name of preset Yard") }).getByRole("button", { name: "Go" }).click();
    await expect.poll(() => device.commands.filter(command => command.op === "GotoPreset").map(command => command.token)).toEqual(["preset-2"]);

    const tours = page.getByRole("region", { name: "Tours" });
    await tours.getByRole("button", { name: "New tour" }).click();
    const dialog = page.getByRole("dialog", { name: "New tour" });
    await dialog.getByLabel("Name", { exact: true }).fill("Patrol");
    await dialog.getByRole("button", { name: "Save tour" }).click();
    await expect(tours).toContainText("Patrol");
    await expect(tours).toContainText("Front gate 30s → Yard 30s");
  });
}

test("one person controls a camera at a time, from Live by keyboard, and an administrator can take over", async ({ page, browser }) => {
  seedFixtures(2, { devices: { 1: device.url } });
  await signInNext(page, "camera-operator");
  await page.getByRole("button", { name: /^Camera 001\b/ }).click();
  const tile = page.getByRole("group", { name: /^Camera 001:/ });
  await tile.focus();
  const pad = tile.getByRole("group", { name: "Camera movement" });
  await expect(pad).toBeVisible();
  // Arrow keys on the focused tile move the camera; letting go stops it.
  await page.keyboard.down("ArrowLeft");
  await expect.poll(() => device.ops()).toContain("ContinuousMove");
  await page.keyboard.up("ArrowLeft");
  await expect.poll(() => device.ops()).toEqual(["ContinuousMove", "Stop"]);
  expect(device.commands[0].pan_tilt).toEqual({ x: "-0.5000", y: "0.0000" });

  // An administrator sees who holds control and takes it over.
  const context = await browser.newContext();
  const admin = await context.newPage();
  await signInNext(admin);
  await openPtzTab(admin);
  const adminPad = admin.getByRole("group", { name: "Camera movement" });
  await expect(adminPad.getByRole("status")).toContainText("Controlled by camera-operator");
  await expect(adminPad.getByRole("button", { name: "Pan right" })).toBeDisabled();
  await adminPad.getByRole("button", { name: "Take over" }).click();
  await expect(adminPad.getByRole("button", { name: "Pan right" })).toBeEnabled();
  await context.close();

  // The operator is now refused, and told who has control.
  await expect(pad.getByRole("status")).toContainText("Controlled by camera-admin", { timeout: 10_000 });
  await expect(pad.getByRole("button", { name: "Pan left" })).toBeDisabled();
});
