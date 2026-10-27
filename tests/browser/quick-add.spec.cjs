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

// The password lives in the form only: not in the address, in any browser storage, or in what the page shows.
async function expectNoSecretKept(page) {
  expect(page.url()).not.toContain(SECRET);
  const kept = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie]));
  expect(kept).not.toContain(SECRET);
}

async function cameras(page) {
  return (await (await page.request.get("/api/cameras")).json()).cameras;
}

test("Quick Add checks a camera from its address and login, shows each step and the diagnostics, and adds it with recording off", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await expect(form.getByRole("button", { name: "Check only" })).toBeDisabled();
  await form.getByLabel("Camera address").fill("127.0.0.1:8766");
  await form.getByLabel("Camera username").fill("fixture-user");
  await form.getByLabel("Camera password").fill(SECRET);
  await form.getByRole("button", { name: "Check only" }).click();

  const steps = form.getByRole("list", { name: "Steps" });
  await expect(steps.getByRole("listitem")).toHaveCount(7, { timeout: 60_000 });
  await expect(steps.getByRole("listitem").filter({ hasText: "Connecting to camera" })).toContainText("Working");
  await expect(steps.getByRole("listitem").filter({ hasText: "Identifying device" })).toContainText("Fixture Browser Camera");
  await expect(steps.getByRole("listitem").filter({ hasText: "Discovering streams" })).toContainText("Working");
  await expect(form.getByRole("table", { name: "What works" })).toContainText("Camera found");
  await expect(form.getByLabel("Camera name")).toHaveValue("Fixture Browser Camera 127.0.0.1:8766");

  // Diagnostics are there on request, list the requests made and never the password.
  await form.getByText("Diagnostics", { exact: true }).click();
  const [download] = await Promise.all([page.waitForEvent("download"), form.getByRole("button", { name: "Download diagnostics" }).click()]);
  expect(download.suggestedFilename()).toBe("camera-diagnostics.json");
  const exported = await require("node:fs/promises").readFile(await download.path(), "utf8");
  expect(exported).not.toContain(SECRET);
  expect(exported).toContain("GetDeviceInformation");
  await expect(form.getByRole("table", { name: "Requests made" })).toContainText("GetDeviceInformation");
  expect(await page.locator("body").innerText()).not.toContain(SECRET);
  await expectNoSecretKept(page);

  await form.getByRole("button", { name: /^Add this camera|^Add camera anyway/ }).click();
  await expect(page.getByRole("status").filter({ hasText: "was added. Recording stays off until you turn it on." })).toBeVisible({ timeout: 60_000 });
  const added = (await cameras(page)).find(camera => camera.name === "Fixture Browser Camera 127.0.0.1:8766");
  expect([added.recording_enabled, added.recording_mode, added.camera_ip]).toEqual([false, "motion", "127.0.0.1"]);
  const health = await (await page.request.get(`/api/cameras/${added.id}/capabilities`)).json();
  expect(health.capabilities.discovery.state).toBe("ok");
  expect(JSON.stringify(health)).not.toContain(SECRET);
  expect(await page.locator("body").innerText()).not.toContain(SECRET);
  await expectNoSecretKept(page);
  await expect(form.getByLabel("Camera password")).toHaveValue("");
});

test("Quick Add adds a camera in one step when it checks out, or shows what is wrong and waits", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await form.getByLabel("Camera address").fill("127.0.0.1:8766");
  await form.getByRole("button", { name: "Add camera", exact: true }).click();
  // Everything works (CI, with the simulated RTSP camera): added at once. Otherwise the result is shown and the camera waits for a decision.
  const added = page.getByRole("status").filter({ hasText: "was added. Recording stays off until you turn it on." });
  const waiting = form.getByRole("button", { name: "Add camera anyway" });
  await expect(added.or(waiting)).toBeVisible({ timeout: 60_000 });
  if (await waiting.isVisible()) {
    await expect(form.getByRole("table", { name: "What works" })).toBeVisible();
    expect((await cameras(page)).some(camera => camera.name === "Fixture Browser Camera 127.0.0.1:8766")).toBe(false);
    await waiting.click();
    await expect(added).toContainText("needs attention", { timeout: 60_000 });
  }
  expect((await cameras(page)).find(camera => camera.name === "Fixture Browser Camera 127.0.0.1:8766").recording_enabled).toBe(false);
});

test("Quick Add asks for a new check when the address or login changes after it, and reports a camera that cannot be reached", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await form.getByLabel("Camera address").fill("127.0.0.1:8766");
  await form.getByRole("button", { name: "Check only" }).click();
  await expect(form.getByRole("list", { name: "Steps" }).getByRole("listitem")).toHaveCount(7, { timeout: 60_000 });
  await form.getByLabel("Camera address").fill("127.0.0.1:8767");
  await expect(form.getByRole("alert")).toContainText("changed since the check");
  await expect(form.getByRole("button", { name: /^Add this camera|^Add camera anyway/ })).toBeDisabled();

  // Nothing listens there: the first step says so, the rest are skipped, and the camera can still be added with recording off.
  await form.getByRole("button", { name: "Check only" }).click();
  const steps = form.getByRole("list", { name: "Steps" });
  await expect(steps.getByRole("listitem").filter({ hasText: "Connecting to camera" })).toContainText("Needs attention", { timeout: 60_000 });
  await expect(steps.getByRole("listitem").filter({ hasText: "Authenticating" })).toContainText("Skipped");
  await expect(form.getByRole("button", { name: "Add camera anyway" })).toBeEnabled();
});

