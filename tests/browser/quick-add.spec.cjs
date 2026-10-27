// Real-browser journeys for the simplified Add Camera flow (#173): Quick Add (address and login only, the NVR's checks shown step by step,
// diagnostics on request) and Bulk Add (many cameras, a few at a time, each with its own result).
// The fixture camera answers ONVIF at 127.0.0.1:8766 and the simulated RTSP camera is the stream, so the video step is "Working" in CI and
// "Needs attention" without it; these journeys hold either way, because a camera that needs attention can still be added.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const SECRET = "Fixture-Secret-91";

async function openAdd(page, mode) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Add Cameras" }).click();
  await page.getByRole("tab", { name: mode }).click();
}

async function cameras(page) {
  return (await (await page.request.get("/api/cameras")).json()).cameras;
}

test("Quick Add checks a camera from its address and login, shows each step and the diagnostics, and adds it with recording off", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await expect(form.getByRole("button", { name: "Check camera" })).toBeDisabled();
  await form.getByLabel("Camera address").fill("127.0.0.1:8766");
  await form.getByLabel("Camera username").fill("fixture-user");
  await form.getByLabel("Camera password").fill(SECRET);
  await form.getByRole("button", { name: "Check camera" }).click();

  const steps = form.getByRole("list", { name: "Steps" });
  await expect(steps.getByRole("listitem")).toHaveCount(7, { timeout: 60_000 });
  await expect(steps.getByRole("listitem").filter({ hasText: "Connecting to camera" })).toContainText("Working");
  await expect(steps.getByRole("listitem").filter({ hasText: "Identifying device" })).toContainText("Fixture Browser Camera");
  await expect(steps.getByRole("listitem").filter({ hasText: "Discovering streams" })).toContainText("Working");
  await expect(form.getByRole("table", { name: "What works" })).toContainText("Camera found");
  await expect(form.getByLabel("Camera name")).toHaveValue("Fixture Browser Camera 127.0.0.1");

  // Diagnostics are there on request, list the requests made and never the password.
  await form.getByText("Diagnostics", { exact: true }).click();
  await expect(form.getByRole("table", { name: "Requests made" })).toContainText("GetDeviceInformation");
  expect(await page.locator("body").innerText()).not.toContain(SECRET);

  await form.getByRole("button", { name: /^Add camera/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "was added. Recording stays off until you turn it on." })).toBeVisible({ timeout: 60_000 });
  const added = (await cameras(page)).find(camera => camera.name === "Fixture Browser Camera 127.0.0.1");
  expect([added.recording_enabled, added.recording_mode, added.camera_ip]).toEqual([false, "motion", "127.0.0.1"]);
  const health = await (await page.request.get(`/api/cameras/${added.id}/capabilities`)).json();
  expect(health.capabilities.discovery.state).toBe("ok");
  expect(JSON.stringify(health)).not.toContain(SECRET);
  expect(await page.locator("body").innerText()).not.toContain(SECRET);
  await expect(form.getByLabel("Camera password")).toHaveValue("");
});

test("Quick Add asks for a new check when the address or login changes after it, and reports a camera that cannot be reached", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await form.getByLabel("Camera address").fill("127.0.0.1:8766");
  await form.getByRole("button", { name: "Check camera" }).click();
  await expect(form.getByRole("list", { name: "Steps" }).getByRole("listitem")).toHaveCount(7, { timeout: 60_000 });
  await form.getByLabel("Camera address").fill("127.0.0.1:8767");
  await expect(form.getByRole("alert")).toContainText("changed since the check");
  await expect(form.getByRole("button", { name: /^Add camera/ })).toBeDisabled();

  // Nothing listens there: the first step says so, the rest are skipped, and the camera can still be added with recording off.
  await form.getByRole("button", { name: "Check again" }).click();
  const steps = form.getByRole("list", { name: "Steps" });
  await expect(steps.getByRole("listitem").filter({ hasText: "Connecting to camera" })).toContainText("Needs attention", { timeout: 60_000 });
  await expect(steps.getByRole("listitem").filter({ hasText: "Authenticating" })).toContainText("Skipped");
  await expect(form.getByRole("button", { name: "Add camera anyway" })).toBeEnabled();
});

