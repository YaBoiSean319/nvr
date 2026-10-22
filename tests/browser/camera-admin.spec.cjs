const { spawn } = require("node:child_process");
const {
  test,
  expect,
  seedFixtures,
  signIn,
  openCameraSettings,
  selectCamera,
  median,
  percentile,
  writeAcceptanceMeasurements,
} = require("./helpers.cjs");

test("empty and one-camera inventories do not auto-select a camera", async ({ page }) => {
  let fixtures = seedFixtures(0);
  await signIn(page);
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-table")).toContainText("No cameras have been added");
  await expect(page.locator("#camera-list")).toContainText("Select a camera");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(0);

  fixtures = seedFixtures(1);
  await page.reload();
  await expect(page.locator("#status")).toBeVisible();
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await expect(page.locator("#camera-list")).toContainText("Select a camera");
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(0);
  await selectCamera(page, fixtures.primary_camera_id);
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(1);
});

test("camera inventory exposes loading and error states and recovers a deleted selection", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);

  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let release;
  const holdResponse = new Promise(resolve => { release = resolve; });
  const delayInventory = async route => {
    const response = await route.fetch();
    captured();
    await holdResponse;
    await route.fulfill({ response });
  };
  await page.route("**/api/cameras", delayInventory);
  await page.locator("#camera-inventory-refresh").click();
  try {
    await requestCaptured;
    await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "loading");
    await expect(page.locator("#camera-inventory-load-status")).toContainText("Loading camera inventory");
  } finally {
    release();
    await page.unroute("**/api/cameras", delayInventory);
  }
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "ready");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);

  const failInventory = route => route.abort("failed");
  await page.route("**/api/cameras", failInventory);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#camera-inventory-load-status")).toContainText("Showing the last loaded 1 camera");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await page.unroute("**/api/cameras", failInventory);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "ready");

  await selectCamera(page, fixtures.primary_camera_id);
  seedFixtures(0);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-table")).toContainText("No cameras have been added");
  await expect(page.locator("#camera-inventory-load-status")).toContainText("Camera 001 is no longer available");
  await expect(page.locator("#camera-list")).toContainText("Camera 001 is no longer available");
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(0);

  seedFixtures(1);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await expect(page.locator("#camera-inventory-load-status")).toContainText("Camera 001 is available again");
  await expect(page.locator("#camera-list")).toContainText("Camera 001 is available again");
  await selectCamera(page, fixtures.primary_camera_id);
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(1);
  await expect(page.locator("#camera-inventory-load-status")).toHaveText("1 camera loaded.");

  await page.locator("#logout").click();
  await expect(page.locator("#login")).toBeVisible();
  await signIn(page);
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "ready");
  await expect(page.locator("#camera-inventory-load-status")).toHaveText("1 camera loaded.");
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(0);
});

