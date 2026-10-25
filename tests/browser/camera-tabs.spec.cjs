// Real-browser journeys for the camera editor tabs (#99): Recording, Motion, AI,
// Schedule & Retention and Health & History. Odd-numbered fixture cameras belong to "Operations".
const fs = require("node:fs");
const path = require("node:path");
const { test, expect, seedFixtures, signInNext } = require("./helpers.cjs");
const { loadFixtureEnvironment } = require("./fixture_environment.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });
const editor = page => page.locator(".editor");

async function openTab(page, cameraName, tab) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: cameraName, exact: true }).click();
  await page.getByRole("tab", { name: tab }).click();
  await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
}

async function camera(page, id) {
  const body = await (await page.request.get("/api/cameras")).json();
  return body.cameras.find(item => item.id === id);
}

test("Recording saves its own settings, survives reload, and reports a partial save honestly", async ({ page }) => {
  const fixtures = seedFixtures(3);
  await signInNext(page);
  await openTab(page, "Camera 002", "Recording");
  const form = page.getByRole("form", { name: "Recording settings" });
  await form.getByLabel("Mode when no schedule applies").selectOption("motion");
  await form.getByLabel("Pre-roll (seconds)").fill("7");
  await form.getByLabel("Post-roll (seconds)").fill("45");
  await form.getByLabel("Group").selectOption("Operations");
  await page.route("**/api/cameras/bulk-update", route => route.fulfill({ status: 500, body: JSON.stringify({ detail: "Simulated failure" }), contentType: "application/json" }));
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("alert")).toHaveText("Saved recording settings. Group not saved: Simulated failure.");
  // Only the unsaved part stays dirty.
  await expect(form.getByLabel("Group")).toHaveValue("Operations");
  await expect(form.getByRole("button", { name: "Save" })).toBeEnabled();
  await page.unroute("**/api/cameras/bulk-update");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toHaveText("Saved group.");

  await page.reload();
  await openTab(page, "Camera 002", "Recording");
  await expect(form.getByLabel("Pre-roll (seconds)")).toHaveValue("7");
  await expect(form.getByLabel("Post-roll (seconds)")).toHaveValue("45");
  await expect(form.getByLabel("Group")).toHaveValue("Operations");
  const saved = await camera(page, fixtures.camera_ids[1]);
  expect([saved.recording_mode, saved.motion_pre_roll_seconds, saved.motion_post_roll_seconds, saved.group_name]).toEqual(["motion", 7, 45, "Operations"]);
});

test("motion zones are drawn, moved and typed, and round-trip as normalized coordinates", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page);
  await openTab(page, "Camera 002", "Motion");
  const form = page.getByRole("form", { name: "Motion settings" });
  const editorGroup = form.getByRole("group", { name: "Server motion zones" });
  await expect(editorGroup.getByText(/No recorded frame yet/)).toBeVisible();

  // Pointer: draw from 10%,10% to 40%,50%.
  const stage = editorGroup.locator(".zone-stage");
  const box = await stage.boundingBox();
  await page.mouse.move(box.x + box.width * 0.1, box.y + box.height * 0.1);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.4, box.y + box.height * 0.5, { steps: 5 });
  await page.mouse.up();
  await expect(editorGroup.getByRole("button", { name: /^Zone 1:/ })).toBeVisible();

  // Keyboard: move right by 5% and grow by 1%.
  const zone = editorGroup.getByRole("button", { name: /^Zone 1:/ });
  await zone.focus();
  await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Alt+ArrowDown");
  // Typed: set exact values.
  await editorGroup.getByLabel("Zone 1 left percent").fill("20");
  await editorGroup.getByLabel("Zone 1 top percent").fill("10");
  await editorGroup.getByLabel("Zone 1 width percent").fill("30");
  await editorGroup.getByLabel("Zone 1 height percent").fill("40");
  await editorGroup.getByRole("button", { name: "Add zone" }).click();
  await editorGroup.getByRole("button", { name: "Remove zone 2" }).click();
  await form.getByLabel("Detect motion on the server when ONVIF events are unavailable").check();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toHaveText("Saved motion settings.");
  expect((await camera(page, fixtures.camera_ids[1])).motion_zones).toEqual([{ x: 0.2, y: 0.1, width: 0.3, height: 0.4 }]);

  await page.reload();
  await openTab(page, "Camera 002", "Motion");
  await expect(form.getByRole("group", { name: "Server motion zones" }).getByLabel("Zone 1 left percent")).toHaveValue("20");
  await expect(form.getByLabel("Detect motion on the server when ONVIF events are unavailable")).toBeChecked();
});

