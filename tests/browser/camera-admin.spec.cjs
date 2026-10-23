const { spawn } = require("node:child_process");
const { request: playwrightRequest } = require("playwright");
const {
  test,
  expect,
  FIXTURE_PASSWORD,
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
  await expect(page.locator("#camera-list")).toContainText("No cameras have been added");
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
    captured();
    await holdResponse;
    await route.continue();
  };
  await page.route("**/api/cameras", delayInventory);
  await page.locator("#camera-inventory-refresh").click();
  try {
    await requestCaptured;
    await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "loading");
    await expect(page.locator("#camera-inventory-load-status")).toContainText("Loading camera inventory");
  } finally {
    release();
  }
  await expect(page.locator("#camera-inventory-load-status")).toHaveAttribute("data-state", "ready");
  await expect(page.locator("#camera-inventory-table tbody tr")).toHaveCount(1);
  await page.unroute("**/api/cameras", delayInventory);

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
  await page.locator('[data-camera-tab-revert="general-connection"]').click();
  await expect(page.locator('[data-config-section="camera-details"] input[type="text"]')).toHaveValue("Camera 001");
  await expect(page.locator('[data-camera-tab-status="general-connection"]')).toContainText("Reverted to saved settings");

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
  await page.reload();
  await openCameraSettings(page);
  await selectCamera(page, partial.primary_camera_id);
  await page.locator('[role="tab"][data-camera-tab="general-connection"]').click();
  await page.locator('[data-config-section="camera-details"] > summary').click();
  await expect(page.locator('[data-config-section="camera-details"] input[type="text"]')).toHaveValue("Saved before source error");
});

test("dirty camera navigation waits for a successful save and keeps failed drafts on the current tab", async ({ page }) => {
  let fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  await page.locator('[data-config-section="camera-details"] > summary').click();
  const name = page.locator('[data-config-section="camera-details"] input[type="text"]');
  const general = page.locator('[role="tab"][data-camera-tab="general-connection"]');
  const recording = page.locator('[role="tab"][data-camera-tab="recording"]');
  await name.fill("Committed after response");

  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const holdCommittedSave = async route => {
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
  };
  await page.route(`**/api/cameras/${fixtures.primary_camera_id}`, holdCommittedSave);
  await recording.click();
  await expect(page.locator("#camera-dirty-decision")).toBeVisible();
  await page.locator('#camera-dirty-decision [data-decision="save"]').click();
  await requestCaptured;
  await expect(general).toHaveAttribute("aria-selected", "true");
  await expect(recording).toHaveAttribute("aria-selected", "false");
  const committed = await page.evaluate(async id => (await (await fetch("/api/cameras")).json()).cameras.find(camera => camera.id === id), fixtures.primary_camera_id);
  expect(committed.name).toBe("Committed after response");
  release();
  await expect(recording).toHaveAttribute("aria-selected", "true");
  await page.unroute(`**/api/cameras/${fixtures.primary_camera_id}`, holdCommittedSave);

  fixtures = seedFixtures(1, { failNameSave: true });
  await page.reload();
  await expect(page.locator("#status")).toBeVisible();
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  await page.locator('[data-config-section="camera-details"] > summary').click();
  await name.fill("Rejected navigation draft");
  await recording.click();
  await expect(page.locator("#camera-dirty-decision")).toBeVisible();
  await page.locator('#camera-dirty-decision [data-decision="save"]').click();
  await expect(general).toHaveAttribute("aria-selected", "true");
  await expect(recording).toHaveAttribute("aria-selected", "false");
  await expect(name).toHaveValue("Rejected navigation draft");
  await expect(page.locator('[data-config-section="camera-details"]')).toHaveAttribute("data-config-dirty", "true");
  const rejected = await page.evaluate(async id => (await (await fetch("/api/cameras")).json()).cameras.find(camera => camera.id === id), fixtures.primary_camera_id);
  expect(rejected.name).toBe("Camera 001");
});

test("recording settings save, survive reload, and revert a later draft", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);

  const recordingTab = page.locator('[role="tab"][data-camera-tab="recording"]');
  await recordingTab.click();
  await expect(recordingTab).toHaveAttribute("aria-selected", "true");
  const mode = page.getByLabel("Recording mode for Camera 001");
  const enabled = page.locator('[data-config-section="camera-recording-policy"] input[type="checkbox"]');
  await mode.selectOption("motion");
  await page.locator('[data-camera-tab-save="recording"]').click();
  await expect(page.locator('[data-camera-tab-status="recording"]')).toHaveAttribute("data-state", "saved");

  const readCamera = () => page.evaluate(async id => (await (await fetch("/api/cameras")).json()).cameras.find(camera => camera.id === id), fixtures.primary_camera_id);
  const saved = await readCamera();
  expect(saved.recording_mode).toBe("motion");
  expect(saved.recording_enabled).toBe(false);

  await page.reload();
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  await recordingTab.click();
  await expect(mode).toHaveValue("motion");
  await expect(enabled).not.toBeChecked();

  await mode.selectOption("disabled");
  await expect(page.locator('[data-camera-tab-revert="recording"]')).toBeEnabled();
  await page.locator('[data-camera-tab-revert="recording"]').click();
  await expect(mode).toHaveValue("motion");
  await expect(page.locator('[data-camera-tab-status="recording"]')).toContainText("Reverted to saved settings");
  const reverted = await readCamera();
  expect(reverted.recording_mode).toBe("motion");
  expect(reverted.recording_enabled).toBe(false);
});

