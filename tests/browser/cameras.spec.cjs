// Real-browser journeys for the Cameras inventory and General & Connection editor (#95, #67).
const { test, expect, seedFixtures, signInNext, trackLiveSockets, FIXTURE_PASSWORD, median, percentile, writeAcceptanceMeasurements } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const inventory = page => page.getByRole("region", { name: "Camera inventory" });
const editor = page => page.locator(".editor");

async function openCameras(page) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await expect(inventory(page)).toBeVisible();
}

function rowButton(page, name) {
  return inventory(page).getByRole("button", { name, exact: true });
}

async function selectCamera(page, name) {
  await rowButton(page, name).click();
  await expect(page.getByRole("region", { name: `Settings for ${name}` })).toBeVisible();
}

async function expectNoHorizontalOverflow(page) {
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(page.viewportSize().width);
}

/** Count requests that would start media or probe a camera. */
function trackCameraWork(page) {
  const work = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (/\/(test|profiles|probe)$/.test(url.pathname) || /^\/api\/cameras\/[^/]+\/live/.test(url.pathname)) work.push(url.pathname);
  });
  return work;
}

test("0, 1 and 200 camera inventories render without selecting anything or starting media", async ({ page }) => {
  seedFixtures(0);
  const work = trackCameraWork(page);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await openCameras(page);
  await expect(inventory(page).getByText("No cameras have been added yet.")).toBeVisible();
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();

  seedFixtures(1);
  await page.reload();
  await expect(rowButton(page, "Camera 001")).toBeVisible();
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();
  await expect(inventory(page).getByRole("row", { name: /Camera 001/ })).toContainText("Direct URL");

  seedFixtures(200);
  await page.reload();
  await expect(inventory(page).locator("tbody tr")).toHaveCount(200);
  await expect(inventory(page).getByText("200 of 200 cameras")).toBeVisible();
  await inventory(page).getByRole("searchbox").fill("192.0.2.199");
  await expect(inventory(page).locator("tbody tr")).toHaveCount(1);
  await inventory(page).getByRole("searchbox").fill("");
  await inventory(page).getByRole("button", { name: "Name" }).click();
  await expect(inventory(page).locator("tbody tr").first()).toContainText("Camera 200");
  await inventory(page).getByLabel("Policy group").selectOption("Operations");
  await expect(inventory(page).locator("tbody tr")).toHaveCount(100);
  await page.waitForTimeout(500);
  expect(work).toEqual([]);
  expect(sockets.opened()).toBe(0);
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();
});

test("a row opens exactly one editor; bulk checkboxes never change it", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await inventory(page).getByRole("checkbox", { name: "Select Camera 003 for bulk changes" }).check();
  await inventory(page).getByRole("checkbox", { name: "Select Camera 001 for bulk changes" }).check();
  await expect(editor(page)).toHaveCount(1);
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
  await expect(inventory(page).getByText("2 selected for bulk changes")).toBeVisible();
  await inventory(page).getByRole("row", { name: /Camera 003/ }).getByRole("cell").nth(3).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 003" })).toBeVisible();
  await expect(editor(page)).toHaveCount(1);
  await expect(page.getByRole("tab", { selected: true })).toHaveText("General & Connection");
});

