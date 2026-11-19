// Real-browser journeys for Add Cameras (#100): Discover, Manual Add and CSV Import.
// Fixtures: "Unadded Fixture Camera" (ONVIF at 127.0.0.1:8766), "Linkable Fixture Camera",
// and "Already Added Fixture" linked to camera 1. A multicast scan finds DeterministicFixtureCamera.
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const STREAM = "rtsp://127.0.0.1:8554/nvr-test";

async function openAdd(page, mode = "Discover") {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Add Cameras" }).click();
  await page.getByRole("tab", { name: mode }).click();
}

async function cameras(page) {
  return (await (await page.request.get("/api/cameras")).json()).cameras;
}

const results = page => page.getByRole("table", { name: "Discovered cameras" });

test("discovery lists unadded devices, links an existing camera and adds a tested device with recording off", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signInNext(page);
  await openAdd(page);
  await expect(results(page)).toContainText("Unadded Fixture Camera");
  await expect(results(page)).not.toContainText("Already Added Fixture");
  await expect(page.getByText("2 not-added devices shown")).toBeVisible();

  await page.getByLabel("Show added devices").check();
  await results(page).getByRole("button", { name: "Already Added Fixture" }).click();
  const setup = page.getByRole("region", { name: "Device setup" });
  await expect(setup).toContainText("Already added as Camera 001");
  await expect(setup.getByRole("button", { name: "Test connection and add camera" })).toBeDisabled();
  await setup.getByRole("button", { name: "Open existing camera" }).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 001" })).toBeVisible();

  await openAdd(page);
  await results(page).getByRole("button", { name: "Linkable Fixture Camera" }).click();
  await setup.getByLabel("Existing camera").selectOption({ label: "Camera 001" });
  await setup.getByRole("button", { name: "Link to existing camera" }).click();
  await expect(setup.getByRole("status")).toHaveText("Linked. The camera ID and recording history were preserved.");
  const linked = await cameras(page);
  expect(linked.map(camera => camera.id)).toEqual([fixtures.primary_camera_id]);
  await expect(page.getByText("1 not-added device shown")).toBeVisible();

  await results(page).getByRole("button", { name: "Unadded Fixture Camera" }).click();
  await setup.getByLabel("Camera name").fill("Discovered Fixture Camera");
  await expect(setup.getByLabel("IP address")).toHaveValue("127.0.0.1");
  await expect(setup.getByLabel("ONVIF port")).toHaveValue("8766");
  await setup.getByRole("button", { name: "Load stream profiles" }).click();
  await expect(setup.getByLabel("Main (recording) profile")).toHaveValue("main");
  await setup.getByRole("button", { name: "Test connection and add camera" }).click();
  await expect(setup.getByRole("status")).toHaveText("Discovered Fixture Camera was added. Recording stays off until you turn it on.");
  const added = (await cameras(page)).find(camera => camera.name === "Discovered Fixture Camera");
  expect([added.recording_enabled, added.recording_mode]).toEqual([false, "motion"]);
  await page.getByLabel("Show added devices").uncheck();
  await expect(page.getByText("0 not-added devices shown")).toBeVisible();
});