test("motion, AI, schedule, retention, history, and revert controls persist through the authenticated app", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  const getCamera = () => page.evaluate(async id => {
    const response = await fetch("/api/cameras");
    return (await response.json()).cameras.find(camera => camera.id === id);
  }, fixtures.primary_camera_id);
  const reloadInTab = async tabId => {
    await page.reload();
    await openCameraSettings(page);
    await selectCamera(page, fixtures.primary_camera_id);
    const tab = page.locator(`[role="tab"][data-camera-tab="${tabId}"]`);
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
    return tab;
  };

  const motionTab = page.locator('[role="tab"][data-camera-tab="motion"]');
  await motionTab.click();
  await expect(motionTab).toHaveAttribute("aria-selected", "true");
  await page.getByLabel("Motion pre-roll seconds").fill("12");
  await page.getByLabel("Motion post-roll seconds").fill("42");
  const motionSource = page.locator('[data-config-section="camera-motion-source"]');
  await motionSource.getByLabel("Use server motion detection when ONVIF events are unavailable").check();
  const sensitivity = motionSource.getByLabel("Server motion sensitivity (1–100)");
  const editorSensitivityId = await sensitivity.getAttribute("id");
  const manualSensitivityId = await page.locator("#camera-form #motion-sensitivity").getAttribute("id");
  expect(editorSensitivityId).toBe("camera-editor-motion-sensitivity");
  expect(manualSensitivityId).toBe("motion-sensitivity");
  expect(editorSensitivityId).not.toBe(manualSensitivityId);
  await expect(motionSource.locator(`label[for="${editorSensitivityId}"]`)).toHaveCount(1);
  await expect(page.locator(`#camera-form label[for="${manualSensitivityId}"]`)).toHaveCount(1);
  await sensitivity.fill("64");
  await motionSource.getByRole("button", { name: "Add zone by coordinates" }).click();
  await page.locator('[data-camera-tab-save="motion"]').click();
  await expect(page.locator('[data-camera-tab-status="motion"]')).toHaveAttribute("data-state", "saved");
  const savedMotion = await getCamera();
  expect(savedMotion.motion_pre_roll_seconds).toBe(12);
  expect(savedMotion.motion_post_roll_seconds).toBe(42);
  expect(savedMotion.motion_fallback_enabled).toBe(true);
  expect(savedMotion.motion_sensitivity).toBe(64);
  expect(savedMotion.motion_zones).toEqual([{ x: 0.1, y: 0.1, width: 0.25, height: 0.25 }]);
  await reloadInTab("motion");
  await expect(page.getByLabel("Motion pre-roll seconds")).toHaveValue("12");
  await expect(page.getByLabel("Motion post-roll seconds")).toHaveValue("42");
  await expect(page.locator('[data-config-section="camera-motion-source"] input[type="checkbox"]')).toBeChecked();
  await expect(page.locator("#camera-editor-motion-sensitivity")).toHaveValue("64");
  await expect(page.locator('[data-config-section="camera-motion-source"] [data-zone-field="x"]')).toHaveValue("0.1");
  await page.getByLabel("Motion pre-roll seconds").fill("15");
  await page.locator('[data-camera-tab-revert="motion"]').click();
  await expect(page.getByLabel("Motion pre-roll seconds")).toHaveValue("12");
  expect((await getCamera()).motion_pre_roll_seconds).toBe(12);

  const aiTab = page.locator('[role="tab"][data-camera-tab="ai"]');
  await aiTab.click();
  await expect(aiTab).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "Configure person/vehicle detection" }).click();
  const aiPanel = page.locator('[data-config-section="camera-ai"]');
  await expect(aiPanel.locator('[data-ai-field="fps"]')).toBeVisible();
  const inheritAI = aiPanel.locator('[data-ai-field="inherit"]');
  if (await inheritAI.isChecked()) await inheritAI.uncheck();
  await aiPanel.locator('[data-ai-field="fps"]').fill("1.2");
  await aiPanel.locator('[data-ai-field="confidence"]').fill("0.72");
  await page.locator('[data-camera-tab-save="ai"]').click();
  await expect(page.locator('[data-camera-tab-status="ai"]')).toHaveAttribute("data-state", "saved");
  const savedAI = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/ai`)).json()), fixtures.primary_camera_id);
  expect(savedAI.config_override).toBe(true);
  expect(savedAI.analysis_fps).toBe(1.2);
  expect(savedAI.confidence_threshold).toBe(0.72);

  await reloadInTab("ai");
  await page.getByRole("button", { name: "Configure person/vehicle detection" }).click();
  await expect(aiPanel.locator('[data-ai-field="fps"]')).toBeVisible();
  await expect(aiPanel.locator('[data-ai-field="fps"]')).toHaveValue("1.2");
  await expect(aiPanel.locator('[data-ai-field="confidence"]')).toHaveValue("0.72");
  await expect(aiPanel.locator('[data-ai-field="inherit"]')).not.toBeChecked();

  await aiPanel.locator('[data-ai-field="confidence"]').fill("0.91");
  await page.locator('[data-camera-tab-revert="ai"]').click();
  await expect(aiPanel.locator('[data-ai-field="confidence"]')).toHaveValue("0.72");
  await expect(page.locator('[data-camera-tab-status="ai"]')).toContainText("Reverted to saved settings");
  const revertedAI = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/ai`)).json()), fixtures.primary_camera_id);
  expect(revertedAI.confidence_threshold).toBe(0.72);

  const scheduleTab = page.locator('[role="tab"][data-camera-tab="schedule-retention"]');
  await scheduleTab.click();
  await expect(scheduleTab).toHaveAttribute("aria-selected", "true");
  const schedulePanel = page.locator('[data-config-section="camera-schedule"]');
  const scheduleLoaded = page.waitForResponse(response => response.url().endsWith(`/api/cameras/${fixtures.primary_camera_id}/schedule`) && response.status() === 200);
  await schedulePanel.getByRole("button", { name: "Load camera schedule" }).click();
  await scheduleLoaded;
  const inheritSchedule = schedulePanel.locator('input[type="checkbox"]');
  if (await inheritSchedule.isChecked()) await inheritSchedule.uncheck();
  await schedulePanel.getByLabel("Schedule time zone (IANA)").fill("UTC");
  await schedulePanel.getByRole("button", { name: "Add schedule interval" }).click();
  const interval = schedulePanel.locator(".schedule-interval").last();
  await interval.locator('[data-schedule-field="start"]').fill("07:30");
  await interval.locator('[data-schedule-field="end"]').fill("18:30");
  await interval.locator('[data-schedule-field="mode"]').selectOption("motion");
  await page.locator('[data-camera-tab-save="schedule-retention"]').click();
  await expect(page.locator('[data-camera-tab-status="schedule-retention"]')).toHaveAttribute("data-state", "saved");
  const savedSchedule = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/schedule`)).json()), fixtures.primary_camera_id);
  expect(savedSchedule.configured).toBe(true);
  expect(savedSchedule.inherits_group).toBe(false);
  expect(savedSchedule.timezone).toBe("UTC");
  expect(savedSchedule.intervals).toEqual([{ days: [0, 1, 2, 3, 4], start: "07:30", end: "18:30", mode: "motion" }]);

  await reloadInTab("schedule-retention");
  const scheduleReloaded = page.waitForResponse(response => response.url().endsWith(`/api/cameras/${fixtures.primary_camera_id}/schedule`) && response.status() === 200);
  await schedulePanel.getByRole("button", { name: "Load camera schedule" }).click();
  await scheduleReloaded;
  await expect(schedulePanel).toBeVisible();
  await expect(schedulePanel.locator('[data-schedule-field="start"]')).toHaveValue("07:30");
  await expect(schedulePanel.locator('[data-schedule-field="end"]')).toHaveValue("18:30");
  await expect(schedulePanel.locator('[data-schedule-field="mode"]')).toHaveValue("motion");
  await schedulePanel.locator('[data-schedule-field="end"]').fill("19:00");
  await page.locator('[data-camera-tab-revert="schedule-retention"]').click();
  await expect(schedulePanel.locator('[data-schedule-field="end"]')).toHaveValue("18:30");
  expect((await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/schedule`)).json()), fixtures.primary_camera_id)).intervals).toEqual(savedSchedule.intervals);

  const retention = page.locator('[data-config-section="camera-retention"]');
  const inheritRetention = retention.locator('input[type="checkbox"]');
  if (await inheritRetention.isChecked()) await inheritRetention.uncheck();
  await retention.locator('input[type="number"]').fill("21");
  await page.locator('[data-camera-tab-save="schedule-retention"]').click();
  await expect(page.locator('[data-camera-tab-status="schedule-retention"]')).toHaveAttribute("data-state", "saved");
  const savedRetention = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/retention`)).json()), fixtures.primary_camera_id);
  expect(savedRetention.policy_source).toBe("camera");
  expect(savedRetention.retention_days).toBe(21);

  await reloadInTab("schedule-retention");
  await expect(retention.locator('input[type="number"]')).toHaveValue("21");
  await expect(retention.locator('input[type="checkbox"]')).not.toBeChecked();
  const retentionScheduleLoaded = page.waitForResponse(response => response.url().endsWith(`/api/cameras/${fixtures.primary_camera_id}/schedule`) && response.status() === 200);
  await schedulePanel.getByRole("button", { name: "Load camera schedule" }).click();
  await retentionScheduleLoaded;
  await expect(schedulePanel.locator('[data-schedule-field="end"]')).toHaveValue("18:30");

  await retention.locator('input[type="number"]').fill("33");
  await page.locator('[data-camera-tab-revert="schedule-retention"]').click();
  await expect(retention.locator('input[type="number"]')).toHaveValue("21");
  const revertedRetention = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/retention`)).json()), fixtures.primary_camera_id);
  expect(revertedRetention.retention_days).toBe(21);
  const retainedSchedule = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/schedule`)).json()), fixtures.primary_camera_id);
  expect(retainedSchedule.intervals).toEqual(savedSchedule.intervals);

  const historyTab = page.locator('[role="tab"][data-camera-tab="health-history"]');
  await historyTab.click();
  await expect(historyTab).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "View coverage history" }).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.primary_camera_id}"]`)).toContainText("No coverage transitions recorded yet.");
  const history = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/coverage`)).json()), fixtures.primary_camera_id);
  expect(history.intervals).toEqual([]);

  const cameraEditor = page.locator(`#camera-list article[data-camera-id="${fixtures.primary_camera_id}"]`);
  page.once("dialog", dialog => dialog.accept());
  await cameraEditor.getByRole("button", { name: "Archive camera" }).click();
  await expect(cameraEditor.getByRole("button", { name: "Restore camera" })).toBeVisible();
  expect((await getCamera()).archived_at).not.toBeNull();
  await cameraEditor.getByRole("button", { name: "Restore camera" }).click();
  await expect(cameraEditor.getByRole("button", { name: "Archive camera" })).toBeVisible();
  expect((await getCamera()).archived_at).toBeNull();
  await page.reload();
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);
  await historyTab.click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.primary_camera_id}"]`).getByRole("button", { name: "Archive camera" })).toBeVisible();
});