test("dirty navigation asks Save / Discard / Cancel and saving moves on only after success", async ({ page }) => {
  const fixtures = seedFixtures(3, { failNameSave: true });
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  const name = editor(page).getByLabel("Name", { exact: true });
  await name.fill("Loading dock");

  // Cancel keeps the target and the edit.
  await rowButton(page, "Camera 003").click();
  const dialog = page.getByRole("dialog", { name: "Unsaved changes" });
  await expect(dialog).toContainText("General & Connection for Camera 002");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(name).toHaveValue("Loading dock");
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();

  // Tabs and other workspaces are guarded too.
  await page.getByRole("tab", { name: "Recording" }).click();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await nav(page).getByRole("link", { name: "Live" }).click();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(name).toHaveValue("Loading dock");

  // Save navigates after the save succeeds.
  await rowButton(page, "Camera 003").click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 003" })).toBeVisible();
  await expect(rowButton(page, "Loading dock")).toBeVisible();

  // Discard leaves the saved value alone.
  await editor(page).getByLabel("Name", { exact: true }).fill("Scratch");
  await rowButton(page, "Loading dock").click();
  await dialog.getByRole("button", { name: "Discard" }).click();
  await expect(page.getByRole("region", { name: "Settings for Loading dock" })).toBeVisible();
  const saved = await (await page.request.get("/api/cameras")).json();
  expect(saved.cameras.find(camera => camera.id === fixtures.camera_ids[2]).name).toBe("Camera 003");

  // A failing save keeps the draft and the camera.
  await rowButton(page, "Camera 001").click();
  await editor(page).getByLabel("Name", { exact: true }).fill("Rejected name");
  await rowButton(page, "Camera 003").click();
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("region", { name: "Settings for Camera 001" })).toBeVisible();
  await expect(editor(page).getByLabel("Name", { exact: true })).toHaveValue("Rejected name");
  await expect(editor(page).getByRole("alert")).toContainText("Name not saved");
  await editor(page).getByRole("button", { name: "Revert" }).click();
  await expect(editor(page).getByLabel("Name", { exact: true })).toHaveValue("Camera 001");
});

test("a stream test answer for a camera that is no longer open is ignored", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.route("**/source/test", async route => {
    await new Promise(resolve => setTimeout(resolve, 1_500));
    await route.continue().catch(() => undefined);
  });
  await editor(page).getByRole("button", { name: "Test stream" }).click();
  await selectCamera(page, "Camera 001");
  await page.waitForTimeout(2_500);
  await expect(editor(page).getByText(/Stream opened|Stream did not open/)).toHaveCount(0);
});

test("the preview starts only on request and is released on every way out", async ({ page }) => {
  seedFixtures(3);
  const sockets = trackLiveSockets(page);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.waitForTimeout(500);
  expect(sockets.opened()).toBe(0);

  const preview = page.getByRole("region", { name: "Preview of Camera 002" });
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect(preview).toHaveAttribute("data-phase", "live", { timeout: 15_000 });
  expect(sockets.open()).toBe(1);
  await page.getByRole("button", { name: "Close preview" }).click();
  await expect.poll(() => sockets.open()).toBe(0);

  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await selectCamera(page, "Camera 003"); // camera change
  await expect.poll(() => sockets.open()).toBe(0);
  await expect(page.getByRole("region", { name: /Preview of/ })).toHaveCount(0);

  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await nav(page).getByRole("link", { name: "Playback" }).click(); // leaving Cameras
  await expect.poll(() => sockets.open()).toBe(0);

  await openCameras(page);
  await expect(page.getByRole("region", { name: /Preview of/ })).toHaveCount(0); // never restored
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await page.getByRole("button", { name: "Archive…" }).click(); // archive
  await page.getByRole("dialog", { name: "Archive Camera 003?" }).getByRole("button", { name: "Archive camera" }).click();
  await expect.poll(() => sockets.open()).toBe(0);
  await expect(page.getByText("Selected camera is outside the current filter.")).toBeVisible();

  await selectCamera(page, "Camera 001");
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect.poll(() => sockets.open()).toBe(1);
  await page.getByRole("button", { name: /Account menu/ }).click(); // sign out
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect.poll(() => sockets.open()).toBe(0);
});

test("archiving keeps the camera reachable through filters, and restore brings it back", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 002");
  await page.getByRole("button", { name: "Archive…" }).click();
  await page.getByRole("dialog", { name: "Archive Camera 002?" }).getByRole("button", { name: "Archive camera" }).click();
  await expect(page.getByText("Selected camera is outside the current filter.")).toBeVisible();
  await expect(rowButton(page, "Camera 002")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
  await page.getByRole("button", { name: "Show in table" }).click();
  await expect(inventory(page).getByLabel("Archived cameras")).toHaveValue("all");
  await expect(inventory(page).getByRole("row", { name: /Camera 002/ })).toContainText("Archived");
  await page.getByRole("button", { name: "Restore camera" }).click();
  await expect(page.getByRole("button", { name: "Archive…" })).toBeVisible();
});