test("Bulk Add checks and adds many cameras a few at a time, with a result for each and invalid lines refused", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = page.getByRole("region", { name: "Bulk add" });
  await panel.getByRole("textbox").fill([
    "# address,username,password,name",
    "127.0.0.1:8766,,,Bulk Dock",
    "127.0.0.1:8766,,,Bulk Yard",
    "127.0.0.1:8766,,,Bulk Gate",
    "bad host name,user,",
    "127.0.0.1:8766,user,,",
  ].join("\n"));
  await expect(panel.getByRole("button", { name: "Check and add 3 cameras" })).toBeEnabled();

  let active = 0;
  let busiest = 0;
  await page.route("**/api/cameras/inspect", async route => {
    active += 1;
    busiest = Math.max(busiest, active);
    try { await route.continue(); } finally { active -= 1; }
  });
  await panel.getByRole("button", { name: "Check and add 3 cameras" }).click();
  const table = panel.getByRole("table", { name: "Bulk add results" });
  await expect(panel.getByRole("status")).toContainText("3 added of 5", { timeout: 120_000 });
  await expect(panel.getByRole("status")).toContainText("2 invalid");
  for (const name of ["Bulk Dock", "Bulk Yard", "Bulk Gate"]) {
    expect((await cameras(page)).some(camera => camera.name === name)).toBe(true);
  }
  await expect(table.locator('tr[data-status="invalid"]')).toHaveCount(2);
  await expect(table.locator('tr[data-status="added"], tr[data-status="attention"]')).toHaveCount(3);
  expect(busiest).toBeLessThanOrEqual(3);
  for (const camera of (await cameras(page)).filter(item => item.name.startsWith("Bulk "))) expect(camera.recording_enabled).toBe(false);
});

test("Bulk Add copes with hundreds of cameras without opening more than three checks at once, and can be stopped", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = page.getByRole("region", { name: "Bulk add" });
  const total = 240;
  await panel.getByRole("textbox").fill(Array.from({ length: total }, (_, index) => `10.9.${Math.floor(index / 250)}.${index % 250 + 1},u,p,Scale ${index + 1}`).join("\n"));
  let active = 0;
  let busiest = 0;
  let inspected = 0;
  const report = { ok: true, device: { manufacturer: "Fixture", model: "Scale" }, brand: "other", device_service_url: "http://10.9.0.1/onvif/device_service", profiles: [],
    selection: { record_token: "main", live_token: null, reason: "", warnings: [] }, steps: [], capabilities: {}, diagnostics: [] };
  await page.route("**/api/cameras/inspect", async route => {
    active += 1;
    busiest = Math.max(busiest, active);
    inspected += 1;
    await new Promise(resolve => setTimeout(resolve, 15));
    active -= 1;
    await route.fulfill({ json: report });
  });
  await page.route("**/api/cameras", async route => {
    if (route.request().method() !== "POST") return route.continue();
    const body = route.request().postDataJSON();
    await route.fulfill({ status: 201, json: { id: `scale-${body.name}`, name: body.name } });
  });
  await panel.getByRole("button", { name: `Check and add ${total} cameras` }).click();
  await expect(panel.getByRole("status")).toContainText(`${total} added of ${total}`, { timeout: 120_000 });
  expect(busiest).toBeLessThanOrEqual(3);
  expect(inspected).toBe(total);
  await expect(panel.getByRole("table", { name: "Bulk add results" }).locator('tr[data-status="added"]')).toHaveCount(total);

  // Stop leaves the rest marked as not run, and offers to retry them.
  await panel.getByRole("textbox").fill(Array.from({ length: 60 }, (_, index) => `10.8.0.${index + 1},u,p,Stopped ${index + 1}`).join("\n"));
  await panel.getByRole("button", { name: "Check and add 60 cameras" }).click();
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(panel.getByRole("button", { name: /^Retry \d+ not added/ })).toBeVisible({ timeout: 30_000 });
  expect(await panel.getByRole("table", { name: "Bulk add results" }).locator('tr[data-status="skipped"]').count()).toBeGreaterThan(0);
});
