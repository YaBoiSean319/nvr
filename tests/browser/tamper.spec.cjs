// Real-browser journeys for camera tamper detection (#122): turning it on and tuning it, what the tab reports as the view
// is learned, covered and learned again, and a rule on it that raises an alarm.
const { test, expect, seedFixtures, signInNext, feedTamper } = require("./helpers.cjs");

const nav = page => page.getByRole("navigation", { name: "Workspaces" });

async function openTamper(page, cameraName) {
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("region", { name: "Camera inventory" }).getByRole("button", { name: cameraName, exact: true }).click();
  await page.getByRole("tab", { name: "Tamper" }).click();
  await expect(page.getByRole("tab", { name: "Tamper" })).toHaveAttribute("aria-selected", "true");
  return page.getByRole("form", { name: "Tamper detection" });
}

test("detection is switched on, learns the view, reports a covered camera and learns again on request", async ({ page }) => {
  const fixtures = seedFixtures(2);
  await signInNext(page);
  const form = await openTamper(page, "Camera 001");
  await expect(form.getByRole("note")).toContainText("Off");
  await form.getByRole("checkbox", { name: /Detect a covered, moved or defocused camera/ }).check();
  await form.getByLabel("Sensitivity (1–100)").fill("60");
  await form.getByLabel("A change must last (seconds)").fill("30");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved. Tamper detection is on.");
  const saved = await (await page.request.get(`/api/cameras/${fixtures.camera_ids[0]}/tamper`)).json();
  expect([saved.enabled, saved.sensitivity, saved.sustain_seconds]).toEqual([true, 60, 30]);
  await expect(form.getByRole("note")).toContainText("Learning the view · 0 of 5 frames");

  expect(feedTamper(1, "normal", 5).state).toBe("ok");
  await page.reload();
  await openTamper(page, "Camera 001");
  await expect(form.getByRole("note")).toContainText("Normal");

  expect(feedTamper(1, "covered", 4).state).toBe("covered");
  await page.reload();
  await openTamper(page, "Camera 001");
  await expect(form.getByRole("note")).toContainText("Covered");
  const incidents = (await (await page.request.get("/api/incidents")).json()).incidents;
  expect(incidents.filter(item => item.fault_type === "tamper_covered" && item.fault_active)).toHaveLength(1);
  expect(await (await page.request.get("/metrics")).status()).toBe(403); // A session is no scraper, and the incident is the signal here.

  await form.getByRole("button", { name: "Re-learn reference" }).click();
  await expect(form.getByRole("note")).toContainText("Learning the view · 0 of 5 frames");
  const after = (await (await page.request.get("/api/incidents")).json()).incidents;
  expect(after.filter(item => item.fault_type === "tamper_covered" && item.fault_active)).toHaveLength(0);
  expect(feedTamper(1, "moved", 5).state).toBe("ok"); // The new view is the normal one now.
});

test("a rule on tampering raises an alarm the inbox names, and test mode replays the detection", async ({ page }) => {
  seedFixtures(2);
  await signInNext(page);
  const form = await openTamper(page, "Camera 001");
  await form.getByRole("checkbox", { name: /Detect a covered, moved or defocused camera/ }).check();
  await form.getByLabel("A change must last (seconds)").fill("30");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form.getByRole("status")).toContainText("Saved.");

  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  const workspace = page.getByRole("region", { name: "Rules" });
  await workspace.getByRole("button", { name: "New rule" }).click();
  const editor = page.getByRole("region", { name: /^Rule: / });
  await editor.getByLabel("Name").fill("Lens covered");
  await editor.getByRole("checkbox", { name: "Camera 001" }).check();
  await editor.getByLabel("When an object").selectOption("tamper");
  await expect(editor.getByRole("group", { name: "Kinds of tampering" })).toBeVisible();
  await expect(editor.getByRole("checkbox", { name: "Defocused" })).toBeChecked();
  await editor.getByRole("checkbox", { name: "Defocused" }).uncheck();
  await expect(editor.getByRole("group", { name: "Objects" })).toHaveCount(0); // A tamper rule has no object classes.
  await editor.getByRole("button", { name: "Save" }).click();
  await expect(editor.getByRole("form", { name: "Rule settings" }).getByRole("status")).toContainText("Saved.");
  const saved = (await (await page.request.get("/api/rules")).json()).rules;
  expect(saved[0].condition).toEqual({ type: "tamper", kinds: ["covered", "moved"] });
  expect(saved[0].classes).toEqual([]);
  await expect(workspace.getByRole("button", { name: /Lens covered/ })).toContainText("covered, moved");

  feedTamper(1, "normal", 5);
  feedTamper(1, "covered", 4);
  await page.reload();
  await page.getByRole("status", { name: "Operational status" }).getByRole("button", { name: /alarm/i }).click();
  const inbox = page.getByRole("dialog", { name: "Alarms" });
  await expect(inbox.getByRole("list", { name: "Alarms" })).toContainText("Lens covered: Camera covered on Camera 001");

  // Test mode replays the detection that was just made.
  await page.reload();
  await nav(page).getByRole("link", { name: "Cameras" }).click();
  await page.getByRole("navigation", { name: "Camera administration" }).getByRole("link", { name: "Rules" }).click();
  await workspace.getByRole("button", { name: /Lens covered/ }).click();
  const testing = page.getByRole("region", { name: "Test mode" });
  await testing.getByRole("button", { name: "Test against the last 24 hours" }).click();
  await expect(testing.getByRole("status")).toContainText("The rule would have fired 1 time.");
  await expect(testing.getByRole("table")).toContainText("Camera covered".replace("Camera covered", "Covered"));
});