test("zones keep their coordinates when a representative frame appears", async ({ page }) => {
  const fixtures = seedFixtures(2, { footage: true });
  await signInNext(page);
  await page.request.post(`/api/cameras/${fixtures.camera_ids[0]}/motion-source`, {
    data: { fallback_enabled: true, zones: [{ x: 0.25, y: 0.5, width: 0.5, height: 0.25 }] },
  });
  await openTab(page, "Camera 001", "Motion");
  const editorGroup = page.getByRole("group", { name: "Server motion zones" });
  await expect(editorGroup.locator(".zone-stage")).toHaveAttribute("data-frame", "loaded", { timeout: 15_000 });
  await expect(editorGroup.getByLabel("Zone 1 left percent")).toHaveValue("25");
  await expect(editorGroup.getByLabel("Zone 1 height percent")).toHaveValue("25");
  await expect(page.getByRole("form", { name: "Motion settings" }).getByRole("button", { name: "Save" })).toBeDisabled();
});

test("AI overrides the group or inherits it, with the source visible", async ({ page }) => {
  const fixtures = seedFixtures(3);
  await signInNext(page);
  await page.request.put(`/api/camera-groups/${fixtures.group_id}/ai`, {
    data: { enabled: false, analysis_fps: 0.5, confidence_threshold: 0.55, use_substream: true, zones: [] },
  });
  await openTab(page, "Camera 003", "AI");
  const form = page.getByRole("form", { name: "AI settings" });
  await expect(form.getByLabel("Use the Operations group's AI settings")).toBeChecked();
  await form.getByLabel("Use the Operations group's AI settings").uncheck();
  await form.getByLabel("Analyse the lower-resolution substream").uncheck();
  await form.getByLabel("Frames analysed per second").fill("1.5");
  await form.getByLabel("Confidence threshold").fill("0.7");
  await form.getByRole("group", { name: "AI zones" }).getByRole("button", { name: "Add zone" }).click();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved AI settings (camera)");
  const ai = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[2]}/ai`)).json();
  expect([ai.config_source, ai.analysis_fps, ai.confidence_threshold, ai.zones]).toEqual(["camera", 1.5, 0.7, [{ x: 0.25, y: 0.25, width: 0.5, height: 0.5 }]]);

  await page.reload();
  await openTab(page, "Camera 003", "AI");
  await expect(form.getByLabel("Frames analysed per second")).toHaveValue("1.5");
  await form.getByLabel("Use the Operations group's AI settings").check();
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved AI settings (group)");
});

test("schedules reject overlaps, keep overnight intervals on their start day, and removing an override restores the group's", async ({ page }) => {
  const fixtures = seedFixtures(3);
  await signInNext(page);
  await page.request.put(`/api/camera-groups/${fixtures.group_id}/schedule`, {
    data: { timezone: "America/New_York", intervals: [{ days: [0, 1, 2, 3, 4], start: "07:00", end: "19:00", mode: "continuous" }] },
  });
  await openTab(page, "Camera 003", "Schedule & Retention");
  const form = page.getByRole("form", { name: "Schedule and retention settings" });
  await expect(form.getByRole("status").first()).toContainText("Schedule from its group");
  await form.getByLabel("This camera's own schedule").check();
  // The group's intervals are copied in as a starting point.
  await expect(form.getByLabel("Interval 1 start")).toHaveValue("07:00");
  await form.getByLabel("Time zone").fill("America/Chicago");
  await form.getByRole("button", { name: "Add interval" }).click();
  await form.getByLabel("Friday in interval 2").check();
  await form.getByLabel("Interval 2 start").fill("18:00");
  await form.getByLabel("Interval 2 end").fill("06:00");
  await form.getByLabel("Interval 2 mode").selectOption("motion");
  await expect(form.getByRole("alert")).toContainText("Interval 2 overlaps interval 1 on Friday at 18:00");
  await expect(form.getByRole("button", { name: "Save" })).toBeDisabled();
  await form.getByLabel("Interval 2 start").fill("19:00");
  await expect(form.getByRole("alert")).toHaveCount(0);
  await expect(form.getByText("Interval 2 · overnight")).toBeVisible();
  // The overnight interval continues into Saturday morning on the overview.
  await expect(form.locator(".schedule-day").nth(5).locator(".schedule-band")).toHaveText("2");
  await form.getByLabel("Keep this camera's footage for a specific time").check();
  await form.getByLabel("Days to keep footage").fill("21");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status").last()).toHaveText("Saved schedule and retention.");

  await page.reload();
  await openTab(page, "Camera 003", "Schedule & Retention");
  await expect(form.getByLabel("Time zone")).toHaveValue("America/Chicago");
  await expect(form.getByLabel("Interval 2 end")).toHaveValue("06:00");
  await expect(form.getByLabel("Days to keep footage")).toHaveValue("21");
  const schedule = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[2]}/schedule`)).json();
  expect(schedule.intervals[1]).toEqual({ days: [4], start: "19:00", end: "06:00", mode: "motion" });

  // Remove both overrides; a failing retention removal is reported separately.
  await form.getByLabel("Use the Operations group's schedule").check();
  await form.getByLabel("Keep this camera's footage for a specific time").uncheck();
  await page.route("**/retention", route => (route.request().method() === "DELETE"
    ? route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Simulated outage" }) }) : route.continue()));
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("alert")).toHaveText("Saved schedule. Retention not saved: Simulated outage.");
  await expect(form.getByRole("status").first()).toContainText("Schedule from its group");
  await expect(form.getByLabel("Keep this camera's footage for a specific time")).not.toBeChecked();
  await page.unroute("**/retention");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status").last()).toHaveText("Saved retention.");
  await expect(form.getByRole("status").first()).toContainText("Retention: 30 days from the system default");
});