test("scans show progress, recover after a failure and explain a refused range", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page);
  await page.getByLabel("Duration (s)").fill("0.5");
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const failOnce = async route => { await held; await route.abort("failed"); };
  await page.route("**/api/discovery/scan", failOnce);
  await page.getByRole("button", { name: "Scan camera network" }).click();
  await expect(page.getByText("Scanning the camera network…")).toBeVisible();
  await expect(page.getByRole("button", { name: "Scan camera network" })).toBeDisabled();
  release();
  await expect(page.getByRole("alert").filter({ hasText: "Scan failed" })).toBeVisible();
  await page.unroute("**/api/discovery/scan", failOnce);
  await page.getByRole("button", { name: "Retry scan" }).click();
  await expect(page.getByText(/Scan complete: 1 device\(s\) found/)).toBeVisible();
  await expect(results(page)).toContainText("DeterministicFixtureCamera");

  await page.getByLabel("Private range").fill("0.0.0.0/32");
  await page.getByRole("button", { name: "Scan range" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Scan failed" })).toContainText("private IPv4");
  await page.getByRole("button", { name: "Retry scan" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Scan failed" })).toContainText("private IPv4");
});

test("the server rejects a concurrent duplicate add, and a failed add keeps the draft without echoing secrets", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signInNext(page);
  const body = { name: "Race", camera_ip: "127.0.0.1", device_service_url: "http://127.0.0.1:8766/onvif/device_service", onvif_profile_token: "main", recording_mode: "motion", recording_enabled: false };
  const [first, second] = await Promise.all([
    page.request.post(`/api/discovery/candidates/${fixtures.discovery_unlinked_id}/add`, { data: body }),
    page.request.post(`/api/discovery/candidates/${fixtures.discovery_unlinked_id}/add`, { data: { ...body, name: "Race 2" } }),
  ]);
  expect([first.status(), second.status()].sort()).toEqual([201, 409]);
  expect((await cameras(page)).filter(camera => camera.name.startsWith("Race"))).toHaveLength(1);

  const secret = "Fixture-Secret-91";
  const bodies = [];
  page.on("response", async response => response.url().includes("/api/") && bodies.push(await response.text().catch(() => "")));
  await openAdd(page);
  await results(page).getByRole("button", { name: "Linkable Fixture Camera" }).click();
  const setup = page.getByRole("region", { name: "Device setup" });
  await setup.getByLabel("Camera name").fill("Unreachable device");
  await setup.getByLabel("IP address").fill("127.0.0.1");
  await setup.getByLabel("ONVIF port").fill("9");
  await setup.getByLabel("Username").fill("admin");
  await setup.getByLabel("Password").fill(secret);
  await setup.getByRole("button", { name: "Test connection and add camera" }).click();
  await expect(setup.getByRole("alert")).toContainText("Not added");
  await expect(setup.getByLabel("Camera name")).toHaveValue("Unreachable device");
  await expect(setup.getByLabel("Password")).toHaveValue(secret);
  expect(bodies.filter(text => text.includes(secret))).toEqual([]);
  expect((await cameras(page)).some(camera => camera.name === "Unreachable device")).toBe(false);
});

test("manual add tests the stream, adds with recording off, and keeps the draft on failure", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Manual Add");
  const form = page.getByRole("form", { name: "Add a camera manually" });
  await form.getByLabel("Camera name").fill("Gate");
  await form.getByLabel("Direct RTSP URL").check();
  await form.getByLabel("RTSP URL", { exact: true }).fill("rtsp://127.0.0.1:9/missing");
  await form.getByRole("button", { name: "Add camera" }).click();
  await expect(form.getByRole("alert")).toContainText("Not added");
  await expect(form.getByLabel("Camera name")).toHaveValue("Gate");
  await expect(form.getByLabel("RTSP URL", { exact: true })).toHaveValue("rtsp://127.0.0.1:9/missing");

  await form.getByLabel("RTSP URL", { exact: true }).fill(STREAM);
  await form.getByRole("button", { name: "Test stream" }).click();
  await expect(form.getByRole("status")).toContainText("Stream opened: H264", { timeout: 20_000 });
  await form.getByRole("button", { name: "Add camera" }).click();
  await expect(form.getByRole("status")).toContainText("Gate was added. Recording stays off until you turn it on.", { timeout: 20_000 });
  const gate = (await cameras(page)).find(camera => camera.name === "Gate");
  expect([gate.recording_enabled, gate.recording_mode]).toEqual([false, "motion"]);
  await form.getByRole("button", { name: "Open Gate" }).click();
  await expect(page.getByRole("region", { name: "Settings for Gate" })).toBeVisible();
});