test("discovery separates linked candidates, links existing cameras, adds tested devices, and retries scans", async ({ page }) => {
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await expect(page.locator("#discovery-table-scroll")).toContainText("Unadded Fixture Camera");
  await expect(page.locator("#discovery-table-scroll")).not.toContainText("Already Added Fixture");
  await expect(page.locator("[data-discovery-count]")).toContainText("2 not-added devices shown");
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

test("discovery scan shows progress, retries after a network failure, and ingests an ONVIF result", async ({ page }) => {
  seedFixtures(0);
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="add"]').click();
  await page.locator("details.discovery-scan-advanced summary").click();
  await page.locator("#discovery-timeout").fill("0.5");

  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const failFirstScan = async route => {
    captured();
    await held;
    await route.abort("failed");
  };
  await page.route("**/api/discovery/scan", failFirstScan);
  await page.locator("#discover-cameras").click();
  await requestCaptured;
  await expect(page.locator("#discovery-message")).toContainText("Scanning the camera VLAN");
  expect(await page.locator("#discover-cameras").isDisabled()).toBe(true);
  release();
  await expect(page.locator("#discovery-message")).toContainText("Scan failed");
  await expect(page.locator("#discovery-retry-scan")).toBeVisible();
  await page.unroute("**/api/discovery/scan", failFirstScan);

  await page.locator("#discovery-retry-scan").click();
  await expect(page.locator("#discovery-message")).toContainText("Scan complete: 1 device(s) found");
  await expect(page.locator("#discovery-last-scan")).toContainText("1 device(s) found");
  await expect(page.locator("#discovery-table-scroll")).toContainText("DeterministicFixtureCamera");
  await expect(page.locator("[data-discovery-count]")).toContainText("3 not-added devices shown");
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
    "-t", "600", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "10",
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

test("bulk update reconciles a committed change after the browser loses its response", async ({ page }) => {
  const fixtures = seedFixtures(2);
  const updateRequests = [];
  page.on("request", request => {
    if (request.url().endsWith("/api/cameras/bulk-update")) updateRequests.push(request.postDataJSON());
  });
  await signIn(page);
  await openCameraSettings(page);
  for (const cameraId of fixtures.camera_ids) await page.locator(`[data-camera-select][value="${cameraId}"]`).check();
  await page.locator("#bulk-group").selectOption("");
  await page.locator("#apply-bulk").click();

  let committedResponse;
  const commitThenDrop = async route => {
    const response = await route.fetch();
    committedResponse = await response.json();
    await route.abort("failed");
  };
  await page.route("**/api/cameras/bulk-update", commitThenDrop);
  await page.locator('dialog.camera-bulk-dialog button.primary').click();
  await expect(page.locator('#camera-bulk-results-dialog [data-status="success"]')).toHaveCount(2);
  await expect(page.locator('#camera-bulk-results-dialog')).toContainText("The requested settings are visible in the inventory.");
  await expect(page.locator("#camera-bulk-reconcile")).toBeHidden();
  await expect(page.locator("#camera-bulk-retry-failed")).toBeHidden();
  expect(committedResponse.results).toHaveLength(2);
  expect(updateRequests).toHaveLength(1);
  expect(updateRequests[0].camera_ids).toEqual(fixtures.camera_ids);
  expect(updateRequests[0].group_name).toBeNull();
  const cameras = await page.evaluate(async () => (await (await fetch("/api/cameras")).json()).cameras);
  expect(cameras.filter(camera => fixtures.camera_ids.includes(camera.id)).every(camera => camera.group_name === null)).toBe(true);
  await page.unroute("**/api/cameras/bulk-update", commitThenDrop);
  await page.locator("#camera-bulk-dismiss-results").click();
});

test("group schedule, retention, storage, and AI save with inherited values and dirty group navigation", async ({ page }) => {
  const fixtures = seedFixtures(2, { extraGroup: true });
  await signIn(page);
  await openCameraSettings(page);
  await page.locator('[data-camera-workspace-button="groups"]').click();
  await page.locator(`#group-management-list [data-group-id="${fixtures.group_id}"]`).click();
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");

  await page.locator('[data-group-policy-tab="recording"]').click();
  await expect(page.locator('[data-group-policy-tab="recording"]')).toHaveAttribute("aria-selected", "true");
  await page.locator("#group-schedule-timezone").fill("UTC");
  await page.locator("#add-group-schedule-interval").click();
  const schedule = page.locator("#group-schedule-editor .schedule-interval").last();
  await schedule.locator('[data-schedule-field="start"]').fill("06:30");
  await schedule.locator('[data-schedule-field="end"]').fill("19:15");
  await schedule.locator('[data-schedule-field="mode"]').selectOption("motion");
  await page.locator('[data-group-tab-save="recording"]').click();
  await expect(page.locator('[data-group-tab-status="recording"]')).toHaveAttribute("data-state", "saved");
  const savedSchedule = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/schedule`)).json()), fixtures.group_id);
  expect(savedSchedule.configured).toBe(true);
  expect(savedSchedule.timezone).toBe("UTC");
  expect(savedSchedule.intervals).toEqual([{ days: [0, 1, 2, 3, 4], start: "06:30", end: "19:15", mode: "motion" }]);

  await page.locator("#group-retention-days").fill("14");
  await page.locator('[data-group-tab-save="recording"]').click();
  await expect(page.locator("#schedule-message")).toContainText("Saved group retention target: 14 days");
  const savedRetention = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/retention`)).json()), fixtures.group_id);
  expect(savedRetention.retention_days).toBe(14);

  await page.locator('[data-group-policy-tab="storage"]').click();
  await expect(page.locator('[data-group-policy-tab="storage"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('#group-storage-volume option[value="archive"]')).toHaveCount(1);
  await page.locator("#group-storage-volume").selectOption("archive");
  await page.locator('[data-group-tab-save="storage"]').click();
  await expect(page.locator('[data-group-tab-status="storage"]')).toHaveAttribute("data-state", "saved");
  const savedStorage = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/storage-volume`)).json()), fixtures.group_id);
  expect(savedStorage.volume_id).toBe("archive");

  await page.locator('[data-group-policy-tab="ai"]').click();
  await expect(page.locator('[data-group-policy-tab="ai"]')).toHaveAttribute("aria-selected", "true");
  await page.locator("#group-ai-enabled").uncheck();
  await page.locator("#group-ai-fps").fill("1.3");
  await page.locator("#group-ai-confidence").fill("0.7");
  await page.locator("#group-ai-substream").uncheck();
  await page.locator('[data-group-tab-save="ai"]').click();
  await expect(page.locator('[data-group-tab-status="ai"]')).toHaveAttribute("data-state", "saved");
  const savedAi = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/ai`)).json()), fixtures.group_id);
  expect(savedAi.enabled).toBe(false);
  expect(savedAi.analysis_fps).toBe(1.3);
  expect(savedAi.confidence_threshold).toBe(0.7);
  expect(savedAi.use_substream).toBe(false);

  const inheritedSchedule = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/schedule`)).json()), fixtures.camera_ids[0]);
  const inheritedRetention = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/retention`)).json()), fixtures.camera_ids[0]);
  const inheritedAi = await page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/ai`)).json()), fixtures.camera_ids[0]);
  const storage = await page.evaluate(async () => (await (await fetch("/api/storage")).json()));
  expect(inheritedSchedule.inherits_group).toBe(true);
  expect(inheritedSchedule.intervals).toEqual(savedSchedule.intervals);
  expect(inheritedRetention.policy_source).toBe("group");
  expect(inheritedRetention.retention_days).toBe(14);
  expect(inheritedAi.config_source).toBe("group");
  expect(inheritedAi.analysis_fps).toBe(1.3);
  expect(storage.volumes.find(volume => volume.id === "archive").cameras.some(camera => camera.id === fixtures.camera_ids[0])).toBe(true);

  const weekend = page.locator(`#group-management-list [data-group-id="${fixtures.extra_group_id}"]`);
  await page.locator("#group-ai-confidence").fill("0.78");
  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const groupAiUrl = `**/api/camera-groups/${fixtures.group_id}/ai`;
  const holdCommittedGroupSave = async route => {
    if (route.request().method() !== "PUT") return route.continue();
    const response = await route.fetch();
    captured();
    await held;
    await route.fulfill({ response });
  };
  await page.route(groupAiUrl, holdCommittedGroupSave);
  await weekend.click();
  await expect(page.locator("#camera-dirty-decision")).toBeVisible();
  await page.locator('#camera-dirty-decision [data-decision="save"]').click();
  await requestCaptured;
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  const committedBeforeNavigation = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/ai`)).json()), fixtures.group_id);
  expect(committedBeforeNavigation.confidence_threshold).toBe(0.78);
  release();
  await expect(page.locator("#group-editor-heading")).toContainText("Weekend");
  await page.unroute(groupAiUrl, holdCommittedGroupSave);

  const operations = page.locator(`#group-management-list [data-group-id="${fixtures.group_id}"]`);
  await operations.click();
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  await page.locator('[data-group-policy-tab="ai"]').click();
  await page.locator("#group-ai-confidence").fill("0.86");
  await weekend.click();
  await expect(page.locator("#camera-dirty-decision")).toBeVisible();
  await page.locator('#camera-dirty-decision [data-decision="cancel"]').click();
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  await expect(page.locator("#group-ai-confidence")).toHaveValue("0.86");

  const failGroupSave = async route => {
    if (route.request().method() !== "PUT") return route.continue();
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "fixture group save unavailable" }) });
  };
  await page.route(groupAiUrl, failGroupSave);
  await weekend.click();
  await page.locator('#camera-dirty-decision [data-decision="save"]').click();
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  await expect(page.locator("#group-ai-confidence")).toHaveValue("0.86");
  await expect(page.locator('[data-group-tab-status="ai"]')).toHaveAttribute("data-state", "error");
  await page.unroute(groupAiUrl, failGroupSave);

  await weekend.click();
  await page.locator('#camera-dirty-decision [data-decision="discard"]').click();
  await expect(page.locator("#group-editor-heading")).toContainText("Weekend");
  const unchanged = await page.evaluate(async id => (await (await fetch(`/api/camera-groups/${id}/ai`)).json()), fixtures.group_id);
  expect(unchanged.confidence_threshold).toBe(0.78);
});