test("administrator row selection stays separate from bulk selection and one editor stays open", async ({ page }) => {
  const fixtures = seedFixtures(2);
  const mediaRequests = [];
  page.on("request", request => {
    if (/\/api\/cameras\/[^/]+\/live(?:\?|$)/.test(request.url())) mediaRequests.push(request.url());
  });
  await signIn(page);
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(2);
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(0);

  await page.locator(`[data-camera-select][value="${fixtures.camera_ids[1]}"]`).check();
  await expect(page.locator("#camera-bulk-summary")).toContainText("1 selected");
  await expect(page.locator("#camera-list")).toContainText("Select a camera");

  const firstCameraButton = page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[0]}"]`);
  await firstCameraButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(1);
  await expect(page.locator(`[data-camera-select][value="${fixtures.camera_ids[1]}"]`)).toBeChecked();
  await expect(page.locator('[role="tab"][data-camera-tab][aria-selected="true"]')).toHaveCount(1);
  await expect(page.locator('[data-camera-preview] [data-preview-status]')).toHaveAttribute("data-state", "idle");
  const generalTab = page.locator('[role="tab"][data-camera-tab="general-connection"]');
  await generalTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator('[role="tab"][data-camera-tab="recording"]')).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await expect(generalTab).toHaveAttribute("aria-selected", "true");
  expect(mediaRequests).toEqual([]);

  await page.locator("#camera-inventory-search").fill("Camera 002");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await expect(page.locator("#camera-bulk-summary")).toContainText("1 selected · 0 hidden");
  await page.locator("#camera-inventory-search").fill("Camera 001");
  await expect(page.locator("#camera-bulk-summary")).toContainText("1 hidden by filters");

  await page.locator("#camera-inventory-search").fill("Camera 002");
  await page.locator('#camera-inventory-table button[data-camera-id]').click();
  await expect(page.locator(".filtered-camera-notice")).toBeVisible();
  await expect(page.locator(".filtered-camera-notice")).toContainText("selected camera is hidden");
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(1);
  await page.getByRole("button", { name: "Show in table" }).click();
  await expect(page.locator(".filtered-camera-notice")).toHaveCount(0);
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`)).toBeVisible();
  expect(mediaRequests).toEqual([]);
});

test("editor tabs retain cancel and failed-save drafts and expose partial-save outcomes", async ({ page }) => {
  const fixtures = seedFixtures(1, { failNameSave: true });
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  await page.locator('[data-config-section="camera-details"] > summary').click();
  const name = page.locator('[data-config-section="camera-details"] input[type="text"]');
  await name.fill("Unsaved fixture draft");
  await page.locator('[role="tab"][data-camera-tab="recording"]').click();
  await expect(page.locator("#camera-dirty-decision")).toBeVisible();
  await page.locator('#camera-dirty-decision [data-decision="cancel"]').click();
  await expect(page.locator('[role="tab"][data-camera-tab="general-connection"]')).toHaveAttribute("aria-selected", "true");
  await expect(name).toHaveValue("Unsaved fixture draft");

  await page.locator('[role="tab"][data-camera-tab="recording"]').click();
  await page.locator('#camera-dirty-decision [data-decision="discard"]').click();
  await expect(page.locator('[role="tab"][data-camera-tab="recording"]')).toHaveAttribute("aria-selected", "true");
  await page.locator('[role="tab"][data-camera-tab="general-connection"]').click();
  await page.locator('[data-config-section="camera-details"] > summary').click();
  await expect(page.locator('[data-config-section="camera-details"] input[type="text"]')).toHaveValue("Camera 001");

  await page.locator('[data-config-section="camera-details"] input[type="text"]').fill("Rejected fixture name");
  await page.locator('[data-camera-tab-save="general-connection"]').click();
  await expect(page.locator('[data-camera-tab-status="general-connection"]')).toHaveAttribute("data-state", "error");
  await expect(page.locator('[data-config-section="camera-details"] input[type="text"]')).toHaveValue("Rejected fixture name");
  await expect(page.locator('[data-config-section="camera-details"]')).toHaveAttribute("data-config-dirty", "true");
  const rejected = await page.evaluate(async id => (await (await fetch("/api/cameras")).json()).cameras.find(camera => camera.id === id), fixtures.primary_camera_id);
  expect(rejected.name).toBe("Camera 001");

  const partial = seedFixtures(1);
  await page.reload();
  await expect(page.locator("#status")).toBeVisible();
  await openCameraSettings(page);
  await selectCamera(page, partial.primary_camera_id);
  await page.locator('[data-config-section="camera-details"] > summary').click();
  await page.locator('[data-config-section="camera-details"] input[type="text"]').fill("Saved before source error");
  const directUrl = page.locator('[data-config-section="camera-source"] input[type="url"]').first();
  await directUrl.fill("rtsp://127.0.0.1:8554/nvr-test");
  await directUrl.fill("");
  await page.locator('[data-camera-tab-save="general-connection"]').click();
  await expect(page.locator("#camera-message")).toContainText("Edit camera details saved");
  await expect(page.locator("#camera-message")).toContainText("failed");
  await expect(page.locator('[data-config-section="camera-source"]')).toHaveAttribute("data-config-dirty", "true");
  const saved = await page.evaluate(async id => (await (await fetch("/api/cameras")).json()).cameras.find(camera => camera.id === id), partial.primary_camera_id);
  expect(saved.name).toBe("Saved before source error");
});

test("discovery separates linked candidates, links existing cameras, adds tested devices, and retries scans", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await expect(page.locator("#discovery-table-scroll")).toContainText("Unadded Fixture Camera");
  await expect(page.locator("#discovery-table-scroll")).not.toContainText("Already Added Fixture");
  await expect(page.locator("[data-discovery-count]")).toContainText("added hidden");
  await page.locator("#discovery-show-linked").check();
  await expect(page.locator("#discovery-table-scroll")).toContainText("Already Added Fixture");

  await page.locator(`[data-discovery-candidate-id="${fixtures.discovery_linkable_id}"]`).click();
  await page.locator(".discovery-link-existing button[data-discovery-submit]").click();
  await expect(page.locator("#discovery-message")).toContainText("Camera ID and recording history were preserved");
  const linked = await page.evaluate(async () => (await (await fetch("/api/cameras")).json()).cameras);
  expect(linked).toHaveLength(1);
  expect(linked[0].id).toBe(fixtures.primary_camera_id);
  await expect(page.locator("[data-discovery-count]")).toContainText("not added");

  await page.locator(`[data-discovery-candidate-id="${fixtures.discovery_unlinked_id}"]`).click();
  const setup = page.locator(".discovery-candidate-setup");
  await setup.locator('[data-discovery-field="name"]').fill("Discovered Fixture Camera");
  await setup.locator('[data-discovery-field="address"]').fill("127.0.0.1");
  await setup.locator('[data-discovery-field="port"]').fill("8766");
  await setup.getByRole("button", { name: "Load stream profiles" }).click();
  await expect(setup.locator('[data-discovery-field="profile"] option[value="main"]')).toHaveCount(1);
  await setup.locator('[data-discovery-field="profile"]').selectOption("main");
  await setup.getByRole("button", { name: "Test connection and add camera" }).click();
  await expect(page.locator("#discovery-message")).toContainText("Discovered Fixture Camera was added");
  const afterAdd = await page.evaluate(async () => (await (await fetch("/api/cameras")).json()).cameras);
  expect(afterAdd).toHaveLength(2);
  expect(afterAdd.find(camera => camera.name === "Discovered Fixture Camera").recording_enabled).toBe(false);

  await page.locator("#discovery-ip-range").fill("0.0.0.0/32");
  await page.locator("#discover-ip-range").click();
  await expect(page.locator("#discovery-message")).toContainText("private IPv4");
  await expect(page.locator("#discovery-retry-scan")).toBeVisible();
  await page.locator("#discovery-retry-scan").click();
  await expect(page.locator("#discovery-message")).toContainText("private IPv4");
});

test("a late discovery refresh cannot replace newer candidate results", async ({ page }) => {
  seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await page.locator("#discovery-show-linked").check();
  let captured;
  const firstResponseCaptured = new Promise(resolve => { captured = resolve; });
  let firstResponseFinished;
  const firstResponseFulfilled = new Promise(resolve => { firstResponseFinished = resolve; });
  let release;
  const holdFirstResponse = new Promise(resolve => { release = resolve; });
  let calls = 0;
  const delayFirst = async route => {
    calls += 1;
    if (calls !== 1) return route.continue();
    const response = await route.fetch();
    captured();
    await holdFirstResponse;
    await route.fulfill({ response });
    firstResponseFinished();
  };
  await page.route("**/api/discovery/candidates", delayFirst);
  await page.getByRole("button", { name: "Refresh results" }).click();
  await firstResponseCaptured;
  seedFixtures(0);
  await page.getByRole("button", { name: "Refresh results" }).click();
  await expect(page.locator("[data-discovery-count]")).toContainText("2 shown · 2 not added · 0 already added");
  release();
  await firstResponseFulfilled;
  await expect(page.locator("#discovery-table-scroll")).not.toContainText("Already Added Fixture");
  await expect(page.locator("[data-discovery-count]")).toContainText("2 shown · 2 not added · 0 already added");
  await page.unroute("**/api/discovery/candidates", delayFirst);
});

test("manual add resolves an ONVIF profile and tests actual generated RTSP media", async ({ page }) => {
  test.setTimeout(120_000);
  seedFixtures(0);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await page.locator('[data-camera-add-mode="manual"]').click();
  await page.locator("#camera-name").fill("Manual Fixture Camera");
  await page.locator("#camera-ip").fill("127.0.0.1");
  await page.locator("#camera-port").fill("8766");
  await page.locator("#load-camera-profiles").click();
  await expect(page.locator('#camera-profile option[value="main"]')).toHaveCount(1);
  await page.locator("#camera-profile").selectOption("main");
  await page.locator('#camera-form button[type="submit"]').click();
  await expect(page.locator("#camera-add-message")).toContainText("Manual Fixture Camera added");
  await page.locator("#camera-manual-open-inventory").click();
  await expect(page.locator("#camera-inventory-table")).toContainText("Manual Fixture Camera");
  const cameras = await page.evaluate(async () => (await (await fetch("/api/cameras")).json()).cameras);
  expect(cameras).toHaveLength(1);
  expect(cameras[0].recording_enabled).toBe(false);
});

test("CSV preview loading, request failure, stale response, partial apply, and failed-only retry", async ({ page }) => {
  test.setTimeout(180_000);
  seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await page.locator('[data-camera-add-mode="csv"]').click();
  const csv = page.locator("#camera-import-csv");

  let firstFailed = false;
  const failOnce = async route => {
    if (!firstFailed) {
      firstFailed = true;
      await route.abort("failed");
    } else {
      await route.continue();
    }
  };
  await page.route("**/api/cameras/import/preview", failOnce);
  await csv.fill("name,stream_url,recording_mode\nPreview Failure,rtsp://127.0.0.1:8554/nvr-test,disabled");
  await page.locator("#preview-import").click();
  await expect(page.locator("#camera-import-message")).not.toHaveText("Validating CSV rows…");
  await expect(page.locator("#camera-import-message")).not.toBeEmpty();
  await page.unroute("**/api/cameras/import/preview", failOnce);

  let previewRequests = 0;
  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let oldResponseFinished;
  const oldResponseCompleted = new Promise(resolve => { oldResponseFinished = resolve; });
  let release;
  const releaseFirstRequest = new Promise(resolve => { release = resolve; });
  const delayPreview = async route => {
    previewRequests += 1;
    if (previewRequests === 1) {
      const response = await route.fetch();
      captured();
      await releaseFirstRequest;
      await route.fulfill({ response });
      oldResponseFinished();
    } else {
      await route.continue();
    }
  };
  await page.route("**/api/cameras/import/preview", delayPreview);
  await csv.fill("name,stream_url,recording_mode\nStale Preview Fixture,rtsp://127.0.0.1:8554/nvr-test,disabled");
  await page.locator("#preview-import").click();
  await requestCaptured;
  await expect(page.locator("#camera-import-message")).toContainText("Validating CSV rows");
  await csv.fill("name,stream_url,recording_mode\nNewer Preview Fixture,rtsp://127.0.0.1:8554/nvr-test,disabled");
  await page.locator("#preview-import").click();
  await expect(page.locator('#import-preview tr[data-import-status="accepted"]')).toHaveCount(1);
  await expect(page.locator("#import-preview")).toContainText("Newer Preview Fixture");
  release();
  await oldResponseCompleted;
  await expect(page.locator("#import-preview")).toContainText("Newer Preview Fixture");
  await expect(page.locator("#import-preview")).not.toContainText("Stale Preview Fixture");

  await csv.fill("name,stream_url,recording_mode\nCSV Fixture,rtsp://127.0.0.1:8554/nvr-test,disabled\nCSV Retry Fixture,rtsp://127.0.0.1:8554/nvr-retry,disabled\nCamera 001,rtsp://127.0.0.1:8554/nvr-test,disabled\nInvalid URL,not-rtsp,disabled");
  await page.locator("#preview-import").click();
  await expect(page.locator('#import-preview tr[data-import-status="accepted"]')).toHaveCount(2);
  await expect(page.locator('#import-preview tr[data-import-status="duplicate"]')).toHaveCount(1);
  await expect(page.locator('#import-preview tr[data-import-status="invalid"]')).toHaveCount(1);
  await expect(page.locator("#import-preview")).toContainText("CSV Fixture");
  await page.unroute("**/api/cameras/import/preview", delayPreview);

  const applyRequests = [];
  page.on("request", request => {
    if (request.url().endsWith("/api/cameras/import/apply")) applyRequests.push(request.postDataJSON());
  });
  await page.locator("#apply-import").click();
  await expect(page.locator('#import-preview tr[data-import-status="created"]')).toHaveCount(1);
  await expect(page.locator('#import-preview tr[data-import-status="failed"]')).toHaveCount(1);
  await expect(page.locator("#retry-import-failed")).toBeVisible();
  const publisher = spawn("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
    "-t", "90", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "10",
    "-f", "rtsp", "-rtsp_transport", "tcp", "rtsp://127.0.0.1:8554/nvr-retry",
  ], { stdio: "ignore" });
  try {
    await page.waitForTimeout(1500);
    await page.locator("#retry-import-failed").click();
    await expect(page.locator('#import-preview tr[data-import-status="created"]')).toHaveCount(2);
    expect(applyRequests).toHaveLength(2);
    expect(applyRequests[0].csv_text).toContain("CSV Fixture");
    expect(applyRequests[0].csv_text).toContain("CSV Retry Fixture");
    expect(applyRequests[1].csv_text).toContain("CSV Retry Fixture");
    expect(applyRequests[1].csv_text).not.toContain("CSV Fixture");
  } finally {
    publisher.kill("SIGTERM");
  }
});

test("bulk review applies fixed IDs, retains per-camera outcomes, and retries only the failed target", async ({ page }) => {
  const fixtures = seedFixtures(2, { bulkTrigger: true });
  const updates = [];
  page.on("request", request => {
    if (request.url().endsWith("/api/cameras/bulk-update")) updates.push(request.postDataJSON());
  });
  await signIn(page);
  await openCameraSettings(page);
  for (const cameraId of fixtures.camera_ids) await page.locator(`[data-camera-select][value="${cameraId}"]`).check();
  await expect(page.locator("#camera-bulk-summary")).toContainText("2 selected");
  await expect(page.locator("#camera-list")).toContainText("Select a camera");
  await page.locator("#bulk-recording-enabled").selectOption("true");
  await page.locator("#apply-bulk").click();
  await expect(page.locator("dialog.camera-bulk-dialog")).toContainText("This review is fixed to 2 selected camera ID(s)");
  await expect(page.locator("dialog.camera-bulk-dialog h2")).toBeFocused();
  await expect(page.locator("dialog.camera-bulk-dialog")).toContainText("Enable recording");
  await page.locator("dialog.camera-bulk-dialog button:not(.primary)").click();
  await expect(page.locator("dialog.camera-bulk-dialog")).toBeHidden();
  await expect(page.locator("#apply-bulk")).toBeFocused();
  await page.locator("#apply-bulk").click();
  await expect(page.locator("dialog.camera-bulk-dialog h2")).toBeFocused();
  await page.locator('dialog.camera-bulk-dialog button.primary').click();
  await expect(page.locator('#camera-bulk-results-dialog [data-status="success"]')).toHaveCount(1);
  await expect(page.locator('#camera-bulk-results-dialog [data-status="failed"]')).toHaveCount(1);
  await expect(page.locator("#camera-bulk-results-title")).toBeFocused();
  expect(updates[0].camera_ids).toHaveLength(2);

  seedFixtures(2);
  await page.locator("#camera-bulk-retry-failed").click();
  await expect(page.locator('#camera-bulk-results-dialog [data-status="success"]')).toHaveCount(2);
  expect(updates).toHaveLength(2);
  expect(updates[1].camera_ids).toEqual([fixtures.bulk_rejected_camera_id]);
  await page.locator("#camera-bulk-dismiss-results").click();
  await expect(page.locator("#camera-bulk-results-dialog")).toBeHidden();
  await expect(page.locator("#camera-inventory-heading")).toBeFocused();
});

test("group policy editor loads shared settings and saves the group retention target", async ({ page }) => {
  const fixtures = seedFixtures(200);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="groups"]').click();
  await page.locator(`#group-management-list [data-group-id="${fixtures.group_id}"]`).click();
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  await page.locator('[data-group-policy-tab="recording"]').click();
  await expect(page.locator('[data-group-policy-tab="recording"]')).toHaveAttribute("aria-selected", "true");
  await page.locator("#group-retention-days").fill("14");
  await page.locator('[data-group-tab-save="recording"]').click();
  await expect(page.locator("#schedule-message")).toContainText("Saved group retention target: 14 days");
  const policy = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/retention`)).json()), fixtures.group_id);
  expect(policy.retention_days).toBe(14);
});

test("only explicit preview starts media and close, selection change, and navigation release it", async ({ page }) => {
  const fixtures = seedFixtures(2);
  const mediaRequests = [];
  page.on("request", request => {
    if (/\/api\/cameras\/[^/]+\/live(?:\?|$)/.test(request.url())) mediaRequests.push(request.url());
  });
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.camera_ids[0]);
  await page.locator("#camera-inventory-search").fill("Camera 002");
  await expect(page.locator(".filtered-camera-notice")).toBeVisible();
  expect(mediaRequests).toEqual([]);
  await page.locator("#camera-inventory-search").fill("");
  await expect(page.locator("[data-preview-start]")).toHaveAttribute("aria-label", "Show muted live preview for Camera 001");
  await page.locator(`[data-preview-start]`).click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await expect(page.locator(".camera-preview-image")).toHaveAttribute("alt", "Muted live preview for Camera 001");
  await expect.poll(async () => page.locator(".camera-preview-image").evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
  const firstStatus = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()), fixtures.camera_ids[0]);
  expect(firstStatus.active_viewers).toBeGreaterThan(0);
  expect(mediaRequests.filter(url => url.includes("/live?quality=low"))).toHaveLength(1);
  expect(new Set(mediaRequests.map(url => new URL(url).pathname))).toEqual(new Set([`/api/cameras/${fixtures.camera_ids[0]}/live`]));

  await page.locator("[data-preview-close]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBe(0);

  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[1]}"]`).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[1]}"]`)).toBeVisible();
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBe(0);
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");

  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await page.locator('#console-nav [data-screen="live"]').click();
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[1])).toBe(0);
  expect(mediaRequests.filter(url => url.includes("/live?quality=low"))).toHaveLength(3);
});