test("credentials stay out of URLs, browser storage and responses", async ({ page }) => {
  const secret = "S3cret-fixture-pass";
  seedFixtures(2);
  const urls = [];
  page.on("request", request => urls.push(request.url()));
  const bodies = [];
  page.on("response", async response => {
    if (response.url().includes("/api/")) bodies.push(await response.text().catch(() => ""));
  });
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 001");
  await editor(page).getByLabel("RTSP URL", { exact: true }).fill("rtsp://127.0.0.1:8554/nvr-test");
  await editor(page).getByLabel("Username").fill("viewer");
  await editor(page).getByLabel("Password").fill(secret);
  await editor(page).getByRole("button", { name: "Test stream" }).click();
  await expect(editor(page).locator(".field-actions").getByRole("status").or(editor(page).locator(".field-actions").getByRole("alert"))).toBeVisible({ timeout: 20_000 });
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(storage).not.toContain(secret);
  expect(urls.filter(url => url.includes(secret) || url.includes(FIXTURE_PASSWORD))).toEqual([]);
  expect(bodies.filter(body => body.includes(secret))).toEqual([]);
  const listed = await (await page.request.get("/api/cameras")).text();
  expect(listed).not.toContain(secret);
});

test("operators see read-only details and the server refuses their changes", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page, "camera-operator");
  await openCameras(page);
  await expect(page.getByRole("link", { name: "Add Cameras" })).toHaveCount(0);
  await expect(inventory(page).getByRole("checkbox", { name: /for bulk changes/ })).toHaveCount(0);
  await selectCamera(page, "Camera 002");
  await expect(editor(page).getByRole("textbox")).toHaveCount(0);
  await expect(editor(page).getByRole("button", { name: "Save" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Archive…" })).toHaveCount(0);
  await expect(editor(page).locator("dl.details")).toContainText("192.0.2.2");
  const rename = await page.request.patch(`/api/cameras/${fixtures.camera_ids[1]}`, { data: { name: "Nope" } });
  expect(rename.status()).toBe(403);
  const archive = await page.request.delete(`/api/cameras/${fixtures.camera_ids[1]}`);
  expect(archive.status()).toBe(403);
});

for (const width of [320, 768, 1440]) {
  test(`keyboard-only administration at ${width}px`, async ({ page }) => {
    seedFixtures(3);
    await page.setViewportSize({ width, height: 800 });
    await signInNext(page);
    await openCameras(page);
    await expectNoHorizontalOverflow(page);
    await inventory(page).getByRole("searchbox").focus();
    await page.keyboard.type("192.0.2");
    const target = rowButton(page, "Camera 002");
    await target.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("region", { name: "Settings for Camera 002" })).toBeVisible();
    const region = editor(page);
    await expectNoHorizontalOverflow(page);
    await region.getByLabel("Name", { exact: true }).focus();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type(`Gate ${width}`);
    await region.getByRole("button", { name: "Save" }).focus();
    await page.keyboard.press("Enter");
    await expect(region.getByRole("status")).toContainText("Saved name");
    await page.getByRole("tab", { name: "General & Connection" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Recording" })).toHaveAttribute("aria-selected", "true");
    if (width <= 760) {
      // The editor covers the inventory, which keeps its search and scroll underneath.
      const onTop = () => page.evaluate(() => Boolean(document.elementFromPoint(innerWidth / 2, innerHeight / 2)?.closest(".editor-pane")));
      expect(await onTop()).toBe(true);
      await page.getByRole("button", { name: "← Back to Cameras" }).focus();
      await page.keyboard.press("Enter");
      await expect.poll(onTop).toBe(false);
      await expect(inventory(page).getByRole("searchbox")).toHaveValue("192.0.2");
      await expect(inventory(page).getByRole("row", { name: new RegExp(`Gate ${width}`) })).toHaveAttribute("aria-selected", "true");
    } else {
      const separator = page.getByRole("separator", { name: "Resize inventory and settings" });
      await separator.focus();
      const before = Number(await separator.getAttribute("aria-valuenow"));
      await page.keyboard.press("ArrowRight");
      await expect(separator).toHaveAttribute("aria-valuenow", String(before + 2));
    }
    await expectNoHorizontalOverflow(page);
  });
}

test("a selected camera that disappears says so, and comes back when it returns", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 001");

  seedFixtures(0);
  await nav(page).getByRole("link", { name: "Live" }).click();
  await openCameras(page); // remounting refetches the inventory
  await expect(inventory(page).getByText("No cameras have been added yet.")).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "This camera is no longer available." })).toBeVisible();

  seedFixtures(1);
  await nav(page).getByRole("link", { name: "Live" }).click();
  await openCameras(page);
  await expect(page.getByRole("region", { name: "Settings for Camera 001" })).toBeVisible();
  await expect(page.getByText("This camera is no longer available.")).toHaveCount(0);
});