test("Bulk Add checks and adds many cameras a few at a time, with a result for each and invalid lines refused", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = page.getByRole("region", { name: "Bulk add", exact: true });
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
    try { await route.fulfill({ response: await route.fetch() }); } finally { active -= 1; }
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
  const panel = page.getByRole("region", { name: "Bulk add", exact: true });
  const total = 240;
  await panel.getByRole("textbox").fill(Array.from({ length: total }, (_, index) => `10.9.${Math.floor(index / 250)}.${index % 250 + 1},u,p,Scale ${index + 1}`).join("\n"));
  let active = 0;
  let busiest = 0;
  let inspected = 0;
  let hold = null;
  const report = { ok: true, device: { manufacturer: "Fixture", model: "Scale" }, brand: "other", device_service_url: "http://10.9.0.1/onvif/device_service", profiles: [],
    selection: { record_token: "main", live_token: null, reason: "", warnings: [] }, steps: [], capabilities: {}, diagnostics: [] };
  await page.route("**/api/cameras/inspect", async route => {
    active += 1;
    busiest = Math.max(busiest, active);
    inspected += 1;
    await new Promise(resolve => setTimeout(resolve, 15));
    if (hold) await hold;
    await route.fulfill({ json: report });
    active -= 1;
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
  let release;
  hold = new Promise(resolve => { release = resolve; });
  await panel.getByRole("button", { name: "Check and add 60 cameras" }).click();
  await expect.poll(() => active).toBe(3);  // Three are being checked, held; the other 57 are queued
  await panel.getByRole("button", { name: "Stop" }).click();
  release();
  await expect(panel.getByRole("button", { name: "Retry 57 not added" })).toBeVisible({ timeout: 30_000 });
  await expect(panel.getByRole("table", { name: "Bulk add results" }).locator('tr[data-status="skipped"]')).toHaveCount(57);
  await expect(panel.getByRole("table", { name: "Bulk add results" }).locator('tr[data-status="added"]')).toHaveCount(3);
});

const OK_REPORT = { ok: true, device: { manufacturer: "Fixture", model: "Scale" }, brand: "other", device_service_url: "http://10.9.0.1/onvif/device_service", profiles: [],
  selection: { record_token: "main", live_token: null, reason: "", warnings: [] }, steps: [], capabilities: {}, diagnostics: [] };

test("Bulk Add reads quoted and awkward passwords exactly as written, a header with a byte-order mark, host:port and bracketed IPv6", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = page.getByRole("region", { name: "Bulk add", exact: true });
  const asked = [];
  await page.route("**/api/cameras/inspect", route => { asked.push(route.request().postDataJSON()); return route.fulfill({ json: OK_REPORT }); });
  await page.route("**/api/cameras", route => (route.request().method() === "POST" ? route.fulfill({ status: 201, json: { id: "x", name: route.request().postDataJSON().name } }) : route.continue()));
  await panel.getByRole("textbox").fill([
    "﻿address,username,password,name",
    '10.7.0.1,user,"pa,ss",Quoted',
    '10.7.0.2,user,pa"ss,Bare',
    '10.7.0.3,user," padded ",Spaces',
    "[::1]:8080,user,pw,Six",
    "cam.example.test:8000,user,pw,Named",
  ].join("\n"));
  await panel.getByRole("button", { name: "Check and add 5 cameras" }).click();
  await expect(panel.getByRole("status")).toContainText("5 added of 5", { timeout: 30_000 });
  const by = host => asked.find(body => body.host === host);
  expect(by("10.7.0.1").password).toBe("pa,ss");
  expect(by("10.7.0.2").password).toBe('pa"ss');
  expect(by("10.7.0.3").password).toBe(" padded ");
  expect(by("::1").device_service_url).toBe("http://[::1]:8080/onvif/device_service");
  expect(by("cam.example.test").device_service_url).toBe("http://cam.example.test:8000/onvif/device_service");
});

test("leaving Bulk Add stops the run, so nothing keeps adding cameras out of sight", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = page.getByRole("region", { name: "Bulk add", exact: true });
  let started = 0;
  let release;
  const hold = new Promise(resolve => { release = resolve; });
  await page.route("**/api/cameras/inspect", async route => { started += 1; await hold; await route.fulfill({ json: OK_REPORT }); });
  await page.route("**/api/cameras", route => (route.request().method() === "POST" ? route.fulfill({ status: 201, json: { id: "x", name: "x" } }) : route.continue()));
  await panel.getByRole("textbox").fill(Array.from({ length: 30 }, (_, index) => `10.6.0.${index + 1},u,p,Leaving ${index + 1}`).join("\n"));
  await panel.getByRole("button", { name: "Check and add 30 cameras" }).click();
  await expect.poll(() => started).toBe(3);
  await page.getByRole("tab", { name: "Manual Add" }).click();
  release();
  await page.waitForTimeout(500);
  expect(started).toBe(3);  // The three that were under way finished; none of the other 27 was started
});