test("operator inventory is read-only, responsive, keyboard and touch accessible, and logout clears session state", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page, "camera-operator");
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await expect(page.locator(".camera-select-column")).toHaveCount(0);
  await expect(page.locator('[data-camera-workspace-button="add"]')).toBeHidden();
  await expect(page.locator('[data-camera-workspace-button="groups"]')).toBeHidden();
  await selectCamera(page, fixtures.primary_camera_id);
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.primary_camera_id}"] [data-config-section]`)).toHaveCount(0);

  for (const width of [320, 360, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator("#selected-camera-heading")).toBeVisible();
  }
  await page.setViewportSize({ width: 360, height: 780 });
  const rowButton = page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.primary_camera_id}"]`);
  await page.locator("#camera-back-to-inventory").click();
  await page.locator("#camera-inventory-search").fill("Camera 001");
  const box = await rowButton.boundingBox();
  await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.primary_camera_id}"]`)).toBeVisible();
  await page.locator("#camera-back-to-inventory").click();
  await expect(page.locator("#camera-inventory-search")).toHaveValue("Camera 001");
  await expect(page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.primary_camera_id}"]`)).toBeFocused();

  await page.locator("#theme-choice").selectOption("light");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.locator("#theme-choice").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator("#theme-choice").selectOption("system");
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });
  const motion = await page.locator("#console-nav").evaluate(element => getComputedStyle(element).transitionDuration);
  expect(motion.split(",").every(value => Number.parseFloat(value) === 0)).toBe(true);
  await page.emulateMedia({ reducedMotion: "no-preference", colorScheme: "light" });

  await page.locator("#logout").click();
  await expect(page.locator("#login")).toBeVisible();
  const sessionStatus = await page.evaluate(async () => (await fetch("/api/auth/me")).status);
  expect(sessionStatus).toBe(401);
  const storage = await page.evaluate(() => Object.keys(localStorage));
  expect(storage.some(key => /draft|secret|preview|selected-camera|bulk-selection/i.test(key))).toBe(false);
});