test("a failed preview is visible, Retry starts media, and signing out releases the viewer", async ({ page }) => {
  test.setTimeout(90_000);
  const fixtures = seedFixtures(1);
  // The first live connection is refused the way the server refuses an unavailable camera.
  // Socket routes are installed into the page as it loads, so this precedes signing in.
  let refused = 0;
  await page.routeWebSocket(/\/api\/cameras\/[^/]+\/live\/stream/, socket => {
    if (refused === 0) {
      refused += 1;
      setTimeout(() => socket.close({ code: 4404, reason: "Deterministic browser fixture preview failure" }), 100);
      return;
    }
    socket.connectToServer();
  });
  await signInNext(page);
  await openCameras(page);
  await selectCamera(page, "Camera 001");

  const preview = page.getByRole("region", { name: "Preview of Camera 001" });
  await page.getByRole("button", { name: "Show preview" }).click();
  await expect(preview).toHaveAttribute("data-phase", "error");
  await expect(preview.getByRole("status")).toContainText("Deterministic browser fixture preview failure");

  const viewers = async () => (await (await page.request.get(`/api/cameras/${fixtures.primary_camera_id}/live/status`)).json()).active_viewers;
  await preview.getByRole("button", { name: "Retry" }).click();
  await expect(preview).toHaveAttribute("data-phase", "live", { timeout: 30_000 });
  await expect.poll(viewers).toBeGreaterThan(0);

  // A second session watches the viewer count while this one signs out.
  const observer = await page.context().browser().newContext({ baseURL: "http://127.0.0.1:8765" });
  try {
    const login = await observer.request.post("/api/auth/login", { data: { username: "camera-admin", password: FIXTURE_PASSWORD } });
    expect(login.status()).toBe(200);
    await page.getByRole("button", { name: /Account menu/ }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect.poll(async () => (await (await observer.request.get(`/api/cameras/${fixtures.primary_camera_id}/live/status`)).json()).active_viewers).toBe(0);
  } finally {
    await observer.close();
  }
});

test("the pane width is the only stored layout preference and survives a reload", async ({ page }) => {
  seedFixtures(1);
  await signInNext(page);
  await openCameras(page);
  const resizer = page.getByRole("separator", { name: "Resize inventory and settings" });
  await resizer.focus();
  await page.keyboard.press("End");
  await expect(resizer).toHaveAttribute("aria-valuenow", "75");
  await page.keyboard.press("ArrowLeft");
  await expect(resizer).toHaveAttribute("aria-valuenow", "73");
  const ratio = await page.locator(".admin-panes").evaluate(panes => {
    const left = panes.querySelector(".inventory").getBoundingClientRect().width;
    const right = panes.querySelector(".editor-pane").getBoundingClientRect().width;
    return Math.round(left / (left + right) * 100);
  });
  expect(Math.abs(ratio - 73)).toBeLessThanOrEqual(1);
  await page.reload();
  await expect(page.getByRole("separator", { name: "Resize inventory and settings" })).toHaveAttribute("aria-valuenow", "73");
  const stored = await page.evaluate(() => Object.fromEntries(Object.keys(localStorage).map(key => [key, localStorage.getItem(key)])));
  const table = Object.entries(stored).filter(([key]) => key.startsWith("nvr-next-camera-table:"));
  expect(table).toHaveLength(1);
  expect(Object.keys(JSON.parse(table[0][1])).sort()).toEqual(["paneWidth", "showBrand", "sort"]);
  expect(Object.keys(stored).some(key => /draft|secret|preview|selected|bulk/i.test(key))).toBe(false);
});

test("200-camera filtering and selection record browser measurements without row media", async ({ page, browserName, browser }) => {
  seedFixtures(200);
  const work = trackCameraWork(page);
  const errors = { page: 0, console: 0, failed_requests: 0, http_errors: 0 };
  const details = [];
  await signInNext(page); // the signed-out probe of /api/auth/me is expected to fail
  page.on("pageerror", error => { errors.page += 1; details.push(`page: ${error.message}`); });
  page.on("console", message => { if (message.type() === "error") { errors.console += 1; details.push(`console: ${message.text()}`); } });
  page.on("requestfailed", request => {
    const { pathname } = new URL(request.url());
    const reason = request.failure()?.errorText || "unknown error";
    // The sign-in page's own sign-in-options probe can still be in flight when signing in replaces the page, and the browser then cancels it. That is
    // not a failure of the Cameras workspace; any other failed request, and any other error for this one, still counts.
    if (pathname === "/api/auth/oidc/status" && reason === "net::ERR_ABORTED") return;
    errors.failed_requests += 1;
    details.push(`request failed ${pathname}: ${reason}`);
  });
  page.on("response", response => { if (response.status() >= 400) { errors.http_errors += 1; details.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`); } });
  await openCameras(page);
  await expect(inventory(page).locator("tbody tr")).toHaveCount(200);
  await expect(page.getByText("Select a camera to view settings.")).toBeVisible();

  const elapsed = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() =>
    resolve(performance.now() - performance.getEntriesByName("camera-acceptance-feedback-start").at(-1).startTime)))));
  const search = inventory(page).getByRole("searchbox");
  const filterSamples = [];
  for (let index = 0; index < 10; index += 1) {
    await page.evaluate(() => performance.mark("camera-acceptance-feedback-start"));
    await search.fill(index % 2 ? "Camera 1" : "Camera 20");
    await expect(inventory(page).locator("tbody tr").first()).toBeVisible();
    filterSamples.push(await elapsed());
  }
  await search.fill("");
  const panelSamples = [];
  for (const name of ["Camera 200", "Camera 199", "Camera 200", "Camera 199", "Camera 200"]) {
    await page.evaluate(() => performance.mark("camera-acceptance-feedback-start"));
    await selectCamera(page, name);
    panelSamples.push(await elapsed());
  }
  const system = await (await page.request.get("/api/system/status")).json();
  const errorCount = Object.values(errors).reduce((total, count) => total + count, 0);
  writeAcceptanceMeasurements({
    record_type: "camera-admin-acceptance-measurements",
    measurement_status: "measured",
    app_version: system.version,
    browser: browserName,
    browser_version: browser.version(),
    environment: {
      runner_os: process.env.RUNNER_OS || process.platform,
      runner_arch: process.arch,
      runner_release: require("node:os").release(),
      node_version: process.version,
      browser_mode: "Chromium headless",
      ci: process.env.GITHUB_ACTIONS === "true" ? "GitHub Actions" : "local host",
    },
    configured_camera_count: 200,
    sample_counts: { filter_feedback: filterSamples.length, panel_feedback: panelSamples.length },
    error_counts: errors,
    error_count: errorCount,
    filter_feedback_ms: { p50: median(filterSamples), p95: percentile(filterSamples, 95), samples: filterSamples },
    panel_feedback_ms: { p50: median(panelSamples), p95: percentile(panelSamples, 95), samples: panelSamples },
    inventory_live_requests_before_explicit_preview: work.length,
    production_capacity_gate: "unchecked",
    thirty_day_qualification_gate: "unchecked",
    owner: "camera-browser-acceptance CI job",
    result: errorCount === 0 && work.length === 0 ? "measurement journey completed without browser errors or inventory media requests" : "measurement journey completed; inspect recorded errors and media requests",
    evidence: ["test-results/camera-browser-acceptance.json", "test-results/playwright/"],
  });
  expect(work).toEqual([]);
  expect(errorCount, details.join("\n") || "No browser error details were captured.").toBe(0);
});