test("Health & History shows write health and opens the camera in Playback", async ({ page }) => {
  seedFixtures(2, { footage: true });
  await signInNext(page);
  await openTab(page, "Camera 001", "Health & History");
  await expect(editor(page).locator("dl.details")).toContainText("Last footage written");
  await expect(editor(page).getByRole("region", { name: "Coverage problems" })).toContainText("Camera offline");
  await expect(editor(page).getByRole("region", { name: "Recent motion events" }).getByRole("listitem")).toHaveCount(2);
  await editor(page).getByRole("button", { name: "Open in Playback" }).click();
  await expect(page).toHaveURL(/\/playback$/);
  await expect(page.getByRole("group", { name: /^Camera 001.*: playback$/ })).toBeVisible();
});

test("operators see effective values read-only on every tab", async ({ page }) => {
  seedFixtures(3);
  await signInNext(page, "camera-operator");
  for (const tab of ["Recording", "Motion", "AI", "Schedule & Retention"]) {
    await openTab(page, "Camera 003", tab);
    await expect(editor(page).getByRole("button", { name: "Save" })).toHaveCount(0);
    const controls = editor(page).locator(".tab-form").locator("input, select, textarea");
    await expect(controls.first()).toBeVisible();
    for (const control of await controls.all()) await expect(control).toBeDisabled();
  }
});

test("Health & History verifies footage against its signed chain and names an altered segment", async ({ page }) => {
  seedFixtures(1, { footage: true });
  await signInNext(page);
  await openTab(page, "Camera 001", "Health & History");
  const region = editor(page).getByRole("region", { name: "Footage verification" });
  await region.getByLabel("Range").selectOption({ label: "Last 24 hours" });
  // The background worker hashes new footage within a few seconds.
  await expect(async () => {
    await region.getByRole("button", { name: "Verify footage" }).click();
    await expect(region.getByRole("status")).toContainText("2 segments checked; no tampering found", { timeout: 2000 });
  }).toPass({ timeout: 30_000 });

  const recordings = loadFixtureEnvironment().NVR_RECORDINGS_PATH;
  const cameraId = (await (await page.request.get("/api/cameras")).json()).cameras.find(item => item.name === "Camera 001").id;
  const altered = path.join(recordings, cameraId, "fixture-0150.mp4");
  const bytes = fs.readFileSync(altered);
  bytes[Math.floor(bytes.length / 2)] ^= 0xff;
  fs.writeFileSync(altered, bytes);
  await region.getByRole("button", { name: "Verify footage" }).click();
  await expect(region.getByRole("status")).toContainText("1 segment failed verification");
  await expect(region.getByRole("list", { name: "Segments that failed verification" }).getByRole("listitem")).toHaveText([/^Altered: the file no longer matches its hash · segment \d+/]);
});