test("administrator Cameras, Add Cameras, and Groups work responsively with keyboard and touch", async ({ page }) => {
  const fixtures = seedFixtures(2, { extraGroup: true });
  await signIn(page);
  await openCameraSettings(page);
  const widths = [320, 360, 601, 640, 700, 721, 740, 741, 768, 1024, 1440];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator("#camera-inventory-table")).toBeVisible();
    if (width <= 740) await expect(page.locator("[data-camera-pane-resizer]")).toBeHidden();
    else await expect(page.locator("[data-camera-pane-resizer]")).toBeVisible();
  }

  await page.setViewportSize({ width: 360, height: 780 });
  const firstCamera = page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[0]}"]`);
  await firstCamera.focus();
  await page.keyboard.press("Enter");
  const selectedEditor = page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`);
  await expect(selectedEditor).toBeVisible();
  const generalPanel = selectedEditor.locator('[data-camera-tab-panel="general-connection"]');
  const generalSave = generalPanel.locator('[data-camera-tab-save="general-connection"]');
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(selectedEditor).toBeVisible();
    await expect(generalPanel).toBeVisible();
    await expect(generalSave).toBeVisible();
    if (width <= 740) {
      await expect(page.locator(".camera-inventory-pane")).toBeHidden();
      await expect(page.locator("#camera-back-to-inventory")).toBeVisible();
    } else {
      await expect(page.locator(".camera-inventory-pane")).toBeVisible();
      await expect(page.locator(".camera-editor-pane")).toBeVisible();
      await expect(page.locator("#camera-back-to-inventory")).toBeHidden();
      const [inventoryBounds, resizerBounds, editorBounds, layoutBounds] = await Promise.all([
        page.locator(".camera-inventory-pane").boundingBox(),
        page.locator("[data-camera-pane-resizer]").boundingBox(),
        page.locator(".camera-editor-pane").boundingBox(),
        page.locator("#camera-admin-layout").boundingBox(),
      ]);
      expect(inventoryBounds).not.toBeNull();
      expect(resizerBounds).not.toBeNull();
      expect(editorBounds).not.toBeNull();
      expect(layoutBounds).not.toBeNull();
      expect(inventoryBounds.x + inventoryBounds.width).toBeLessThanOrEqual(resizerBounds.x);
      expect(resizerBounds.x + resizerBounds.width).toBeLessThanOrEqual(editorBounds.x);
      expect(editorBounds.x + editorBounds.width).toBeLessThanOrEqual(layoutBounds.x + layoutBounds.width);
      const renderedRatio = Math.round(inventoryBounds.width / (inventoryBounds.width + editorBounds.width) * 100);
      const accessibleRatio = Number(await page.locator("[data-camera-pane-resizer]").getAttribute("aria-valuenow"));
      expect(Math.abs(renderedRatio - accessibleRatio)).toBeLessThanOrEqual(1);
    }
    const controlBounds = await generalSave.boundingBox();
    expect(controlBounds).not.toBeNull();
    expect(controlBounds.x).toBeGreaterThanOrEqual(0);
    expect(controlBounds.x + controlBounds.width).toBeLessThanOrEqual(width);
  }
  await page.setViewportSize({ width: 360, height: 780 });
  await page.locator("#camera-back-to-inventory").click();
  await firstCamera.scrollIntoViewIfNeeded();
  const firstBox = await firstCamera.boundingBox();
  await page.touchscreen.tap(firstBox.x + firstBox.width / 2, firstBox.y + firstBox.height / 2);
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`)).toBeVisible();
  await page.locator("#camera-back-to-inventory").click();

  await page.locator('[data-camera-workspace-button="add"]').click();
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator("#camera-add-mode-nav")).toBeVisible();
  }
  await page.setViewportSize({ width: 360, height: 780 });
  const discover = page.locator('[data-camera-add-mode="discover"]');
  await discover.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator('[data-camera-add-mode="manual"]')).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  const csv = page.locator('[data-camera-add-mode="csv"]');
  await expect(csv).toHaveAttribute("aria-selected", "true");
  await csv.scrollIntoViewIfNeeded();
  const csvBox = await csv.boundingBox();
  await page.touchscreen.tap(csvBox.x + csvBox.width / 2, csvBox.y + csvBox.height / 2);
  await expect(csv).toHaveAttribute("aria-selected", "true");

  await page.locator('[data-camera-workspace-button="groups"]').click();
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator("#group-management-list")).toBeVisible();
  }
  await page.setViewportSize({ width: 360, height: 780 });
  const operations = page.locator(`#group-management-list [data-group-id="${fixtures.group_id}"]`);
  await operations.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#group-editor-heading")).toContainText("Operations");
  const general = page.locator('[data-group-policy-tab="general"]');
  await general.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.locator('[data-group-policy-tab="recording"]')).toHaveAttribute("aria-selected", "true");
  const weekend = page.locator(`#group-management-list [data-group-id="${fixtures.extra_group_id}"]`);
  await weekend.scrollIntoViewIfNeeded();
  const weekendBox = await weekend.boundingBox();
  await page.touchscreen.tap(weekendBox.x + weekendBox.width / 2, weekendBox.y + weekendBox.height / 2);
  await expect(page.locator("#group-editor-heading")).toContainText("Weekend");
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
  await expect.poll(async () => page.locator(".camera-preview-image").getAttribute("src")).toBeNull();

  await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[0]}"]`).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`)).toBeVisible();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");
  await expect.poll(async () => page.locator(".camera-preview-image").getAttribute("src")).toBeNull();
  await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[1]}"]`).click();
  await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[1]}"]`)).toBeVisible();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");

  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await page.locator('#console-nav [data-screen="live"]').click();
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[1])).toBe(0);
  expect(mediaRequests.filter(url => url.includes("/live?quality=low"))).toHaveLength(3);
});

