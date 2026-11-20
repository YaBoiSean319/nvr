// Real-browser journeys for the simplified Add Camera flow (#173): Quick Add (address and login only, the NVR's checks shown step by step,
// diagnostics on request) and Bulk Add (many cameras, added by the NVR in the background a few at a time, each with its own result).
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

const AT = "2026-11-17T12:00:00.000Z";
const item = (row, host, status, detail = "", name = "") => ({ row, host, name, status, detail, camera_id: status === "added" || status === "attention" ? `camera-${row}` : null });
function bulkJob(state, items, id = "job-1") {
  const counts = {};
  for (const entry of items) counts[entry.status] = (counts[entry.status] ?? 0) + 1;
  return { id, state, created_at: AT, finished_at: state === "running" ? null : AT, add_anyway: true, recording: false, counts, items };
}
const bulkPanel = page => page.getByRole("region", { name: "Bulk add", exact: true });

test("Bulk Add adds many cameras in the background, with a result for each, invalid lines refused, and the job is still there when the page is left and opened again", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = bulkPanel(page);
  await panel.getByRole("textbox").fill([
    "# address,username,password,name",
    "127.0.0.1:8766,,,Bulk Dock",
    "127.0.0.1:8766,,,Bulk Yard",
    "127.0.0.1:8766,,,Bulk Gate",
    "bad host name,user,",
    "127.0.0.1:8766,user,,",
  ].join("\n"));
  await expect(panel.getByRole("button", { name: "Check and add 3 cameras" })).toBeEnabled();
  await panel.getByRole("button", { name: "Check and add 3 cameras" }).click();
  const table = panel.getByRole("table", { name: "Bulk add results" });
  await expect(panel.getByRole("status")).toContainText("3 added of 5", { timeout: 120_000 });
  await expect(panel.getByRole("status")).toContainText("2 invalid");
  for (const name of ["Bulk Dock", "Bulk Yard", "Bulk Gate"]) {
    expect((await cameras(page)).some(camera => camera.name === name)).toBe(true);
  }
  await expect(table.locator('tr[data-status="invalid"]')).toHaveCount(2);
  await expect(table.locator('tr[data-status="added"], tr[data-status="attention"]')).toHaveCount(3);
  for (const camera of (await cameras(page)).filter(item => item.name.startsWith("Bulk "))) expect(camera.recording_enabled).toBe(false);

  // The NVR did the work, not this page: leave and come back, and what happened is shown from the server.
  await page.getByRole("tab", { name: "Manual Add" }).click();
  await page.getByRole("tab", { name: "Bulk Add" }).click();
  await expect(bulkPanel(page).getByRole("status")).toContainText("3 added of 3");
  await expect(bulkPanel(page).getByRole("table", { name: "Bulk add results" }).locator('tr[data-status="added"], tr[data-status="attention"]')).toHaveCount(3);
  await expectNoSecretKept(page);
});

test("a Bulk Add that is still running when the page is opened is picked up, shows its progress and can be stopped", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  let cancelled = 0;
  const running = bulkJob("running", [item(1, "10.5.0.1", "added", "Fixture 10.5.0.1"), item(2, "10.5.0.2", "checking"), item(3, "10.5.0.3", "queued")], "job-9");
  await page.route("**/api/cameras/bulk-jobs/latest", route => route.fulfill({ json: { job: running } }));
  await page.route("**/api/cameras/bulk-jobs/job-9", route => route.fulfill({ json: running }));
  await page.route("**/api/cameras/bulk-jobs/job-9/cancel", route => {
    cancelled += 1;
    return route.fulfill({ json: bulkJob("cancelled", [item(1, "10.5.0.1", "added", "Fixture 10.5.0.1"), item(2, "10.5.0.2", "checking"), item(3, "10.5.0.3", "skipped", "Stopped before this camera")], "job-9") });
  });
  await openAdd(page, "Bulk Add");
  const panel = bulkPanel(page);
  const table = panel.getByRole("table", { name: "Bulk add results" });
  await expect(panel.getByRole("status")).toContainText("1 added of 3");
  await expect(panel.getByRole("status")).toContainText("1 in progress");
  await expect(table.locator('tr[data-status="queued"]')).toHaveCount(1);
  await expect(panel.getByRole("textbox")).toBeDisabled();
  await panel.getByRole("button", { name: "Stop" }).click();
  await expect(table.locator('tr[data-status="skipped"]')).toHaveCount(1);
  await expect(panel.getByRole("button", { name: "Stop" })).toHaveCount(0);
  expect(cancelled).toBe(1);
  await expect(panel.getByRole("button", { name: /^Retry/ })).toHaveCount(0);  // A job picked up again holds no passwords to retry with
});