test("200-camera filtering and panel selection record browser measurements without row media", async ({ page, browserName, browser }) => {
  const fixtures = seedFixtures(200);
  const mediaRequests = [];
  const browserErrors = { page: 0, console: 0, failed_requests: 0, http_errors: 0 };
  page.on("request", request => {
    if (/\/api\/cameras\/[^/]+\/live(?:\?|$)/.test(request.url())) mediaRequests.push(request.url());
  });
  page.on("pageerror", () => { browserErrors.page += 1; });
  page.on("console", message => { if (message.type() === "error") browserErrors.console += 1; });
  page.on("requestfailed", () => { browserErrors.failed_requests += 1; });
  page.on("response", response => { if (response.status() >= 400) browserErrors.http_errors += 1; });
  await signIn(page);
  await openCameraSettings(page);
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(200);
  await expect(page.locator("#camera-list")).toContainText("Select a camera");
  await expect(page.locator(`#camera-inventory-table tr[data-camera-id="${fixtures.camera_ids[0]}"]`)).toContainText("Not reported");
  await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[16]}"]`).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[16]}"]`)).toContainText("unknown codec");
  const filterSamples = [];
  for (let index = 0; index < 10; index += 1) {
    const term = index % 2 ? "Camera 1" : "Camera 20";
    await page.evaluate(() => performance.mark("camera-acceptance-feedback-start"));
    await page.locator("#camera-inventory-search").fill(term);
    await expect(page.locator("#camera-inventory-table tbody tr").first()).toBeVisible();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    filterSamples.push(await page.evaluate(() => performance.now() - performance.getEntriesByName("camera-acceptance-feedback-start").at(-1).startTime));
  }
  await page.locator("#camera-inventory-search").fill("");
  const panelSamples = [];
  for (const cameraId of [fixtures.camera_ids[199], fixtures.camera_ids[198], fixtures.camera_ids[199], fixtures.camera_ids[198], fixtures.camera_ids[199]]) {
    await page.evaluate(() => performance.mark("camera-acceptance-feedback-start"));
    await page.locator(`#camera-inventory-table button[data-camera-id="${cameraId}"]`).click();
    await expect(page.locator(`#camera-list article[data-camera-id="${cameraId}"]`)).toBeVisible();
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    panelSamples.push(await page.evaluate(() => performance.now() - performance.getEntriesByName("camera-acceptance-feedback-start").at(-1).startTime));
  }
  const system = await page.request.get("/api/system/status").then(response => response.json());
  const errorCount = Object.values(browserErrors).reduce((total, count) => total + count, 0);
  const evidence = {
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
    error_counts: browserErrors,
    error_count: errorCount,
    filter_feedback_ms: { p50: median(filterSamples), p95: percentile(filterSamples, 95), samples: filterSamples },
    panel_feedback_ms: { p50: median(panelSamples), p95: percentile(panelSamples, 95), samples: panelSamples },
    inventory_live_requests_before_explicit_preview: mediaRequests.length,
    production_capacity_gate: "unchecked",
    thirty_day_qualification_gate: "unchecked",
    owner: "camera-browser-acceptance CI job",
    result: errorCount === 0 && mediaRequests.length === 0 ? "measurement journey completed without browser errors or inventory media requests" : "measurement journey completed; inspect recorded errors and media requests",
    evidence: ["test-results/camera-browser-acceptance.json", "test-results/playwright/"],
  };
  writeAcceptanceMeasurements(evidence);
  expect(mediaRequests).toEqual([]);
  expect(errorCount).toBe(0);
  expect(evidence.sample_counts.filter_feedback).toBe(10);
  expect(evidence.sample_counts.panel_feedback).toBe(5);
});