test("a pending preview is discarded on quick selection changes and an active preview stops on archive or removal", async ({ page }) => {
  test.setTimeout(90_000);
  const fixtures = seedFixtures(2);
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.camera_ids[0]);

  let captured;
  const requestCaptured = new Promise(resolve => { captured = resolve; });
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let handlerStarted = false;
  let finishHandler;
  const handlerFinished = new Promise(resolve => { finishHandler = resolve; });
  const firstPreview = url => url.pathname.endsWith(`/api/cameras/${fixtures.camera_ids[0]}/live`) && url.searchParams.get("quality") === "low";
  const delayPreview = async route => {
    handlerStarted = true;
    captured();
    await held;
    try { await route.continue(); } catch { /* Selection may have aborted the request before it was released. */ }
    finally { finishHandler(); }
  };
  await page.route(firstPreview, delayPreview);
  try {
    await page.locator("[data-preview-start]").click();
    await requestCaptured;
    await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "loading");
    await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[1]}"]`).click();
    await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[1]}"]`)).toBeVisible();
    await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");
    await expect(page.locator(".camera-preview-image")).toBeHidden();
    await page.locator(`#camera-inventory-table button[data-camera-id="${fixtures.camera_ids[0]}"]`).click();
    await expect(page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`)).toBeVisible();
    await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "idle");
    await expect.poll(async () => page.locator(".camera-preview-image").getAttribute("src")).toBeNull();
  } finally {
    release();
    if (handlerStarted) await handlerFinished;
    await page.unroute(firstPreview, delayPreview);
  }
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBe(0);

  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBeGreaterThan(0);

  const cameraEditor = page.locator(`#camera-list article[data-camera-id="${fixtures.camera_ids[0]}"]`);
  await page.locator('[role="tab"][data-camera-tab="health-history"]').click();
  page.once("dialog", dialog => dialog.accept());
  await cameraEditor.getByRole("button", { name: "Archive camera" }).click();
  await expect(cameraEditor.getByRole("button", { name: "Restore camera" })).toBeVisible();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "unavailable");
  await expect(page.locator(".camera-preview-image")).toBeHidden();
  const archivedStatus = await page.evaluate(async id => {
    const response = await fetch(`/api/cameras/${id}/live/status`);
    return { status: response.status, body: await response.json() };
  }, fixtures.camera_ids[0]);
  expect(archivedStatus.status).toBe(410);

  await cameraEditor.getByRole("button", { name: "Restore camera" }).click();
  await expect(cameraEditor.getByRole("button", { name: "Archive camera" })).toBeVisible();
  await expect.poll(async () => page.evaluate(async id => {
    const response=await fetch(`/api/cameras/${id}/live/status`);
    return {status:response.status,body:await response.json()};
  }, fixtures.camera_ids[0])).toMatchObject({status:200,body:{active_viewers:0}});

  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBeGreaterThan(0);
  seedFixtures(0);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-list")).toContainText("Camera 001 is no longer available");
  await expect(page.locator(".camera-preview-image")).toHaveCount(0);
  seedFixtures(2);
  await page.locator("#camera-inventory-refresh").click();
  await expect(page.locator("#camera-inventory-load-status")).toContainText("Camera 001 is available again");
  await expect.poll(async () => page.evaluate(async id => (await (await fetch(`/api/cameras/${id}/live/status`)).json()).active_viewers, fixtures.camera_ids[0])).toBe(0);
  await expect(page.locator("#camera-list article[data-camera-id]")).toHaveCount(0);
});