test("Bulk Add sends passwords exactly as written (quotes, commas, a header with a byte-order mark, host:port, bracketed IPv6) and retries only what was not added", async ({ page }) => {
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Bulk Add");
  const panel = bulkPanel(page);
  const sent = [];
  await page.route("**/api/cameras/bulk-jobs", route => {
    if (route.request().method() !== "POST") return route.continue();
    sent.push(route.request().postDataJSON());
    const rows = sent.length === 1
      ? [item(1, "10.7.0.1", "added"), item(2, "10.7.0.2", "failed", "Camera authentication rejected"), item(3, "10.7.0.3", "added"), item(4, "[::1]:8080", "skipped", "Stopped before this camera"), item(5, "cam.example.test:8000", "added")]
      : [item(1, "10.7.0.2", "added"), item(2, "[::1]:8080", "added")];
    return route.fulfill({ status: 201, json: bulkJob("done", rows, `job-${sent.length}`) });
  });
  await panel.getByRole("textbox").fill([
    "\uFEFFaddress,username,password,name",
    '10.7.0.1,user,"pa,ss",Quoted',
    '10.7.0.2,user,pa"ss,Bare',
    '10.7.0.3,user," padded ",Spaces',
    "[::1]:8080,user,pw,Six",
    "cam.example.test:8000,user,pw,Named",
  ].join("\n"));
  await panel.getByRole("button", { name: "Check and add 5 cameras" }).click();
  await expect(panel.getByRole("status")).toContainText("3 added of 5");
  const by = host => sent[0].cameras.find(camera => camera.host === host);
  expect(by("10.7.0.1").password).toBe("pa,ss");
  expect(by("10.7.0.2").password).toBe('pa"ss');
  expect(by("10.7.0.3").password).toBe(" padded ");
  expect(by("[::1]:8080")).toMatchObject({ username: "user", password: "pw", name: "Six" });
  expect(by("cam.example.test:8000").name).toBe("Named");
  expect(sent[0]).toMatchObject({ add_anyway: true, recording: false });

  // Retry sends the two that were not added, with their passwords, as a new job.
  await panel.getByRole("button", { name: "Retry 2 not added" }).click();
  await expect(panel.getByRole("status")).toContainText("2 added of 2");
  expect(sent).toHaveLength(2);
  expect(sent[1].cameras.map(camera => [camera.host, camera.password])).toEqual([["10.7.0.2", 'pa"ss'], ["[::1]:8080", "pw"]]);
  await expectNoSecretKept(page);
});

test("Quick Add shows each step of the check as it is decided, not all at once at the end (#173)", async ({ page }) => {
  // The check is told by the NVR one line at a time; here the browser's fetch hands those lines over when the test says, so what the page
  // shows between them can be checked. The other Quick Add journeys use the real check.
  await page.addInitScript(() => {
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (String(input).endsWith("/api/cameras/inspect/stream")) {
        const stream = new ReadableStream({ start(controller) { window.__checkLines = controller; } });
        return Promise.resolve(new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } }));
      }
      return realFetch(input, init);
    };
  });
  const send = (update) => page.evaluate(line => window.__checkLines.enqueue(new TextEncoder().encode(line + "\n")), JSON.stringify(update));
  const step = (id, label, state, detail = "") => ({ id, label, state, detail });
  const STEPS = [["connect", "Connecting to camera"], ["authenticate", "Authenticating"], ["identify", "Identifying device"], ["streams", "Discovering streams"],
    ["motion", "Configuring motion events"], ["video", "Verifying video"], ["ready", "Camera ready"]];
  seedFixtures(0);
  await signInNext(page);
  await openAdd(page, "Quick Add");
  const form = page.getByRole("form", { name: "Add a camera by address" });
  await form.getByLabel("Camera address").fill("192.0.2.20");
  await form.getByRole("button", { name: "Check only" }).click();
  await page.waitForFunction(() => Boolean(window.__checkLines));

  await send({ type: "steps", steps: STEPS.map(([id, label]) => step(id, label, "pending")) });
  const live = form.getByRole("list", { name: "Checking the camera" });
  const item = name => live.getByRole("listitem").filter({ hasText: name });
  await expect(item("Connecting to camera")).toContainText("Checking…");
  await expect(item("Authenticating")).toContainText("Waiting");

  await send({ type: "step", step: step("connect", "Connecting to camera", "ok") });
  await expect(item("Connecting to camera")).toContainText("Working");
  await expect(item("Authenticating")).toContainText("Checking…");
  await send({ type: "step", step: step("authenticate", "Authenticating", "ok", "Signed in with digest") });
  await send({ type: "step", step: step("identify", "Identifying device", "ok", "Fixture Make Model-9") });
  await expect(item("Identifying device")).toContainText("Fixture Make Model-9");
  await expect(item("Discovering streams")).toContainText("Checking…");
  await expect(form.getByRole("list", { name: "Steps" })).toHaveCount(0);  // No report yet

  const done = STEPS.map(([id, label]) => step(id, label, ["motion", "video", "ready"].includes(id) ? "failed" : "ok"));
  await send({ type: "report", report: { ok: false, device: { manufacturer: "Fixture Make", model: "Model-9" }, brand: "other", device_service_url: null, profiles: [], selection: null,
    steps: done, capabilities: { discovery: { state: "ok", message: "An ONVIF device service answered" } }, diagnostics: [] } });
  await page.evaluate(() => window.__checkLines.close());
  await expect(form.getByRole("list", { name: "Steps" }).getByRole("listitem")).toHaveCount(7);
  await expect(live).toHaveCount(0);
  await expect(form.getByLabel("Camera name")).toHaveValue("Fixture Make Model-9 192.0.2.20");
  await expect(form.getByRole("button", { name: "Add camera anyway" })).toBeVisible();
});