test("CSV import previews, then reports every row and never adds an untested stream", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openAdd(page, "CSV Import");
  const csv = [
    "name,stream_url,recording_mode,group_name",
    `Dock,${STREAM},continuous,Yard`,
    `Camera 001,${STREAM},motion,`,
    "Broken,not-a-url,motion,",
    "Offline,rtsp://127.0.0.1:9/missing,motion,",
  ].join("\n");
  await page.getByRole("textbox", { name: /^CSV/ }).fill(csv);
  await page.getByRole("button", { name: "Preview import" }).click();
  const preview = page.getByRole("table", { name: "Import preview" });
  await expect(preview.locator("tbody tr")).toHaveCount(4);
  await expect(preview.locator('tr[data-status="accepted"]')).toHaveCount(2);
  await expect(preview.locator('tr[data-status="duplicate"]')).toContainText("Camera 001");
  await expect(preview.locator('tr[data-status="invalid"]')).toContainText("Broken");
  await preview.getByRole("button", { name: "4" }).click();
  await expect(page.getByRole("region", { name: "Row details" })).toContainText("Row 4: Broken");

  // Editing after the preview requires a fresh preview.
  await page.getByRole("textbox", { name: /^CSV/ }).fill(`${csv}\n`);
  await expect(page.getByRole("button", { name: "Import 2 cameras" })).toBeDisabled();
  await page.getByRole("textbox", { name: /^CSV/ }).fill(csv);
  await page.getByRole("button", { name: "Import 2 cameras" }).click();
  const outcome = page.getByRole("table", { name: "Import results" });
  await expect(outcome.locator('tr[data-status="created"]')).toContainText("Dock", { timeout: 30_000 });
  await expect(outcome.locator('tr[data-status="failed"]')).toContainText("Offline");
  await expect(page.getByText("1 added, 3 not added.")).toBeVisible();
  const all = await cameras(page);
  expect(all.map(camera => camera.name).sort()).toEqual(["Camera 001", "Dock"]);
  expect(all.every(camera => camera.recording_enabled === false)).toBe(true);
});

test("a late candidate list cannot replace newer scan results", async ({ page }) => {
  seedFixtures(0);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let captured;
  const firstCaptured = new Promise(resolve => { captured = resolve; });
  let finished;
  const firstFinished = new Promise(resolve => { finished = resolve; });
  let calls = 0;
  const holdFirst = async route => {
    calls += 1;
    if (calls !== 1) return route.continue();
    // The answer is taken now, before the scan, and delivered after it.
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
    finished();
  };
  await page.route("**/api/discovery/candidates", holdFirst);
  await signInNext(page);
  await openAdd(page);
  await firstCaptured;
  await page.getByLabel("Duration (s)").fill("0.5");
  await page.getByRole("button", { name: "Scan camera network" }).click();
  await expect(page.getByText(/Scan complete: 1 device\(s\) found/)).toBeVisible();
  await expect(results(page)).toContainText("DeterministicFixtureCamera");
  release();
  await firstFinished;
  await page.waitForTimeout(300);
  await expect(results(page)).toContainText("DeterministicFixtureCamera");
  await page.unroute("**/api/discovery/candidates", holdFirst);
});

test("a failed CSV preview explains itself and a preview of older text cannot be imported", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "CSV Import");
  const csv = page.getByRole("textbox", { name: /^CSV/ });
  const failOnce = route => route.abort("failed");
  await page.route("**/api/cameras/import/preview", failOnce);
  await csv.fill(`name,stream_url,recording_mode\nPreview Failure,${STREAM},disabled`);
  await page.getByRole("button", { name: "Preview import" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /preview|reach/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Import \d+ camera/ })).toBeDisabled();
  await page.unroute("**/api/cameras/import/preview", failOnce);

  // The text changes while a preview is in flight: its answer describes the old text.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const holdPreview = async route => {
    const response = await route.fetch();
    await held;
    await route.fulfill({ response });
  };
  await page.route("**/api/cameras/import/preview", holdPreview);
  await csv.fill(`name,stream_url,recording_mode\nOlder Text Fixture,${STREAM},disabled`);
  await page.getByRole("button", { name: "Preview import" }).click();
  await expect(page.getByRole("button", { name: "Checking…" })).toBeDisabled();
  await csv.fill(`name,stream_url,recording_mode\nNewer Text Fixture,${STREAM},disabled`);
  release();
  await expect(page.getByRole("table", { name: "Import preview" })).toContainText("Older Text Fixture");
  await expect(page.getByText("The CSV changed since the preview. Preview it again before importing.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Import 1 camera" })).toBeDisabled();
  await page.unroute("**/api/cameras/import/preview", holdPreview);

  await page.getByRole("button", { name: "Preview import" }).click();
  await expect(page.getByRole("table", { name: "Import preview" })).toContainText("Newer Text Fixture");
  await expect(page.getByRole("button", { name: "Import 1 camera" })).toBeEnabled();
});