test("preview failure is visible, retry starts media, and logout tears down the live viewer", async ({ page }) => {
  test.setTimeout(90_000);
  const fixtures = seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  await selectCamera(page, fixtures.primary_camera_id);

  let firstRequest = true;
  const failFirstPreview = async route => {
    if (!firstRequest) return route.continue();
    firstRequest = false;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Deterministic browser fixture preview failure" }),
    });
  };
  const isPrimaryLowPreview = url => url.pathname.endsWith(`/api/cameras/${fixtures.primary_camera_id}/live`) && url.searchParams.get("quality") === "low";
  await page.route(isPrimaryLowPreview, failFirstPreview);
  await page.locator("[data-preview-start]").click();
  await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "failed");
  await expect(page.locator("[data-preview-status]")).toContainText("Deterministic browser fixture preview failure");
  await expect(page.locator("[data-preview-retry]")).toBeVisible();
  await page.unroute(isPrimaryLowPreview, failFirstPreview);

  const observer = await playwrightRequest.newContext({ baseURL: "http://127.0.0.1:8765" });
  try {
    const observerLogin = await observer.post("/api/auth/login", { data: { username: "camera-admin", password: FIXTURE_PASSWORD } });
    expect(observerLogin.status()).toBe(200);
    await page.locator("[data-preview-retry]").click();
    await expect(page.locator("[data-preview-status]")).toHaveAttribute("data-state", "fresh", { timeout: 30_000 });
    await expect.poll(async () => (await (await observer.get(`/api/cameras/${fixtures.primary_camera_id}/live/status`)).json()).active_viewers).toBeGreaterThan(0);

    await page.locator("#logout").click();
    await expect(page.locator("#login")).toBeVisible();
    await expect.poll(async () => (await (await observer.get(`/api/cameras/${fixtures.primary_camera_id}/live/status`)).json()).active_viewers).toBe(0);
    await expect((await observer.post("/api/auth/logout")).status()).toBe(204);
  } finally {
    await observer.dispose();
  }
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
  const browserErrorDetails = [];
  page.on("request", request => {
    if (/\/api\/cameras\/[^/]+\/live(?:\?|$)/.test(request.url())) mediaRequests.push(request.url());
  });
  page.on("pageerror", error => { browserErrors.page += 1; browserErrorDetails.push(`page: ${error.message}`); });
  page.on("console", message => { if (message.type() === "error") { browserErrors.console += 1; browserErrorDetails.push(`console: ${message.text()}`); } });
  page.on("requestfailed", request => { browserErrors.failed_requests += 1; browserErrorDetails.push(`request failed ${new URL(request.url()).pathname}: ${request.failure()?.errorText||"unknown error"}`); });
  page.on("response", response => { if (response.status() >= 400) { browserErrors.http_errors += 1; browserErrorDetails.push(`HTTP ${response.status()} ${new URL(response.url()).pathname}`); } });
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
  expect(errorCount, browserErrorDetails.join("\n") || "No browser error details were captured.").toBe(0);
  expect(evidence.sample_counts.filter_feedback).toBe(10);
  expect(evidence.sample_counts.panel_feedback).toBe(5);
});