test("resizer persists only a bounded harmless preference, and camera assets resolve", async ({ page }) => {
  seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator("[data-camera-pane-resizer]").focus();
  await page.keyboard.press("End");
  await expect(page.locator("[data-camera-pane-resizer]")).toHaveAttribute("aria-valuenow", "75");
  const preference = await page.evaluate(() => ({
    keys: Object.keys(localStorage),
    value: localStorage.getItem(`nvr-camera-pane-ratio:${encodeURIComponent("camera-admin")}`),
  }));
  expect(preference.value).toBe("75");
  expect(preference.keys.filter(key => key.startsWith("nvr-camera-pane-ratio:"))).toHaveLength(1);
  const assetResults = await page.evaluate(async () => {
    const urls = [...document.querySelectorAll('script[src],link[href],img[src],source[src],video[src],audio[src],object[data]')]
      .map(element => element.getAttribute("src") || element.getAttribute("href") || element.getAttribute("data"))
      .filter(url => url && new URL(url, location.href).origin === location.origin);
    return Promise.all([...new Set(urls)].map(async url => ({ url, status: (await fetch(url)).status })));
  });
  expect(assetResults.filter(asset => asset.status !== 200)).toEqual([]);
});

test("branded accent keeps readable foreground contrast in light and dark themes", async ({ page }) => {
  seedFixtures(0);
  await signIn(page);
  await page.locator('#console-nav [data-screen="app-settings"]').click();
  await page.locator('[data-settings-category="appearance"]').click();
  await page.locator("#brand-app-name").fill("Acceptance Fixture NVR");
  await page.locator("#brand-primary-color").fill("#123456");
  await page.locator("#brand-accent-color").fill("#ffffff");
  await page.locator("#brand-save").click();
  await expect(page.locator("#branding-message")).toContainText("Branding saved and published");
  await expect(page.locator("#brand-header-name")).toHaveText("Acceptance Fixture NVR");
  await expect(page.locator("html")).toHaveCSS("--accent-ink", "rgb(0, 0, 0)");
  await page.locator("#theme-choice").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveCSS("--accent-ink", "rgb(0, 0, 0)");
});