test("a discovered device that is not wanted can be forgotten (#381)", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openAdd(page);
  await expect(results(page)).toContainText("Unadded Fixture Camera");
  await expect(page.getByText("2 not-added devices shown")).toBeVisible();
  await results(page).getByRole("button", { name: "Unadded Fixture Camera" }).click();
  const setup = page.getByRole("region", { name: "Device setup" });
  await setup.getByRole("button", { name: "Forget this device" }).click();
  await expect(results(page)).not.toContainText("Unadded Fixture Camera");
  await expect(page.getByText("1 not-added device shown")).toBeVisible();
  await expect(setup).toContainText("Select a discovered device to set it up.");
});

test("several discovered devices are added with one shared login, with the progress in Bulk Add (#173)", async ({ page }) => {
  // Both unadded fixtures answer at the same fixture camera, so both are named alike: one is added and the other is reported as not added,
  // whichever is checked first. Without the simulated RTSP camera the one added needs attention; the journey holds either way.
  const secret = "Shared-Fixture-Secret-37";
  seedFixtures(1);
  await signInNext(page);
  await openAdd(page);
  await expect(page.getByText("2 not-added devices shown")).toBeVisible();
  const setup = page.getByRole("region", { name: "Device setup" });
  await results(page).getByLabel("Choose Unadded Fixture Camera").check();
  await expect(setup.getByRole("heading", { name: "Add 1 device" })).toBeVisible();
  await results(page).getByLabel("Choose every device shown that is not added").check();
  const form = setup.getByRole("form", { name: "Add the chosen devices" });
  await expect(form.getByRole("heading", { name: "Add 2 devices" })).toBeVisible();
  await expect(form).toContainText("Linkable Fixture Camera");
  await form.getByLabel("Username").fill("fixture-user");
  await expect(form.getByRole("alert")).toHaveText("Enter both a username and a password, or leave both blank.");
  await expect(form.getByRole("button", { name: "Check and add 2 devices" })).toBeDisabled();
  await form.getByLabel("Password").fill(secret);
  await form.getByRole("button", { name: "Check and add 2 devices" }).click();

  await expect(page.getByRole("tab", { name: "Bulk Add" })).toHaveAttribute("aria-selected", "true");
  const table = page.getByRole("table", { name: "Bulk add results" });
  await expect(table.getByRole("row")).toHaveCount(3);
  await expect(table).toContainText("127.0.0.1:8766");
  await expect(page.getByRole("status").filter({ hasText: /^1 added of 2.*1 not added\.$/ })).toBeVisible({ timeout: 90_000 });
  await expect(table).toContainText("already exists");
  expect(await page.locator("body").innerText()).not.toContain(secret);
  const kept = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), document.cookie]));
  expect(kept).not.toContain(secret);

  const added = (await cameras(page)).filter(camera => camera.camera_ip === "127.0.0.1");
  expect(added.length).toBe(1);
  expect([added[0].recording_enabled, added[0].recording_mode]).toEqual([false, "motion"]);
  await page.getByRole("tab", { name: "Discover" }).click();
  await expect(page.getByText("1 not-added device shown")).toBeVisible();  // The one added is linked to its discovery record
});