test("resizer persists only a bounded harmless preference, and camera assets resolve", async ({ page }) => {
  seedFixtures(1);
  await signIn(page);
  await openCameraSettings(page);
  const resizer = page.locator("[data-camera-pane-resizer]");
  await resizer.focus();
  await page.keyboard.press("End");
  const upperBound = Number(await resizer.getAttribute("aria-valuemax"));
  const lowerBound = Number(await resizer.getAttribute("aria-valuemin"));
  expect(lowerBound).toBeGreaterThanOrEqual(25);
  expect(upperBound).toBeLessThanOrEqual(75);
  await expect(resizer).toHaveAttribute("aria-valuenow", String(upperBound));
  const renderedRatio = await page.locator("#camera-admin-layout").evaluate(layout => {
    const inventory = layout.querySelector(".camera-inventory-pane").getBoundingClientRect();
    const editor = layout.querySelector(".camera-editor-pane").getBoundingClientRect();
    return Math.round(inventory.width / (inventory.width + editor.width) * 100);
  });
  expect(Math.abs(renderedRatio-upperBound)).toBeLessThanOrEqual(1);
  const preference = await page.evaluate(() => ({
    keys: Object.keys(localStorage),
    value: localStorage.getItem(`nvr-camera-pane-ratio:${encodeURIComponent("camera-admin")}`),
  }));
  expect(preference.value).toBe(String(upperBound));
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
  await page.locator('[data-settings-category="ai-runtime"]').click();
  await expect(page.locator("#ai-runtime")).toBeVisible();
  await page.locator('[data-settings-category="integrations"]').click();
  await page.locator('#settings-integrations-link [data-open-camera-admin="ai"]').click();
  await expect(page.locator('[data-settings-category="ai-runtime"]')).toHaveAttribute("aria-current", "true");
  await expect(page.locator("#ai-runtime")).toBeVisible();
  await page.locator('[data-settings-category="appearance"]').click();
  await page.locator("#brand-app-name").fill("Acceptance Fixture NVR");
  await page.locator("#brand-primary-color").fill("#123456");
  await page.locator("#brand-accent-color").fill("#ffffff");
  await page.locator("#brand-save").click();
  await expect(page.locator("#branding-message")).toContainText("Branding saved and published");
  await expect(page.locator("#brand-header-name")).toHaveText("Acceptance Fixture NVR");
  await expect(page.locator("html")).toHaveCSS("--accent-ink", "#000");
  await page.locator("#theme-choice").selectOption("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveCSS("--accent-ink", "#000");
});
